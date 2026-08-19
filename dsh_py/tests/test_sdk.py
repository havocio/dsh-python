"""SDK（进程内 dsh-sdk client 翻译）与 headless CLI 测试。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from typing import Any

from dsh_py.sdk import DeepSeekHarness, HarnessSession, RunResult, final_response
from dsh_py.services.llm import ChunkType, LlmAdapter, StreamChunk
from dsh_py.services.message import as_text


class MockAdapter(LlmAdapter):
    """固定回复的 mock 模型。"""

    def __init__(self, reply: str = "（mock）我已收到你的消息。") -> None:
        self.reply = reply

    async def stream(self, options: Any):  # type: ignore[override]
        yield StreamChunk(ChunkType.TEXT_DELTA, text=self.reply)
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


def _mock_harness(**kwargs) -> DeepSeekHarness:
    """构造装配了 mock 适配器的 harness（mock 挂在 openai 上，即 CLI 默认 provider）。"""
    harness = DeepSeekHarness(**kwargs)
    return harness


async def _register_mock(harness: DeepSeekHarness) -> None:
    await harness.start()
    harness.ctx.llm.register_adapter(["openai"], MockAdapter(), replace=True)


async def test_run_returns_final_response():
    harness = _mock_harness()
    await _register_mock(harness)
    result = await harness.run("你好", {"provider": "openai", "model": "gpt-4o"})
    assert isinstance(result, RunResult)
    assert result.session_id.startswith("session-")
    assert "（mock）" in result.final_response
    assert result.events, "事件列表不应为空"
    types = [e.type for e in result.events]
    assert "assistant/message" in types
    await harness.close()
    print("  ✓ run 返回 RunResult（session_id/final_response/events）")


async def test_session_reuse_continues_history():
    harness = _mock_harness()
    await _register_mock(harness)
    sess = harness.session("s1")
    r1 = await sess.run("第一轮", {"provider": "openai", "model": "gpt-4o"})
    r2 = await sess.run("第二轮", {"provider": "openai", "model": "gpt-4o"})
    # 同一会话：第二轮事件包含前一轮的 assistant 消息（历史延续）
    roles = [e.type for e in r2.events]
    assert roles.count("assistant/message") == 2   # 两轮各产出 1 条
    assert roles.count("turn/end") == 2            # 两轮各收尾 1 次
    # 新 run 返回到当前为止的全部事件（含历史）
    assert len(r2.events) > len(r1.events)
    await harness.close()
    print("  ✓ 同一 session id 复用：历史延续")


async def test_multi_session_independent():
    harness = _mock_harness()
    await _register_mock(harness)
    a = await harness.run("A 的问题", {"provider": "openai", "model": "gpt-4o", "sessionId": "sA"})
    b = await harness.run("B 的问题", {"provider": "openai", "model": "gpt-4o", "sessionId": "sB"})
    assert a.session_id == "sA" and b.session_id == "sB"
    assert a.session is not b.session
    # A 的会话只含 A 的消息（按文本内容校验，不受记忆插件注入影响）
    a_user_texts = _user_texts(a.events)
    assert "A 的问题" in a_user_texts and "B 的问题" not in a_user_texts
    await harness.close()
    print("  ✓ 多会话相互独立")


async def test_close_disposes_plugins():
    harness = _mock_harness()
    await _register_mock(harness)
    ctx = harness.ctx
    assert ctx.has_service("llm") and ctx.has_service("agentLoop")
    await harness.close()
    # 插件全部卸载：核心服务被回收，ctx 引用被清除
    assert not ctx.has_service("llm")
    assert not ctx.has_service("agentLoop")
    assert harness._ctx is None
    print("  ✓ close() 卸载全部插件（Fiber dispose 回收服务）")


async def test_default_profile_assembly():
    # 默认装配点（configs/profile.py）：应注册 openai 兼容 7 厂商 + 长记忆
    harness = _mock_harness()
    await harness.start()
    providers = [p.id for p in harness.ctx.llm.list_providers()]
    assert {"openai", "deepseek", "qwen", "zhipu", "moonshot", "ollama", "vllm"} <= set(providers)
    assert harness.ctx.has_service("systemPrompt") is False  # 默认业务 profile 不挂 systemPrompt
    await harness.close()
    print("  ✓ 默认装配点：7 厂商注册")


async def test_inline_profile_assembly():
    # 内联 profile 列表装配（无需任何文件）
    def _marker(ctx, config=None):
        ctx.provide("sdk_marker", "inline")

    harness = DeepSeekHarness(profile=[_marker])
    await harness.start()
    assert harness.ctx.has_service("sdk_marker")
    await harness.close()
    print("  ✓ 内联 profile 列表装配")


def test_final_response_empty_without_assistant():
    assert final_response([]) == ""
    print("  ✓ final_response 无 assistant 消息时返回空串")


def test_headless_cli_prints_final_and_exits():
    # 子进程验证 headless：输出含 mock 回复，且进程正常退出（exit code 0）
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "dsh_py.cli", "--mock", "--message", "写个冒泡排序"],
        capture_output=True, text=True, timeout=60, env=env, cwd=root,
    )
    assert proc.returncode == 0, f"headless 退出码非 0：{proc.stderr[-500:]}"
    assert "（mock）" in proc.stdout, f"headless 输出缺少 mock 回复：{proc.stdout[-500:]}"
    print("  ✓ headless CLI：一条任务 → 打印最终文本 → 正常退出")


def _user_texts(events) -> list[str]:
    """提取事件中全部 user/message 的文本（供跨会话隔离校验）。

    user/message 事件的 data 直接是 Message 对象（与 assistant/message 的
    ``{"message": ...}`` 包裹形式不同），此处兼容两种形态。
    """
    texts = []
    for ev in events:
        if ev.type == "user/message":
            data = ev.data
            message = data if hasattr(data, "content") else data.get("message")
            if message is not None:
                texts.append(as_text(message.content))
    return texts


async def main():
    print("== test_sdk ==")
    await test_run_returns_final_response()
    await test_session_reuse_continues_history()
    await test_multi_session_independent()
    await test_close_disposes_plugins()
    await test_default_profile_assembly()
    await test_inline_profile_assembly()
    test_final_response_empty_without_assistant()
    test_headless_cli_prints_final_and_exits()
    print("OK: SDK 与 headless CLI 测试通过")


if __name__ == "__main__":
    asyncio.run(main())

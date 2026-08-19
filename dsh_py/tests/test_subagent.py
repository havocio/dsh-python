"""子代理工具（subagent）的验证（第 3 层第三批）。

运行：python dsh_py/tests/test_subagent.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.plugins.subagent import apply as apply_subagent
from dsh_py.services.agent import AgentOptions
from dsh_py.services.llm import ChunkType, LlmAdapter, StreamChunk
from dsh_py.services.message import TextBlock, ToolCallBlock, ToolResultBlock, as_text


class ParentAgentAdapter(LlmAdapter):
    """父代理：首步调用 subagent 工具，看到工具结果后收尾。"""

    async def stream(self, options):
        if any(isinstance(b, ToolResultBlock) for m in options.messages for b in m.content):
            yield StreamChunk(ChunkType.TEXT_DELTA, text="父代理收到子代理结果。")
            yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})
            return
        yield StreamChunk(ChunkType.BLOCK_START, index=0, block_type="tool-call")
        yield StreamChunk(ChunkType.TOOL_CALL_DELTA, index=0, tool_call_id="s1",
                          tool_call_name="subagent",
                          arguments_delta='{"prompt":"子任务：查一下天气"}')
        yield StreamChunk(ChunkType.BLOCK_END, index=0,
                          block=ToolCallBlock(id="s1", name="subagent",
                                              arguments='{"prompt":"子任务：查一下天气"}'))
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "tool-calls"})


class ChildEchoAdapter(LlmAdapter):
    """子代理的模型：回复固定文本。"""

    async def stream(self, options):
        yield StreamChunk(ChunkType.TEXT_DELTA, text="（子代理）天气晴，气温 25°C。")
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


def _setup(max_depth=3):
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    apply_subagent(ctx, {"provider": "mock", "model": "m", "max_depth": max_depth})
    ctx.llm.register_adapter(["mock"], ChildEchoAdapter())
    return ctx


async def test_subagent_tool_executes_child():
    ctx = _setup()  # mock provider 已挂 ChildEchoAdapter（子代理用它）
    # 工具已注册进 tools 服务（父代理的 schema 自动可见）
    schemas = ctx.tools.list_schemas()
    assert any(s["name"] == "subagent" for s in schemas)

    # 父代理用独立 provider（避免覆盖子代理的 adapter）
    ctx.llm.register_adapter(["parent"], ParentAgentAdapter())
    session = ctx.sessions.create()
    agent = ctx.agents.create_agent(session, AgentOptions(provider="parent", model="m"))
    await agent.run("派个子代理")

    # 父代理最终收到子代理文本
    final = None
    for ev in session.events:
        if ev.type == "assistant/message":
            final = as_text(ev.data["message"].content)
    assert final == "父代理收到子代理结果。"
    # 子代理执行过：工具结果携带子代理输出
    results = [e for e in session.events if e.type == "tool/result"]
    assert len(results) == 1
    child_text = as_text(_tool_result_message(results[0].data["message"]).content)
    assert "天气晴" in child_text and "（子代理）" in child_text


class NestedChildAdapter(LlmAdapter):
    """子代理的模型：首步发起 subagent 嵌套调用；看到「深度超限」错误后收尾。

    若深度限制失效（嵌套成功），本轮消息里不会出现「深度超限」→ 继续发起 →
    死循环至 max_steps，父侧文本会变长——据此可严格区分限制是否生效。
    """

    async def stream(self, options):
        for m in options.messages:
            for b in m.content:
                if isinstance(b, ToolResultBlock) and "深度超限" in as_text(b.content):
                    yield StreamChunk(ChunkType.TEXT_DELTA, text="子代理尝试嵌套失败")
                    yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})
                    return
        yield StreamChunk(ChunkType.BLOCK_START, index=0, block_type="tool-call")
        yield StreamChunk(ChunkType.TOOL_CALL_DELTA, index=0, tool_call_id="n1",
                          tool_call_name="subagent", arguments_delta='{"prompt":"再派"}')
        yield StreamChunk(ChunkType.BLOCK_END, index=0,
                          block=ToolCallBlock(id="n1", name="subagent",
                                              arguments='{"prompt":"再派"}'))
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "tool-calls"})


async def test_subagent_depth_limit():
    """max_depth=1：子代理内再派生被拒，父侧恰好收到一轮嵌套尝试。"""
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    apply_subagent(ctx, {"provider": "child", "model": "m", "max_depth": 1})
    ctx.llm.register_adapter(["child"], NestedChildAdapter())
    ctx.llm.register_adapter(["parent"], ParentAgentAdapter())

    session = ctx.sessions.create()
    agent = ctx.agents.create_agent(session, AgentOptions(provider="parent", model="m"))
    await agent.run("派个子代理")

    # 父 tool/result 文本 = 子代理全部 assistant 文本（恰好一轮嵌套尝试）
    child_output = None
    for ev in session.events:
        if ev.type == "tool/result":
            child_output = _tool_result_text(ev.data["message"])
    assert child_output == "子代理尝试嵌套失败"
    # turn 正常收尾（completed），没有死循环到 max-tokens
    turn_end = [e for e in session.events if e.type == "turn/end"][0]
    assert turn_end.data["reason"]["kind"] == "completed"


async def test_subagent_forbidden_at_depth_zero():
    ctx = _setup(max_depth=0)  # 0 = 禁止派生
    text, is_error = await ctx.tools.execute("subagent", '{"prompt":"hi"}')
    assert is_error is True
    assert "深度超限" in text


def _tool_result_text(msg) -> str:
    """提取工具结果消息的文本（下钻 ToolResultBlock.content）。"""
    for block in msg.content:
        if isinstance(block, ToolResultBlock):
            return as_text(block.content)
    return as_text(msg.content)


def _tool_result_message(msg):
    """取工具结果消息内容（ToolResultBlock 内部文本）。"""
    for block in msg.content:
        if isinstance(block, ToolResultBlock):
            return block
    return msg


async def test_subagent_without_provider_fails_clearly():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    apply_subagent(ctx, {})  # 未配置 provider/model
    text, is_error = await ctx.tools.execute("subagent", '{"prompt":"hi"}')
    assert is_error is True
    assert "provider/model" in text


async def main():
    await test_subagent_tool_executes_child()
    await test_subagent_depth_limit()
    await test_subagent_forbidden_at_depth_zero()
    await test_subagent_without_provider_fails_clearly()
    print("OK: 子代理工具测试通过（子代理执行、深度限制、缺配置明确报错）")


if __name__ == "__main__":
    asyncio.run(main())

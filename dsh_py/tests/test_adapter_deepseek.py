"""DeepSeek 专用适配器（llm-deepseek 翻译）测试。"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncIterator

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.adapters import deepseek as ds
from dsh_py.services.adapters.deepseek import (
    DeepSeekAdapter,
    PROVIDER,
    assert_usable_api_key,
    http_error_code,
    is_context_window_exceeded,
    is_quota_exceeded,
    provider_retry_after_ms,
    resolve_adapter_options,
    resolve_thinking,
    serialize_request,
)
from dsh_py.services.llm import (
    ChunkType,
    GenerateOptions,
    LlmError,
    StreamChunk,
)
from dsh_py.services.message import (
    Message,
    MessageSource,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    create_assistant_message,
    create_user_message,
)


# --------------------------------------------------------------------------- #
# thinking / effort 组合
# --------------------------------------------------------------------------- #
async def test_resolve_thinking_combinations():
    opts = GenerateOptions(provider="p", model="m", messages=[])
    # 默认：无 effort 无 thinking → 空（provider 默认生效）
    assert resolve_thinking(opts, {}) == {}
    # session-title → 强制 disabled
    opts.purpose = "session-title"
    assert resolve_thinking(opts, {}) == {"thinking": "disabled"}
    opts.purpose = None
    # off → thinking disabled
    opts.reasoning_effort = "off"
    assert resolve_thinking(opts, {}) == {"thinking": "disabled"}
    # high / max → enabled + effort
    opts.reasoning_effort = "high"
    assert resolve_thinking(opts, {"reasoningEffort": "high"}) == {"thinking": "enabled", "reasoningEffort": "high"}
    opts.reasoning_effort = "max"
    assert resolve_thinking(opts, {"reasoningEffort": "max"}) == {"thinking": "enabled", "reasoningEffort": "max"}
    # 非法 effort → 明确报错
    opts.reasoning_effort = "low"
    try:
        resolve_thinking(opts, {})
        raise AssertionError("非法 effort 应报错")
    except LlmError as e:
        assert e.code == "UNSUPPORTED_REASONING_EFFORT"
    # defaults.thinking=disabled 时只允许 off
    opts.reasoning_effort = "high"
    try:
        resolve_thinking(opts, {"thinking": "disabled"})
        raise AssertionError("disabled + 非 off effort 应报错")
    except LlmError as e:
        assert e.code == "UNSUPPORTED_REASONING_EFFORT"
    # 只配 thinking 不配 effort → 回落默认
    opts.reasoning_effort = None
    assert resolve_thinking(opts, {"thinking": "enabled"}) == {"thinking": "enabled"}
    print("  ✓ thinking/effort 组合解析")


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
async def test_serialize_request_thinking_fields():
    opts = GenerateOptions(
        provider="p", model="deepseek-v4-pro", messages=[create_user_message([TextBlock("hi")])],
        reasoning_effort="high",
    )
    body = serialize_request(opts)
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"
    # off 不暴露为 wire effort
    opts.reasoning_effort = "off"
    body = serialize_request(opts)
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body
    # 无配置 → 两字段都省略（provider 默认生效）
    opts.reasoning_effort = None
    body = serialize_request(opts)
    assert "thinking" not in body and "reasoning_effort" not in body
    print("  ✓ serialize_request 的 thinking/effort 字段")


async def test_serialize_reasoning_passback_only_on_tool_turns():
    # 纯文本 assistant：无 reasoning_content（省 token）
    text_only = create_assistant_message([TextBlock("你好")], MessageSource("model"))
    wire = ds.serialize_messages([text_only])
    assert wire[0]["role"] == "assistant"
    assert wire[0]["content"] == "你好"
    assert "reasoning_content" not in wire[0]
    # 工具轮 assistant：text + reasoning + tool_calls → reasoning_content 回传
    tool_turn = create_assistant_message(
        [ReasoningBlock("思考过程"), TextBlock(""), ToolCallBlock(id="c1", name="get_weather", arguments="{}")],
        MessageSource("model"),
    )
    wire = ds.serialize_messages([tool_turn])
    assert wire[0]["reasoning_content"] == "思考过程"
    assert wire[0]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert wire[0]["content"] == ""  # 绝不为 null
    # 推理-only 轮（无工具调用）：reasoning 不发给 wire
    reasoning_only = create_assistant_message([ReasoningBlock("想")], MessageSource("model"))
    wire = ds.serialize_messages([reasoning_only])
    assert "reasoning_content" not in wire[0]
    print("  ✓ reasoning_content 仅在工具轮回传")


async def test_serialize_rejects_image_blocks():
    # 在消息内容里塞一个 image 块（dict 形态；Message 是 frozen，构造时传入）
    user_msg = Message(
        id="u1", role="user",
        content=(TextBlock("看图"), {"type": "image", "image": b"..."}),
        source=MessageSource("user"),
    )
    try:
        ds.serialize_messages([user_msg])
        raise AssertionError("图片块应被拒绝")
    except LlmError as e:
        assert e.code == "UNSUPPORTED_CONTENT"
    print("  ✓ 图片块拒绝（纯文本路由）")


async def test_serialize_tool_results_split():
    user_msg = create_user_message([
        TextBlock("用户的话"),
        ToolResultBlock(tool_call_id="c1", content=(TextBlock("结果文本"),), is_error=False),
    ])
    wire = ds.serialize_messages([user_msg])
    assert wire[0] == {"role": "user", "content": "用户的话"}
    assert wire[1] == {"role": "tool", "tool_call_id": "c1", "content": "结果文本"}
    # 空工具输出兜底
    user_msg2 = create_user_message([
        ToolResultBlock(tool_call_id="c2", content=(TextBlock(""),), is_error=False),
    ])
    wire = ds.serialize_messages([user_msg2])
    assert wire[0]["role"] == "tool" and wire[0]["content"] == "(no output)"
    print("  ✓ tool-result 拆分为 role:tool 消息")


# --------------------------------------------------------------------------- #
# 错误映射
# --------------------------------------------------------------------------- #
def test_http_error_code_mapping():
    assert http_error_code(401) == "AUTH"
    assert http_error_code(403) == "AUTH"
    assert http_error_code(429) == "RATE_LIMIT"
    assert http_error_code(400, {"message": "insufficient_quota"}) == "QUOTA"
    assert http_error_code(400, {"message": "maximum context length exceeded"}) == "CONTEXT_WINDOW_EXCEEDED"
    assert http_error_code(400, {"message": "bad request"}) == "INVALID_REQUEST"
    assert http_error_code(502) == "SERVER"
    assert http_error_code(418) == "HTTP_418"
    # 分类器
    assert is_quota_exceeded("insufficient quota")
    assert is_quota_exceeded("usage limit reached")
    assert is_context_window_exceeded("max context length")
    assert is_context_window_exceeded("input is too long for this model")
    print("  ✓ HTTP 错误码映射与分类器")


def test_provider_retry_after_ms():
    assert provider_retry_after_ms("5") == 5000
    assert provider_retry_after_ms("0") is None
    assert provider_retry_after_ms(None) is None
    assert provider_retry_after_ms("不是日期") is None
    print("  ✓ retry-after 解析")


# --------------------------------------------------------------------------- #
# 适配器流式（mock transport）
# --------------------------------------------------------------------------- #
def _sse_transport(lines: list[str]):
    """构造一个返回固定 SSE 行的 transport。"""

    async def transport(url: str, body: dict, headers: dict) -> AsyncIterator[str]:
        async def gen() -> AsyncIterator[str]:
            for line in lines:
                yield line
        return gen()

    return transport


async def test_stream_reasoning_and_text():
    sse = [
        'data: {"choices":[{"delta":{"reasoning_content":"","content":null}}]}',
        'data: {"choices":[{"delta":{"reasoning_content":"先想想"}}]}',
        'data: {"choices":[{"delta":{"content":"结果来了"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":5,"prompt_tokens_details":{"cached_tokens":3}}}',
        "data: [DONE]",
    ]
    adapter = DeepSeekAdapter(
        options=lambda: {
            "baseURL": "https://api.deepseek.com",
            "apiKeyEnv": "DEEPSEEK_API_KEY",
            "defaults": {"thinking": "enabled", "reasoningEffort": "high"},
            "maxTokens": 256000,
            "defaultContextWindow": 1000000,
            "models": [],
            "streamIdleTimeoutMs": 300000,
            "retryPolicy": None,
        },
        resolve_api_key=lambda conn: "sk-test",
        transport=_sse_transport(sse),
    )
    opts = GenerateOptions(provider=PROVIDER, model="deepseek-v4-pro", messages=[])
    chunks = [c async for c in adapter.stream(opts)]
    types = [c.type for c in chunks]
    # reasoning 空首块不开块；reasoning 块先于 text 块
    assert ChunkType.BLOCK_START in types
    assert ChunkType.REASONING_DELTA in types
    assert ChunkType.TEXT_DELTA in types
    assert ChunkType.FINISH in types
    deltas = [c for c in chunks if c.type == ChunkType.REASONING_DELTA]
    assert deltas[0].reasoning == "先想想"
    text = [c for c in chunks if c.type == ChunkType.TEXT_DELTA][0]
    assert text.text == "结果来了"
    # usage：缓存命中从 inputTokens 扣除（10 - 3 = 7）
    usage = [c for c in chunks if c.type == ChunkType.USAGE][0]
    assert usage.usage["inputTokens"] == 7
    assert usage.usage["cacheReadTokens"] == 3
    print("  ✓ 流式：reasoning + text + usage（缓存扣减）")


async def test_stream_aborted_on_signal():
    adapter = DeepSeekAdapter(
        options=lambda: {
            "baseURL": "https://x", "apiKeyEnv": "K", "defaults": {},
            "maxTokens": 1, "defaultContextWindow": 1, "models": [],
            "streamIdleTimeoutMs": 300000, "retryPolicy": None,
        },
        resolve_api_key=lambda conn: "sk-test",
        transport=_sse_transport(['data: {"choices":[{"delta":{"content":"a"}}]}'] * 100),
    )
    from dsh_py.core.signal import CancelSignal

    signal = CancelSignal()
    opts = GenerateOptions(provider=PROVIDER, model="m", messages=[], signal=signal)
    async def consume():
        try:
            async for _ in adapter.stream(opts):
                signal.abort("够了")
        except LlmError as e:
            return e.code
        return None

    code = await consume()
    assert code == "ABORTED"
    print("  ✓ 调用方取消 → ABORTED")


# --------------------------------------------------------------------------- #
# 插件装配
# --------------------------------------------------------------------------- #
async def test_apply_registers_provider_and_missing_key():
    ctx = AppContext()
    load_profile(ctx, [*CORE_PROFILE, ds.apply])
    providers = [p.id for p in ctx.llm.list_providers()]
    assert PROVIDER in providers
    # 缺 key → MISSING_CREDENTIAL（适配器已就位，只差凭据）
    from dsh_py.services.llm import LlmService

    opts = GenerateOptions(provider=PROVIDER, model="deepseek-v4-pro", messages=[])
    try:
        async for _ in ctx.llm.stream(opts):
            pass
        raise AssertionError("缺 key 应抛错")
    except LlmError as e:
        assert e.code == "MISSING_CREDENTIAL"
    print("  ✓ apply 注册 deepseek-official；缺 key 报 MISSING_CREDENTIAL")


async def test_apply_resolves_key_from_env():
    os.environ["DEEPSEEK_API_KEY"] = "sk-env-key"
    try:
        ctx = AppContext()
        load_profile(ctx, [*CORE_PROFILE, ds.apply])
        opts = GenerateOptions(provider=PROVIDER, model="deepseek-v4-flash", messages=[])
        # 只走到 api key 校验（transport 未注入会走 httpx，因此用错误码判断：
        # 凭据就位后不再抛 MISSING_CREDENTIAL）
        try:
            async for _ in ctx.llm.stream(opts):
                pass
            raise AssertionError("不应成功（无真实端点）")
        except LlmError as e:
            assert e.code != "MISSING_CREDENTIAL"
            assert e.code != "INVALID_CREDENTIAL"
    finally:
        del os.environ["DEEPSEEK_API_KEY"]
    print("  ✓ env key 解析（不再 MISSING_CREDENTIAL）")


def test_resolve_adapter_options_validation():
    # thinking=disabled + effort≠off → 报错
    try:
        resolve_adapter_options({"thinking": "disabled", "reasoningEffort": "high"})
        raise AssertionError("应报错")
    except ValueError as e:
        assert "off" in str(e)
    # baseURL 分层：config > env > public
    assert resolve_adapter_options({"baseURL": "http://cfg"})["baseURL"] == "http://cfg"
    assert resolve_adapter_options({}, {"DEEPSEEK_BASE_URL": "http://env"})["baseURL"] == "http://env"
    assert resolve_adapter_options({})["baseURL"] == "https://api.deepseek.com"
    # 目录校验：重复 id 报错
    try:
        resolve_adapter_options({"models": [{"id": "a"}, {"id": "a"}]})
        raise AssertionError("重复模型应报错")
    except ValueError:
        pass
    print("  ✓ resolve_adapter_options 校验与端点分层")


def test_assert_usable_api_key():
    assert assert_usable_api_key("  sk-abc  ", "K") == "sk-abc"
    try:
        assert_usable_api_key("", "K")
        raise AssertionError("空 key 应报错")
    except LlmError as e:
        assert e.code == "INVALID_CREDENTIAL"
    try:
        assert_usable_api_key("带空格 key", "K")
        raise AssertionError("非法 key 应报错")
    except LlmError as e:
        assert e.code == "INVALID_CREDENTIAL"
    print("  ✓ assert_usable_api_key 校验")


async def main():
    print("== test_adapter_deepseek ==")
    await test_resolve_thinking_combinations()
    await test_serialize_request_thinking_fields()
    await test_serialize_reasoning_passback_only_on_tool_turns()
    await test_serialize_rejects_image_blocks()
    await test_serialize_tool_results_split()
    test_http_error_code_mapping()
    test_provider_retry_after_ms()
    await test_stream_reasoning_and_text()
    await test_stream_aborted_on_signal()
    await test_apply_registers_provider_and_missing_key()
    await test_apply_resolves_key_from_env()
    test_resolve_adapter_options_validation()
    test_assert_usable_api_key()
    print("OK: DeepSeek 专用适配器测试通过")


if __name__ == "__main__":
    asyncio.run(main())

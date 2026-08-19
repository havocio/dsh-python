"""OpenAI 兼容适配器的验证（Step 3）。

运行：python dsh_py/tests/test_adapter.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.services.adapters.openai_compatible import (
    OpenAICompatibleAdapter,
    serialize_messages,
    serialize_request,
)
from dsh_py.services.llm import ChunkType, GenerateOptions, LlmError
from dsh_py.services.message import (
    Message,
    MessageSource,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    create_user_message,
)


def mock_transport_factory(chunks):
    """构造一个 mock transport：把若干 JSON 帧包装成 SSE 行。"""
    async def transport(url, body, headers):
        async def gen():
            for c in chunks:
                yield f"data: {json.dumps(c, ensure_ascii=False)}"
            yield "data: [DONE]"
        return gen()
    return transport


def test_serialize_user_text_and_tool_result():
    # user 文本 + 工具结果应分别成为 role:'user' 与 role:'tool'
    user = create_user_message([TextBlock("查天气")])
    tool_result = create_user_message(
        [ToolResultBlock(tool_call_id="c1", content=(TextBlock("晴"),), is_error=False)],
        source=MessageSource("tool"),
    )
    wire = serialize_messages([user, tool_result])
    assert wire[0] == {"role": "user", "content": "查天气"}
    assert wire[1] == {"role": "tool", "tool_call_id": "c1", "content": "晴"}


def test_serialize_assistant_tool_calls():
    assistant = Message(
        id="a1", role="assistant",
        content=(ToolCallBlock(id="c1", name="get_weather", arguments='{"city":"北京"}'),),
        source=MessageSource("model"),
    )
    wire = serialize_messages([assistant])
    assert wire[0]["role"] == "assistant"
    assert wire[0]["tool_calls"][0]["function"]["name"] == "get_weather"


def test_serialize_request_always_streaming():
    opts = GenerateOptions(provider="openai", model="gpt-4o", messages=[create_user_message([TextBlock("hi")])])
    body = serialize_request(opts)
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["model"] == "gpt-4o"


async def test_stream_text_and_tool_call():
    chunks = [
        {"choices": [{"delta": {"content": "你好"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "get_weather", "arguments": '{"city":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"北京"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
    ]
    adapter = OpenAICompatibleAdapter(
        resolve_endpoint=lambda p: {"baseURL": "http://x/v1", "allowEmptyKey": True},
        resolve_api_key=lambda p: "",
        transport=mock_transport_factory(chunks),
    )
    opts = GenerateOptions(provider="openai", model="gpt-4o", messages=[create_user_message([TextBlock("hi")])])
    out = [c async for c in adapter.stream(opts)]

    types = [c.type for c in out]
    assert ChunkType.TEXT_DELTA in types
    text = "".join(c.text for c in out if c.type == ChunkType.TEXT_DELTA)
    assert text == "你好"
    # 工具调用的增量应拼出完整参数
    tc_deltas = [c for c in out if c.type == ChunkType.TOOL_CALL_DELTA]
    assert any(c.tool_call_name == "get_weather" for c in tc_deltas)
    args = "".join(c.arguments_delta for c in tc_deltas)
    assert args == '{"city":"北京"}'
    # 结束原因与用量
    finish = [c for c in out if c.type == ChunkType.FINISH][0]
    assert finish.finish == {"kind": "tool-calls"}
    usage = [c for c in out if c.type == ChunkType.USAGE][0]
    assert usage.usage["outputTokens"] == 5


async def test_stream_truncated_without_done_raises():
    # 流在 [DONE] 之前结束应抛 STREAM_CLOSED
    async def bad_transport(url, body, headers):
        async def gen():
            yield "data: " + json.dumps({"choices": [{"delta": {"content": "x"}}]})
            # 不再发送 [DONE]
        return gen()
    adapter = OpenAICompatibleAdapter(
        resolve_endpoint=lambda p: {"baseURL": "http://x/v1", "allowEmptyKey": True},
        resolve_api_key=lambda p: "",
        transport=bad_transport,
    )
    opts = GenerateOptions(provider="openai", model="m", messages=[])
    try:
        async for _ in adapter.stream(opts):
            pass
        raise AssertionError("expected LlmError STREAM_CLOSED")
    except LlmError as e:
        assert e.code == "STREAM_CLOSED"


def main():
    test_serialize_user_text_and_tool_result()
    test_serialize_assistant_tool_calls()
    test_serialize_request_always_streaming()
    asyncio.run(test_stream_text_and_tool_call())
    asyncio.run(test_stream_truncated_without_done_raises())
    print("OK: OpenAI 兼容适配器测试通过（序列化、SSE 解析、translate、工具调用、截断保护）")


if __name__ == "__main__":
    main()

"""LLM 接口层的验证（Step 2）。

运行：python dsh_py/tests/test_llm.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.services.llm import (
    ChunkType,
    GenerateOptions,
    LlmAdapter,
    LlmService,
    StreamChunk,
)


class MockAdapter(LlmAdapter):
    """测试用的内存适配器，固定产出一段文本再结束。"""
    def __init__(self, text="hi"):
        self.text = text

    async def stream(self, options):
        yield StreamChunk(ChunkType.TEXT_DELTA, text=self.text)
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "completed"})


async def test_stream_returns_expected_chunks():
    ctx = AppContext()
    llm = LlmService(ctx)
    llm.register_adapter(["mock"], MockAdapter("hello"))

    options = GenerateOptions(provider="mock", model="m", messages=[{"role": "user", "content": "x"}])
    chunks = [c async for c in llm.stream(options)]

    assert chunks[0].type == ChunkType.TEXT_DELTA and chunks[0].text == "hello"
    assert chunks[-1].type == ChunkType.FINISH and chunks[-1].finish == {"kind": "completed"}


async def test_duplicate_provider_raises():
    ctx = AppContext()
    llm = LlmService(ctx)
    llm.register_adapter(["mock"], MockAdapter())
    try:
        llm.register_adapter(["mock"], MockAdapter())
    except RuntimeError as e:
        assert "mock" in str(e)
    else:  # pragma: no cover
        raise AssertionError("重复注册供应商时本应报错")


async def test_missing_provider_raises():
    ctx = AppContext()
    llm = LlmService(ctx)
    options = GenerateOptions(provider="nope", model="m", messages=[])
    try:
        async for _ in llm.stream(options):
            pass
    except RuntimeError as e:
        assert "nope" in str(e)
    else:  # pragma: no cover
        raise AssertionError("请求未注册供应商时本应报错")


async def test_llm_stream_waterfall_middleware():
    ctx = AppContext()
    llm = LlmService(ctx)
    llm.register_adapter(["mock"], MockAdapter("via-mw"))
    seen = []

    async def middleware(options, nxt):
        # 中间件通过迭代 next() 包裹原始流
        async for chunk in nxt():
            seen.append(chunk.type)
            yield chunk

    ctx.on("llm/stream", middleware)
    options = GenerateOptions(provider="mock", model="m", messages=[])
    chunks = [c async for c in llm.stream(options)]
    assert any(c.type == ChunkType.TEXT_DELTA and c.text == "via-mw" for c in chunks)
    assert ChunkType.TEXT_DELTA in seen and ChunkType.FINISH in seen


def main():
    asyncio.run(test_stream_returns_expected_chunks())
    asyncio.run(test_duplicate_provider_raises())
    asyncio.run(test_missing_provider_raises())
    asyncio.run(test_llm_stream_waterfall_middleware())
    print("OK: LLM 接口层测试通过（适配器注册、流式调用、瀑布流中间件）")


if __name__ == "__main__":
    main()

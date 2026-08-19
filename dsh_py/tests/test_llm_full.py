"""LLM 完整版（call-config / retry-policy / api-key 校验）的验证（第 2 层 LLM）。

运行：python dsh_py/tests/test_llm_full.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.agent import AgentOptions
from dsh_py.services.call_config import (
    LlmCallConfig,
    call_config_equals,
    call_config_from_options,
    merge_call_config,
)
from dsh_py.services.llm import (
    ChunkType,
    GenerateOptions,
    LlmAdapter,
    LlmError,
    LlmService,
    StreamChunk,
    normalize_api_key,
)
from dsh_py.services.message import TextBlock, create_user_message
from dsh_py.services.retry_policy import (
    DEFAULT_RETRYABLE_CODES,
    RetryPolicyError,
    resolve_retry_policy,
)


# --------------------------------------------------------------------------- #
# normalize_api_key（对标 dsh 的 normalizeApiKey）
# --------------------------------------------------------------------------- #
def test_normalize_api_key():
    assert normalize_api_key("  sk-abc  ") == ("ok", "sk-abc")   # trim
    assert normalize_api_key("   ") == ("empty", "")              # 空
    assert normalize_api_key("") == ("empty", "")
    assert normalize_api_key("带中文的key") == ("illegal", "")    # 非法字符
    assert normalize_api_key("sk-with space") == ("illegal", "")  # 空格非法


# --------------------------------------------------------------------------- #
# retry-policy（对标 dsh 的 retry-policy.ts）
# --------------------------------------------------------------------------- #
def test_retry_policy_defaults_and_custom():
    policy = resolve_retry_policy(None)
    assert policy.mode == "normal"
    assert policy.max_retries == 2
    assert "RATE_LIMIT" in policy.retryable_codes

    custom = resolve_retry_policy({
        "mode": "normal",
        "maxRetries": 5,
        "retryableCodes": ["SERVER", "TIMEOUT"],
        "backoff": {"initialDelayMs": 100, "maxDelayMs": 1000, "jitterRatio": 0.2},
    })
    assert custom.max_retries == 5
    assert custom.retryable_codes == ("SERVER", "TIMEOUT")
    assert custom.initial_delay_ms == 100

    always = resolve_retry_policy({"mode": "always"})
    assert always.mode == "always"
    assert always.should_retry("WHATEVER", 99) is True


def test_retry_policy_invalid_config_fails_loud():
    for bad in (
        {"mode": "sometimes"},                          # 非法 mode
        {"backoff": {"initialDelayMs": -1}},            # 非法退避
        {"retryableCodes": []},                         # 空重试码
        {"unknownField": 1},                            # 未知字段
    ):
        try:
            resolve_retry_policy(bad)
        except RetryPolicyError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"非法配置应报错：{bad}")


def test_retry_policy_should_retry():
    policy = resolve_retry_policy(None)  # normal, maxRetries=2
    assert policy.should_retry("RATE_LIMIT", 0) is True
    assert policy.should_retry("RATE_LIMIT", 2) is False    # 达上限
    assert policy.should_retry("AUTH", 0) is False          # 非可重试码
    assert policy.delay_for(1) > 0                          # 退避延迟为正


# --------------------------------------------------------------------------- #
# call-config（对标 dsh 的 call-config.ts）
# --------------------------------------------------------------------------- #
def test_merge_call_config_priority():
    merged = merge_call_config(
        {"provider": "p", "model": "default-model", "max_tokens": 100},   # provider 默认
        {"provider": "p", "model": "header-model"},                       # header（session）
        {"provider": "p", "model": "request-model", "temperature": 0.5},  # 本次请求
    )
    # 优先级：request > header > provider 默认；缺失字段逐层填充
    assert merged.model == "request-model"
    assert merged.temperature == 0.5
    assert merged.max_tokens == 100
    assert call_config_equals(merged, LlmCallConfig(
        provider="p", model="request-model", temperature=0.5, max_tokens=100))


def test_call_config_from_options():
    opts = GenerateOptions(provider="p", model="m", messages=[], max_tokens=64, stop=["END"])
    out = call_config_from_options(opts)
    assert out == {"provider": "p", "model": "m", "max_tokens": 64, "stop": ["END"]}


# --------------------------------------------------------------------------- #
# 重试集成（LlmService.stream 按策略自动重试）
# --------------------------------------------------------------------------- #
class FlakyAdapter(LlmAdapter):
    """第一次调用抛 RATE_LIMIT，之后成功。"""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, options):
        if False:  # 保证是异步生成器（契约：stream 为 lazy 流）
            yield StreamChunk(ChunkType.TEXT_DELTA, text="never")
        self.calls += 1
        if self.calls == 1:
            raise LlmError("rate limited", "RATE_LIMIT")
        yield StreamChunk(ChunkType.TEXT_DELTA, text="recovered")
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


async def test_stream_retries_transient_error():
    ctx = AppContext()
    llm = LlmService(ctx)
    adapter = FlakyAdapter()
    llm.register_adapter(
        ["mock"], adapter,
        retry=resolve_retry_policy({"mode": "normal", "maxRetries": 3,
                                    "backoff": {"initialDelayMs": 1, "maxDelayMs": 5}}),
    )
    opts = GenerateOptions(provider="mock", model="m", messages=[])
    chunks = [c async for c in llm.stream(opts)]
    assert adapter.calls == 2                    # 失败 1 次 + 重试 1 次
    assert any(c.type == ChunkType.TEXT_DELTA and c.text == "recovered" for c in chunks)


async def test_stream_gives_up_after_max_retries():
    ctx = AppContext()
    llm = LlmService(ctx)

    class AlwaysFail(LlmAdapter):
        def __init__(self):
            self.calls = 0

        async def stream(self, options):
            if False:  # 保证是异步生成器（契约：stream 为 lazy 流）
                yield StreamChunk(ChunkType.TEXT_DELTA, text="never")
            self.calls += 1
            raise LlmError("boom", "SERVER")

    adapter = AlwaysFail()
    llm.register_adapter(["mock"], adapter, retry=resolve_retry_policy({
        "mode": "normal", "maxRetries": 2, "backoff": {"initialDelayMs": 1, "maxDelayMs": 5}}))
    opts = GenerateOptions(provider="mock", model="m", messages=[])
    try:
        async for _ in llm.stream(opts):
            pass
    except LlmError as e:
        assert e.code == "SERVER"
        assert adapter.calls == 3                # 首次 + 2 次重试
    else:  # pragma: no cover
        raise AssertionError("超过重试上限后应抛出原始错误")


async def test_stream_no_retry_without_policy():
    ctx = AppContext()
    llm = LlmService(ctx)

    class FailOnce(LlmAdapter):
        def __init__(self):
            self.calls = 0

        async def stream(self, options):
            if False:  # 保证是异步生成器（契约：stream 为 lazy 流）
                yield StreamChunk(ChunkType.TEXT_DELTA, text="never")
            self.calls += 1
            raise LlmError("rate limited", "RATE_LIMIT")

    adapter = FailOnce()
    llm.register_adapter(["mock"], adapter)      # 未配置 retry → 不重试
    try:
        async for _ in llm.stream(GenerateOptions(provider="mock", model="m", messages=[])):
            pass
    except LlmError:
        assert adapter.calls == 1
    else:  # pragma: no cover
        raise AssertionError("应抛出错误")


# --------------------------------------------------------------------------- #
# header.request 记录 + api-key 校验
# --------------------------------------------------------------------------- #
async def test_step_records_call_config_in_header():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)

    class Echo(LlmAdapter):
        async def stream(self, options):
            yield StreamChunk(ChunkType.TEXT_DELTA, text="hi")
            yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})

    ctx.llm.register_adapter(["mock"], Echo())
    session = ctx.sessions.create()
    agent = ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))
    await agent.run("hello")
    # epoch 级配置已记录到 header（请求从 header 构建）
    assert session.header.request is not None
    assert session.header.request["provider"] == "mock"
    assert session.header.request["model"] == "m"


async def test_adapter_rejects_illegal_api_key():
    from dsh_py.services.adapters.openai_compatible import OpenAICompatibleAdapter

    adapter = OpenAICompatibleAdapter(
        resolve_endpoint=lambda p: {"baseURL": "http://x/v1", "allowEmptyKey": False},
        resolve_api_key=lambda p: "非法 key",
        transport=lambda url, body, headers: _empty_sse(),
    )
    opts = GenerateOptions(provider="p", model="m", messages=[])
    try:
        await _collect(adapter.stream(opts))
    except LlmError as e:
        assert e.code == "ILLEGAL_API_KEY"
    else:  # pragma: no cover
        raise AssertionError("非法 API key 应被本地拒绝")


async def _empty_sse():
    async def gen():
        yield "data: [DONE]"
    return gen()


async def _collect(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


async def main():
    test_normalize_api_key()
    test_retry_policy_defaults_and_custom()
    test_retry_policy_invalid_config_fails_loud()
    test_retry_policy_should_retry()
    test_merge_call_config_priority()
    test_call_config_from_options()
    await test_stream_retries_transient_error()
    await test_stream_gives_up_after_max_retries()
    await test_stream_no_retry_without_policy()
    await test_step_records_call_config_in_header()
    await test_adapter_rejects_illegal_api_key()
    print("OK: LLM 完整版测试通过（call-config 三层合并、retry 重试、api-key 校验、header 记录）")


if __name__ == "__main__":
    asyncio.run(main())

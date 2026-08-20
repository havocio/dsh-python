"""OpenAI 兼容适配器（对标 dsh 的 ``llm-deepseek``）。

一个适配器对接任意 OpenAI 兼容的 ``/chat/completions`` 端点。流程：
harness 消息 → ``serialize`` 成 wire 格式 → 通过可插拔 transport 发送并解析
SSE → ``translate`` 把 OpenAI chunk 翻回 harness 的 :class:`StreamChunk`。

默认在插件 ``apply`` 里注册 7 个休眠态厂商（OpenAI / 通义千问 / 智谱 GLM /
Kimi / DeepSeek 兼容 / Ollama / vLLM）；只有对应的环境变量存在才真正发请求。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, AsyncIterable, AsyncIterator, Awaitable, Callable, Optional

from dsh_py.services.llm import ChunkType, GenerateOptions, LlmAdapter, LlmError, LlmProviderInfo, StreamChunk, normalize_api_key
from dsh_py.services.message import (
    Message,
    MessageSource,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    as_text,
)

# SSE 终止哨兵：OpenAI / DeepSeek 在最后一帧之后发送
DONE = "[DONE]"


# --------------------------------------------------------------------------- #
# 序列化：harness 消息 → OpenAI wire 格式
# --------------------------------------------------------------------------- #
def flatten_text(blocks: tuple) -> str:
    """拼接消息内容里的全部文本块。"""
    return as_text(blocks)


def serialize_messages(messages: list[Any]) -> list[dict]:
    """把 harness 消息列表翻译成 OpenAI 的 wire 消息列表。

    - user 文本合并为 ``role:'user'``；tool-result 块拆成独立的 ``role:'tool'`` 消息。
    - assistant 的文本进入 ``content``，工具调用进入 ``tool_calls``，推理进入
      ``reasoning_content``（仅在存在工具调用时回传，符合 thinking 模式约定）。
    - 空文本且非工具结果时仍发送 ``content:''``（绝不为 null）。
    """
    wire: list[dict] = []
    for msg in messages:
        role = msg.role if isinstance(msg, Message) else msg.get("role")
        content = msg.content if isinstance(msg, Message) else msg.get("content", [])
        if isinstance(content, str):
            content = [TextBlock(content)]
        if role == "system":
            wire.append({"role": "system", "content": flatten_text(content)})
            continue
        if role == "assistant":
            text = flatten_text(content)
            reasoning = "".join(b.text for b in content if isinstance(b, ReasoningBlock))
            tool_calls = [
                {"id": b.id, "type": "function", "function": {"name": b.name, "arguments": b.arguments}}
                for b in content
                if isinstance(b, ToolCallBlock)
            ]
            wm: dict[str, Any] = {"role": "assistant", "content": text}
            if tool_calls and reasoning:
                wm["reasoning_content"] = reasoning
            if tool_calls:
                wm["tool_calls"] = tool_calls
            wire.append(wm)
            continue
        # user（工具结果也走 user 词汇，但 DeepSeek 要求它们作为 role:'tool'）
        tool_results = [b for b in content if isinstance(b, ToolResultBlock)]
        text = flatten_text(content)
        if text or not tool_results:
            wire.append({"role": "user", "content": text})
        for tr in tool_results:
            wire.append({
                "role": "tool",
                "tool_call_id": tr.tool_call_id,
                "content": flatten_text(tr.content) or "(no output)",
            })
    return wire


def serialize_request(options: GenerateOptions) -> dict:
    """构造完整的 wire 请求体；始终流式（``stream:true`` + ``include_usage``）。"""
    messages: list[dict] = []
    if options.system:
        messages.append({"role": "system", "content": options.system})
    messages.extend(serialize_messages(options.messages))

    body: dict[str, Any] = {
        "model": options.model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if options.tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {}),
                },
            }
            for t in options.tools
        ]
    if options.temperature is not None:
        body["temperature"] = options.temperature
    if options.max_tokens is not None:
        body["max_tokens"] = options.max_tokens
    if options.stop:
        body["stop"] = options.stop
    return body


# --------------------------------------------------------------------------- #
# 翻译：OpenAI chunk → harness StreamChunk（对标 dsh 的 translate.ts）
# --------------------------------------------------------------------------- #
def map_finish_reason(reason: str) -> dict:
    """把 wire 的 finish_reason 映射到 harness 的结束原因。"""
    mapping = {
        "stop": {"kind": "stop"},
        "tool_calls": {"kind": "tool-calls"},
        "length": {"kind": "max-tokens"},
    }
    if reason in mapping:
        return mapping[reason]
    # content_filter、未知值等归为错误
    return {"kind": "error", "failure": {"message": f"model stopped: {reason}", "code": reason.upper()}}


def map_usage(usage: dict) -> dict:
    """映射 wire 用量；缓存命中从 inputTokens 中扣除，保持不相交计数。"""
    cache_read = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cache_read is None:
        cache_read = usage.get("prompt_cache_hit_tokens")
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    result: dict[str, Any] = {
        "inputTokens": usage.get("prompt_tokens", 0) - (cache_read or 0),
        "outputTokens": usage.get("completion_tokens", 0),
    }
    if cache_read is not None:
        result["cacheReadTokens"] = cache_read
    if reasoning is not None:
        result["reasoningTokens"] = reasoning
    return result


async def translate(payloads: AsyncIterable[str]) -> AsyncIterator[StreamChunk]:
    """消费 SSE 数据载荷（以 ``[DONE]`` 结尾），产出 :class:`StreamChunk`。

    维护每个内容块（文本/推理/工具调用）的状态；block-end、usage、finish
    都延迟到 ``[DONE]`` 哨兵后再下发（与 dsh 一致）。空响应（stop 且无任何块）
    映射为 ``EMPTY_RESPONSE`` 错误结束。
    """
    next_index = 0
    text_block: Optional[dict] = None
    reasoning_block: Optional[dict] = None
    tool_blocks: dict[int, dict] = {}
    order: list[tuple[str, int]] = []
    pending_finish: Optional[dict] = None
    pending_usage: Optional[dict] = None

    def open(kind: str) -> int:
        nonlocal next_index
        idx = next_index
        next_index += 1
        order.append((kind, idx))
        return idx

    async for payload in payloads:
        if payload == DONE:
            # 逐个关闭已打开的块（保持开启顺序）
            for kind, idx in order:
                if kind == "text":
                    yield StreamChunk(ChunkType.BLOCK_END, index=idx, block=TextBlock(text_block["text"]))
                elif kind == "reasoning":
                    yield StreamChunk(ChunkType.BLOCK_END, index=idx, block=ReasoningBlock(text=reasoning_block["text"]))
                else:
                    tc = tool_blocks[idx]
                    yield StreamChunk(
                        ChunkType.BLOCK_END,
                        index=idx,
                        block=ToolCallBlock(id=tc["id"], name=tc["name"] or "", arguments=tc["text"]),
                    )
            if pending_usage:
                yield StreamChunk(ChunkType.USAGE, usage=pending_usage)
            reason = pending_finish or {"kind": "stop"}
            if reason.get("kind") == "stop" and len(order) == 0:
                reason = {
                    "kind": "error",
                    "failure": {"message": "model returned a completed response with no content", "code": "EMPTY_RESPONSE"},
                }
            yield StreamChunk(ChunkType.FINISH, finish=reason)
            return

        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            raise LlmError(f"malformed SSE payload: {payload[:120]}", "MALFORMED_RESPONSE")

        for choice in chunk.get("choices", []) or []:
            delta = choice.get("delta", {}) or {}

            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                if reasoning_block is None:
                    idx = open("reasoning")
                    reasoning_block = {"index": idx, "text": ""}
                    yield StreamChunk(ChunkType.BLOCK_START, index=idx, block_type="reasoning")
                reasoning_block["text"] += reasoning
                yield StreamChunk(ChunkType.REASONING_DELTA, index=reasoning_block["index"], reasoning=reasoning)

            content = delta.get("content")
            if isinstance(content, str) and content:
                if text_block is None:
                    idx = open("text")
                    text_block = {"index": idx, "text": ""}
                    yield StreamChunk(ChunkType.BLOCK_START, index=idx, block_type="text")
                text_block["text"] += content
                yield StreamChunk(ChunkType.TEXT_DELTA, index=text_block["index"], text=content)

            for call in delta.get("tool_calls", []) or []:
                idx = call.get("index", 0)
                if idx not in tool_blocks:
                    tool_blocks[idx] = {"id": "", "name": None, "text": ""}
                    order.append(("tool", idx))
                    yield StreamChunk(ChunkType.BLOCK_START, index=idx, block_type="tool-call")
                tc = tool_blocks[idx]
                if call.get("id") is not None:
                    tc["id"] = call["id"]
                fn = call.get("function", {}) or {}
                if fn.get("name") is not None:
                    tc["name"] = fn["name"]
                fragment = fn.get("arguments", "") or ""
                tc["text"] += fragment
                yield StreamChunk(
                    ChunkType.TOOL_CALL_DELTA,
                    index=idx,
                    tool_call_id=tc["id"],
                    tool_call_name=tc["name"],
                    arguments_delta=fragment,
                )

            fr = choice.get("finish_reason")
            if isinstance(fr, str):
                pending_finish = map_finish_reason(fr)

        if chunk.get("usage"):
            pending_usage = map_usage(chunk["usage"])

    # 流在 [DONE] 之前结束，视为截断（不可信）
    raise LlmError("SSE 流在未收到 [DONE] 时结束", "STREAM_CLOSED")


# --------------------------------------------------------------------------- #
# 适配器
# --------------------------------------------------------------------------- #
@dataclass
class ProviderConfig:
    """一个 OpenAI 兼容供应商的连接信息。"""
    provider: str
    display_name: str
    base_url: str
    api_key_env: str
    allow_empty_key: bool = False
    context_window: Optional[int] = None
    max_tokens: Optional[int] = None


# transport：接收 (url, json_body, headers)，产出 SSE 原始行（异步可迭代）
Transport = Callable[[str, dict, dict], Awaitable[AsyncIterable[str]]]


class OpenAICompatibleAdapter(LlmAdapter):
    """对接任意 OpenAI 兼容端点的适配器。

    连接信息（端点、api key 解析）通过两个 thunk 注入，使注册插件拥有校验、
    分层与凭据策略；transport 可插拔，默认用 httpx（仅此依赖外部包）。
    """

    def __init__(
        self,
        resolve_endpoint: Callable[[str], dict],
        resolve_api_key: Callable[[str], Awaitable[str]],
        transport: Optional[Transport] = None,
    ) -> None:
        self._resolve_endpoint = resolve_endpoint
        self._resolve_api_key = resolve_api_key
        self._transport = transport

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        endpoint = self._resolve_endpoint(options.provider)
        api_key = self._resolve_api_key(options.provider)
        if asyncio.iscoroutine(api_key):
            api_key = await api_key
        # API key 校验（对齐 dsh 的 normalizeApiKey）：空 → MISSING_CREDENTIAL，
        # 非法字符 → ILLEGAL_API_KEY（本地解释性拒绝，而非远端 401）
        verdict, normalized = normalize_api_key(api_key or "")
        if verdict == "empty":
            if endpoint.get("allowEmptyKey"):
                normalized = ""
            else:
                raise LlmError(f"供应商 {options.provider!r} 缺少 API key", "MISSING_CREDENTIAL")
        elif verdict == "illegal":
            raise LlmError("API key 含非法字符（仅允许可打印 ASCII）", "ILLEGAL_API_KEY")
        base_url = endpoint["baseURL"]
        body = serialize_request(options)
        headers = {
            "authorization": f"Bearer {normalized or 'not-needed'}",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }

        async def sse_lines() -> AsyncIterator[str]:
            if self._transport is not None:
                async for line in await self._transport(f"{base_url}/chat/completions", body, headers):
                    yield line
            else:
                async for line in self._httpx_transport(f"{base_url}/chat/completions", body, headers):
                    yield line

        async def payloads() -> AsyncIterator[str]:
            async for line in sse_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                yield line[len("data:"):].strip()

        async for chunk in translate(payloads()):
            yield chunk

    async def _httpx_transport(self, url: str, json_body: dict, headers: dict) -> AsyncIterator[str]:
        """默认 transport：用 httpx 建立流式 POST 并逐行产出。"""
        import httpx  # 延迟导入，使框架本体不依赖 httpx

        async with httpx.AsyncClient() as client:
            async with client.stream("POST", url, json=json_body, headers=headers, timeout=300.0) as resp:
                if resp.status_code >= 400:
                    err = await resp.aread()
                    raise LlmError(f"HTTP {resp.status_code}: {err[:200]}", f"HTTP_{resp.status_code}")
                async for line in resp.aiter_lines():
                    yield line


# --------------------------------------------------------------------------- #
# 插件：默认注册 7 个厂商（休眠态）
# --------------------------------------------------------------------------- #
DEFAULT_PROVIDERS: list[ProviderConfig] = [
    ProviderConfig("openai", "OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    ProviderConfig("qwen", "通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
    ProviderConfig("zhipu", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY"),
    ProviderConfig("moonshot", "Kimi", "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY"),
    ProviderConfig("deepseek", "DeepSeek 兼容", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    ProviderConfig("ollama", "Ollama", os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"), "OLLAMA_API_KEY", allow_empty_key=True),
    ProviderConfig("vllm", "vLLM", os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1"), "VLLM_API_KEY", allow_empty_key=True),
]


def merge_providers(defaults: list[ProviderConfig], overrides: list[dict]) -> list[ProviderConfig]:
    """以 provider 为键，用覆盖项合并默认厂商表。"""
    table: dict[str, ProviderConfig] = {p.provider: p for p in defaults}
    for o in overrides:
        table[o["provider"]] = ProviderConfig(
            provider=o["provider"],
            display_name=o.get("displayName", o["provider"]),
            base_url=o["baseURL"],
            api_key_env=o.get("apiKeyEnv", ""),
            allow_empty_key=o.get("allowEmptyKey", False),
            context_window=o.get("contextWindow"),
            max_tokens=o.get("maxTokens"),
        )
    return list(table.values())


def apply(ctx: Any, config: Optional[dict] = None) -> None:
    """注册 OpenAI 兼容适配器，默认带上 7 个厂商；``config.providers`` 可合并覆盖。"""
    config = config or {}
    providers = merge_providers(DEFAULT_PROVIDERS, config.get("providers", []) or [])

    def resolve_endpoint(provider: str) -> dict:
        for p in providers:
            if p.provider == provider:
                return {"baseURL": p.base_url, "allowEmptyKey": p.allow_empty_key}
        raise LlmError(f"未配置供应商 {provider!r}", "NO_ADAPTER")

    async def resolve_api_key(provider: str) -> str:
        for p in providers:
            if p.provider == provider:
                # 1. 统一配置文件优先：llm.api_keys.<provider>，再兜底 llm.api_key
                if ctx.has_service("appConfig"):
                    per_provider = ctx.appConfig.get("llm.api_keys")
                    if isinstance(per_provider, dict) and per_provider.get(provider):
                        return per_provider[provider]
                    fallback = ctx.appConfig.get("llm.api_key")
                    if fallback:
                        return fallback
                key = os.environ.get(p.api_key_env) if p.api_key_env else None
                if not key and not p.allow_empty_key:
                    raise LlmError(
                        f"缺少 API Key：请在配置文件的 llm.api_keys.{provider} 或环境变量 "
                        f"{p.api_key_env!r} 中提供",
                        "MISSING_CREDENTIAL",
                    )
                return key or ""
        raise LlmError(f"未配置供应商 {provider!r}", "NO_ADAPTER")

    adapter = OpenAICompatibleAdapter(resolve_endpoint, resolve_api_key, transport=config.get("transport"))
    ctx.llm.register_adapter([p.provider for p in providers], adapter)


name = "llm-openai-compatible"

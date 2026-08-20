"""DeepSeek 官方专用适配器（对标 dsh 的 ``llm-deepseek``）。

与通用 OpenAI 兼容适配器（``openai_compatible.py``）并存、路由不同：本插件
只注册 ``deepseek-official`` 一个 provider，内置 V4 模型目录，并实现 DeepSeek
特有的协议细节：

- **thinking / reasoningEffort 双开关**：`thinking: enabled|disabled` ×
  `reasoningEffort: off|high|max` 解析成合法 wire 组合（``session-title`` 用途
  强制 disabled；``off`` 映射为 ``thinking: disabled``；非法组合启动期报
  ``UNSUPPORTED_REASONING_EFFORT``）；
- **thinking-mode passback**：assistant 推理只在**工具调用轮**回放为
  ``reasoning_content``（官方 thinking_mode 指南要求），普通轮省略省 token；
- **纯文本路由**：含图片内容块显式拒绝（``UNSUPPORTED_CONTENT``）；
- **连接快照**：每次调用 resolve 一次连接事实（端点 + api key 同源冻结），
  配置变更对下一次请求立即可见；api key 经 credentials seam 解析（无 seam
  时环境变量兜底），缺失抛 ``MISSING_CREDENTIAL``、非法字符抛
  ``INVALID_CREDENTIAL``；
- **错误归一**：HTTP 状态映射稳定错误码（AUTH / QUOTA / RATE_LIMIT /
  INVALID_REQUEST / CONTEXT_WINDOW_EXCEEDED / SERVER / HTTP_x），流空闲超时
  抛 ``TIMEOUT``、调用方取消抛 ``ABORTED``、传输失败抛 ``TRANSPORT``；
- **settings 集成**：注册 ``llm-deepseek`` 设置命名空间，改配置即时生效
  （重试策略变化时原地重注册路由）。

translate / mapUsage 与 OpenAI 兼容版共用（wire 协议同源）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.services.adapters.openai_compatible import (
    DONE,
    map_finish_reason,
    map_usage,
    translate,
)
from dsh_py.services.credentials import credential_ref
from dsh_py.services.llm import (
    GenerateOptions,
    LlmAdapter,
    LlmError,
    LlmProviderInfo,
    StreamChunk,
    normalize_api_key,
)
from dsh_py.services.message import ReasoningBlock, TextBlock, ToolCallBlock, ToolResultBlock
from dsh_py.services.retry_policy import resolve_retry_policy
from dsh_py.services.settings import install_settings_section, settings_namespace

# --------------------------------------------------------------------------- #
# 常量（对齐 dsh-llm-deepseek 的 adapter.ts / index.ts）
# --------------------------------------------------------------------------- #
PUBLIC_BASE_URL = "https://api.deepseek.com"
BASE_URL_ENV = "DEEPSEEK_BASE_URL"          # 仅受信环境层可提供端点
DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"
PROVIDER = "deepseek-official"               # 本插件独占的 provider 路由
DEFAULT_CONTEXT_WINDOW = 1_000_000
DEFAULT_MAX_TOKENS = 256_000
DEFAULT_STREAM_IDLE_TIMEOUT_MS = 300_000     # 单次读空闲上限（默认 5 分钟）
STREAM_IDLE_TIMEOUT_CODE = "LLM_STREAM_IDLE_TIMEOUT"
MAX_TIMER_DELAY_MS = 2**31 - 1

# 稳定错误码（对齐 dsh-llm 的 error.ts）
CONTEXT_WINDOW_EXCEEDED_CODE = "CONTEXT_WINDOW_EXCEEDED"
QUOTA_EXCEEDED_CODE = "QUOTA"
INVALID_CREDENTIAL_CODE = "INVALID_CREDENTIAL"

# 推理强度档位（wire 层 reasoning_effort 只支持 high/max；off 走 thinking:disabled）
REASONING_EFFORTS = ("off", "high", "max")

# 分类正则（对齐 dsh-llm 的 error.ts 启发式，简化保留核心模式）
_CTX_WINDOW_PATTERNS = (
    re.compile(r"\b(?:maximum|max)(?:\s+(?:allowed|supported))?\s+context\s+(?:length|window)\b", re.I),
    re.compile(r"\b(?:input|prompt|request)\s+(?:is\s+)?too\s+(?:long|large)\s+for\s+(?:this|the)\s+model\b", re.I),
    re.compile(r"\b(?:context|token)[\s_-]+(?:length|window)[\s_-]+(?:exceeded|too[\s_-]+long)\b", re.I),
)
_QUOTA_PATTERNS = (
    re.compile(r"\binsufficient[\s_-]+(?:quota|balance|credits?)\b", re.I),
    re.compile(r"\b(?:quota|usage[\s_-]+limit)[\s_-]+(?:exceeded|exhausted|reached)\b", re.I),
    re.compile(r"\b(?:balance|credits?)[\s_-]+(?:exhausted|depleted)\b", re.I),
    re.compile(r"\bout[\s_-]+of[\s_-]+(?:credits?|budget)\b", re.I),
)


def is_context_window_exceeded(detail: str) -> bool:
    """判断 provider 报错文本是否标识超出模型上下文窗口。"""
    return any(p.search(detail) for p in _CTX_WINDOW_PATTERNS)


def is_quota_exceeded(detail: str) -> bool:
    """判断 provider 报错文本是否标识配额耗尽（而非瞬时限流）。"""
    return any(p.search(detail) for p in _QUOTA_PATTERNS)


def assert_usable_api_key(raw: str, ref: str) -> str:
    """校验并返回可用 api key（对齐 dsh 的 assertUsableApiKey）。

    空 / 非法字符统一抛 ``INVALID_CREDENTIAL``（消息区分原因）。
    """
    verdict, value = normalize_api_key(raw)
    if verdict == "ok":
        return value
    reason = "是空的" if verdict == "empty" else "含 HTTP 头无法携带的字符"
    raise LlmError(
        f"llm-deepseek: 从 {ref} 解析到的 API key {reason}；请设置 {ref} 为原始 key",
        INVALID_CREDENTIAL_CODE,
    )


# --------------------------------------------------------------------------- #
# 序列化（对齐 dsh-llm-deepseek 的 serialize.ts）
# --------------------------------------------------------------------------- #
def reasoning_effort(effort: str) -> str:
    """校验推理强度取值，非法抛 ``UNSUPPORTED_REASONING_EFFORT``。"""
    if effort in REASONING_EFFORTS:
        return effort
    raise LlmError(f'DeepSeek does not support reasoning effort "{effort}"', "UNSUPPORTED_REASONING_EFFORT")


def resolve_thinking(options: GenerateOptions, defaults: dict) -> dict:
    """把 thinking/effort 解析成合法 wire 组合（不把 off 暴露为 wire effort）。

    - ``purpose == 'session-title'`` → thinking 强制 disabled；
    - 仅显式传入的 effort 会被校验；默认 effort 来自配置且不校验；
    - ``thinking: disabled`` 时只允许 ``off`` 生效，否则报错；
    - ``off`` → ``{"thinking": "disabled"}``；``high|max`` → enabled + effort；
    - 其余情况回落到配置默认 thinking（无则空）。
    """
    if options.purpose == "session-title":
        return {"thinking": "disabled"}
    if options.reasoning_effort is not None:
        effort = reasoning_effort(options.reasoning_effort)
    else:
        effort = defaults.get("reasoningEffort")
    if defaults.get("thinking") == "disabled" and effort is not None and effort != "off":
        raise LlmError(
            f'DeepSeek deployment does not support reasoning effort "{effort}"',
            "UNSUPPORTED_REASONING_EFFORT",
        )
    if effort == "off":
        return {"thinking": "disabled"}
    if effort in ("high", "max"):
        return {"thinking": "enabled", "reasoningEffort": effort}
    if defaults.get("thinking") is None:
        return {}
    return {"thinking": defaults["thinking"]}


def assert_text_only(blocks: tuple) -> None:
    """拒绝图片等非文本内容块（该 wire 路由是纯文本）。"""
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "image":
            raise LlmError(
                "The DeepSeek chat-completions adapter does not support image content.",
                "UNSUPPORTED_CONTENT",
            )


def flatten_text(blocks: tuple) -> str:
    """拼接消息内容里的全部文本块。"""
    return "".join(b.text for b in blocks if isinstance(b, TextBlock))


def serialize_messages(messages: list[Any]) -> list[dict]:
    """把 harness 消息翻译成 DeepSeek wire 消息列表。

    - user 文本合并为 ``role:'user'``；tool-result 块拆成独立 ``role:'tool'``
      消息（空输出用 ``'(no output)'`` 兜底）；
    - assistant 文本进 ``content``（**绝不为 null**），工具调用进 ``tool_calls``，
      推理只在**有工具调用**时回放为 ``reasoning_content``（thinking passback）；
    - 任意图片块在拼接前被拒绝。
    """
    wire: list[dict] = []
    for msg in messages:
        role = msg.role if hasattr(msg, "role") else msg.get("role")
        content = msg.content if hasattr(msg, "content") else msg.get("content", [])
        if isinstance(content, str):
            content = [TextBlock(content)]
        assert_text_only(content)
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
        # user：工具结果在 harness 词汇里挂在 user 消息上，但 DeepSeek 要求
        # 它们作为 role:'tool' 独立消息
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


def serialize_request(options: GenerateOptions, defaults: Optional[dict] = None) -> dict:
    """构造完整 wire 请求体（始终流式 + usage 上报）。

    可选字段（thinking / reasoning_effort / tools / temperature / max_tokens /
    stop）省略而非传 null，让 provider 默认生效。
    """
    defaults = defaults or {}
    messages: list[dict] = []
    if options.system is not None:
        messages.append({"role": "system", "content": options.system})
    messages.extend(serialize_messages(options.messages))

    tools: Optional[list[dict]] = None
    if options.tools:
        tools = [
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

    resolved = resolve_thinking(options, defaults)
    body: dict[str, Any] = {
        "model": options.model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if resolved.get("thinking") is not None:
        body["thinking"] = {"type": resolved["thinking"]}
    if resolved.get("reasoningEffort") is not None:
        body["reasoning_effort"] = resolved["reasoningEffort"]
    if tools:
        body["tools"] = tools
    if options.temperature is not None:
        body["temperature"] = options.temperature
    if options.max_tokens is not None:
        body["max_tokens"] = options.max_tokens
    if options.stop is not None:
        body["stop"] = options.stop
    return body


# --------------------------------------------------------------------------- #
# HTTP 错误映射（对齐 dsh-llm-deepseek 的 adapter.ts）
# --------------------------------------------------------------------------- #
def provider_retry_after_ms(value: Optional[str]) -> Optional[int]:
    """解析 ``retry-after`` 头为毫秒延迟（数字秒 / HTTP 日期）。"""
    if value is None:
        return None
    if re.fullmatch(r"\d+", value):
        delay = int(value) * 1000
        return delay if delay > 0 else None
    try:
        import email.utils
        parsed = email.utils.parsedate_to_datetime(value)
        delay = int((parsed.timestamp() - time.time()) * 1000)
        return delay if delay > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def request_id(headers: dict) -> Optional[str]:
    """从响应头提取 provider 请求 id。"""
    value = headers.get("x-request-id") or headers.get("x-deepseek-request-id")
    return value or None


def http_error_code(status: int, error: Optional[dict] = None) -> str:
    """把 HTTP 状态映射成稳定错误码（对齐 dsh 的 httpErrorCode）。"""
    if status in (401, 403):
        return "AUTH"
    error = error or {}
    detail = " ".join(
        x for x in (error.get("code"), error.get("type"), error.get("message")) if x
    )
    if is_quota_exceeded(detail):
        return QUOTA_EXCEEDED_CODE
    if status == 429:
        return "RATE_LIMIT"
    if status == 400:
        if is_context_window_exceeded(detail):
            return CONTEXT_WINDOW_EXCEEDED_CODE
        return "INVALID_REQUEST"
    if status >= 500:
        return "SERVER"
    return f"HTTP_{status}"


# --------------------------------------------------------------------------- #
# 适配器（对齐 dsh-llm-deepseek 的 adapter.ts）
# --------------------------------------------------------------------------- #
# transport：接收 (url, json_body, headers)，产出 SSE 原始行（异步可迭代）
Transport = Callable[[str, dict, dict], Awaitable[AsyncIterator[str]]]

DEFAULT_MODELS: list[dict] = [
    {"id": "deepseek-v4-flash", "name": "DeepSeek-V4-Flash", "contextWindow": DEFAULT_CONTEXT_WINDOW},
    {"id": "deepseek-v4-pro", "name": "DeepSeek-V4-Pro", "contextWindow": DEFAULT_CONTEXT_WINDOW},
]

_REASONING_EFFORT_ENTRIES = [
    {"id": "off", "name": "Off"},
    {"id": "high", "name": "High"},
    {"id": "max", "name": "Max"},
]


def model_info(provider: str, model: dict) -> dict:
    """目录条目 → 模型元信息（纯文本模态）。"""
    info = {"provider": provider, "id": model["id"], "name": model.get("name", model["id"]), "inputModalities": ["text"]}
    if model.get("description") is not None:
        info["description"] = model["description"]
    return info


class DeepSeekAdapter(LlmAdapter):
    """DeepSeek 官方端点适配器（transport-only）。

    连接事实经 ``options`` thunk 每次调用重新解析、api key 经 ``resolve_api_key``
    按请求解析（与端点同源冻结）；transport 可插拔，默认用 httpx（懒加载）。
    """

    def __init__(
        self,
        options: Callable[[], dict],
        resolve_api_key: Callable[[dict], Awaitable[str]],
        resolve_user_id: Optional[Callable[[], str]] = None,
        transport: Optional[Transport] = None,
    ) -> None:
        self._options = options
        self._resolve_api_key = resolve_api_key
        self._resolve_user_id = resolve_user_id or (lambda: "anonymous")
        self._transport = transport

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name="DeepSeek")

    def provider_retry_policy(self, provider: str):
        """返回本适配器所属 provider 的已解析重试策略。"""
        return self._options().get("retryPolicy")

    async def list_models(self, provider: str) -> list[dict]:
        return [model_info(provider, m) for m in self._options().get("models", [])]

    async def resolve_model(self, provider: str, model: str) -> dict:
        connection = self._options()
        configured = next((m for m in connection.get("models", []) if m["id"] == model), None)
        context_window = (
            configured.get("contextWindow") if configured and "contextWindow" in configured
            else connection.get("defaultContextWindow")
        )
        info = (
            model_info(provider, configured)
            if configured is not None
            else {"provider": provider, "id": model, "name": model, "inputModalities": ["text"]}
        )
        info.update({
            "context": {"contextWindow": context_window},
            "defaultMaxTokens": (
                configured.get("maxTokens") if configured and "maxTokens" in configured
                else connection.get("maxTokens")
            ),
        })
        defaults = connection.get("defaults", {})
        if defaults.get("thinking") == "disabled":
            info["reasoning"] = {
                "efforts": [{"id": "off", "name": "Off"}],
                "defaultEffort": "off",
            }
        else:
            default_effort = defaults.get("reasoningEffort")
            if default_effort == "off":
                default_effort = "off"
            elif default_effort == "max":
                default_effort = "max"
            else:
                default_effort = "high"
            info["reasoning"] = {
                "efforts": [dict(e) for e in _REASONING_EFFORT_ENTRIES],
                "defaultEffort": default_effort,
            }
        return info

    # ------------------------------------------------------------------ #
    # 流式调用
    # ------------------------------------------------------------------ #
    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        # 每次调用一次 resolve：连接事实与凭据在本请求期间冻结，下一次调用
        # 重新解析——配置变更对下一请求立即可见，飞行中的流不受影响。
        connection = self._options()
        api_key = self._resolve_api_key(connection)
        if asyncio.iscoroutine(api_key):  # 兼容同步/异步 resolver
            api_key = await api_key
        normalized = assert_usable_api_key(api_key, connection.get("apiKeyEnv", DEFAULT_API_KEY_ENV))
        user_id = self._resolve_user_id()
        body = serialize_request(options, connection.get("defaults"))
        headers = {
            "authorization": f"Bearer {normalized}",
            "content-type": "application/json",
            "accept": "text/event-stream",
            "x-deepseek-harness-user-id": str(user_id),
        }
        if options.session_id is not None:
            headers["x-deepseek-harness-session-id"] = str(options.session_id)
        if options.purpose == "compaction":
            headers["x-deepseek-harness-compact"] = "1"

        url = f"{connection['baseURL']}/chat/completions"

        async def sse_lines() -> AsyncIterator[str]:
            if self._transport is not None:
                async for line in await self._transport(url, body, headers):
                    yield line
            else:
                async for line in self._httpx_transport(url, body, headers):
                    yield line

        async def payloads() -> AsyncIterator[str]:
            async for line in sse_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                yield line[len("data:"):].strip()

        timeout = connection.get("streamIdleTimeoutMs", DEFAULT_STREAM_IDLE_TIMEOUT_MS)
        signal = options.signal
        try:
            async for chunk in self._iter_with_idle_timeout(translate(payloads()), timeout, signal):
                yield chunk
        except LlmError as exc:
            if exc.code == "TIMEOUT":
                raise LlmError(
                    f"DeepSeek stream idle timeout after {timeout}ms", "TIMEOUT", cause=exc) from exc
            raise
        except Exception as exc:  # noqa: BLE001
            if signal is not None and getattr(signal, "aborted", False):
                raise LlmError("DeepSeek request aborted by caller", "ABORTED", cause=exc) from exc
            raise LlmError(
                f"DeepSeek API stream from {connection['baseURL']} failed", "TRANSPORT", cause=exc
            ) from exc

    async def _iter_with_idle_timeout(
        self,
        iterator: AsyncIterator[StreamChunk],
        timeout_ms: int,
        signal: Any,
    ) -> AsyncIterator[StreamChunk]:
        """逐帧迭代；单次读等待超过 streamIdleTimeoutMs 抛 ``TIMEOUT``。"""
        timeout = timeout_ms / 1000.0
        while True:
            if signal is not None and getattr(signal, "aborted", False):
                raise LlmError("DeepSeek request aborted by caller", "ABORTED")
            try:
                if timeout > 0:
                    chunk = await asyncio.wait_for(anext(iterator), timeout=timeout)
                else:
                    chunk = await anext(iterator)
            except asyncio.TimeoutError as exc:
                raise LlmError(
                    f"DeepSeek stream idle timeout after {timeout_ms}ms", "TIMEOUT") from exc
            except StopAsyncIteration:
                return
            yield chunk

    async def _httpx_transport(self, url: str, json_body: dict, headers: dict) -> AsyncIterator[str]:
        """默认 transport：httpx 流式 POST，非 2xx 解析 error body 并映射错误码。"""
        import httpx  # 延迟导入，使框架本体不依赖 httpx

        try:
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST", url, json=json_body, headers=headers, timeout=300.0
                ) as resp:
                    if resp.status_code >= 400:
                        provider_error: Optional[dict] = None
                        message = f"DeepSeek API error (HTTP {resp.status_code})"
                        try:
                            parsed = json.loads(await resp.aread())
                            provider_error = parsed.get("error") if isinstance(parsed, dict) else None
                            if provider_error and provider_error.get("message"):
                                message = provider_error["message"]
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            pass  # 错误体解析失败不掩盖 HTTP 状态
                        exc = LlmError(message, http_error_code(resp.status_code, provider_error))
                        # 附加诊断字段（LlmError 构造只收 message/code/cause）
                        exc.status = resp.status_code  # type: ignore[attr-defined]
                        exc.provider_retry_after_ms = provider_retry_after_ms(  # type: ignore[attr-defined]
                            resp.headers.get("retry-after"))
                        exc.request_id = request_id(dict(resp.headers))  # type: ignore[attr-defined]
                        raise exc
                    async for line in resp.aiter_lines():
                        yield line
        except LlmError:
            raise
        except Exception as exc:  # noqa: BLE001 - DNS/拒绝连接/TLS 等传输失败
            raise LlmError(
                f"DeepSeek API request to {url} failed", "TRANSPORT", cause=exc) from exc


# --------------------------------------------------------------------------- #
# 插件（对齐 dsh-llm-deepseek 的 index.ts）
# --------------------------------------------------------------------------- #
def resolve_models(models: Optional[list[dict]]) -> list[dict]:
    """校验并规整模型目录（id 非空、去重、正整数约束）。"""
    if not models:
        return list(DEFAULT_MODELS)
    seen: set[str] = set()
    out: list[dict] = []
    for model in models:
        mid = model.get("id", "")
        if not mid:
            raise ValueError("llm-deepseek: catalog model ids must be non-empty")
        if mid in seen:
            raise ValueError(f'llm-deepseek: duplicate catalog model "{mid}"')
        seen.add(mid)
        if model.get("contextWindow") is not None:
            cw = model["contextWindow"]
            if not isinstance(cw, int) or cw <= 0:
                raise ValueError(f'llm-deepseek: catalog model "{mid}" contextWindow must be a positive integer')
        if model.get("maxTokens") is not None:
            mt = model["maxTokens"]
            if not isinstance(mt, int) or mt <= 0:
                raise ValueError(f'llm-deepseek: catalog model "{mid}" maxTokens must be a positive integer')
        entry = {"id": mid}
        for key in ("name", "description", "contextWindow", "maxTokens"):
            if model.get(key) is not None:
                entry[key] = model[key]
        out.append(entry)
    return out


def resolve_adapter_options(config: dict, env: Optional[dict] = None) -> dict:
    """把原始配置解析成一次调用所需的连接事实（校验 + 默认值 + 分层端点）。

    与 dsh 的 resolveAdapterOptions 对应：程序化构造可能绕过 schema 校验，
    因此这里的每个默认值与边界都被重新判定（加载时 fail loud，settings
    快照首次使用时同样）。
    """
    if config.get("thinking") == "disabled" and config.get("reasoningEffort") not in (None, "off"):
        raise ValueError('llm-deepseek: only reasoningEffort "off" can be configured when thinking is disabled')
    if config.get("defaultContextWindow") is not None:
        cw = config["defaultContextWindow"]
        if not isinstance(cw, int) or cw <= 0:
            raise ValueError("llm-deepseek: defaultContextWindow must be a positive integer")
    if config.get("maxTokens") is not None:
        mt = config["maxTokens"]
        if not isinstance(mt, int) or mt <= 0:
            raise ValueError("llm-deepseek: maxTokens must be a positive safe integer")
    idle = config.get("streamIdleTimeoutMs", DEFAULT_STREAM_IDLE_TIMEOUT_MS)
    if not isinstance(idle, (int, float)) or idle <= 0 or idle > MAX_TIMER_DELAY_MS:
        raise ValueError(
            f"llm-deepseek: streamIdleTimeoutMs must be a positive finite number no greater than {MAX_TIMER_DELAY_MS}")

    base_url = config.get("baseURL")
    if base_url is None and env is not None:
        base_url = env.get(BASE_URL_ENV)
    if base_url is None:
        base_url = PUBLIC_BASE_URL

    return {
        "apiKeyEnv": credential_ref(config.get("apiKeyEnv") or DEFAULT_API_KEY_ENV),
        "baseURL": base_url,
        "defaults": {
            "thinking": config.get("thinking"),
            "reasoningEffort": config.get("reasoningEffort"),
        },
        "maxTokens": config.get("maxTokens") or DEFAULT_MAX_TOKENS,
        "defaultContextWindow": config.get("defaultContextWindow") or DEFAULT_CONTEXT_WINDOW,
        "models": resolve_models(config.get("models")),
        "streamIdleTimeoutMs": idle,
        "retryPolicy": resolve_retry_policy(config.get("retryPolicy"), "llm-deepseek: retryPolicy"),
    }


Config = z.object({
    "apiKeyEnv": z.string().optional(),
    "baseURL": z.string().optional(),
    "thinking": z.union([z.const("enabled"), z.const("disabled")]).optional(),
    "reasoningEffort": z.union([z.const("off"), z.const("high"), z.const("max")]).optional(),
    "maxTokens": z.integer(minimum=1).optional(),
    "defaultContextWindow": z.integer(minimum=1).optional(),
    "models": z.array(z.object({
        "id": z.string(),
        "name": z.string().optional(),
        "description": z.string().optional(),
        "contextWindow": z.integer(minimum=1).optional(),
        "maxTokens": z.integer(minimum=1).optional(),
    })).optional(),
    "streamIdleTimeoutMs": z.number(minimum=1).optional(),
    "retryPolicy": z.any().optional(),
})


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``deepseek-official`` 路由与 V4 模型目录。

    - 连接事实每次请求重新解析（settings 变更即时生效）；
    - api key 经 credentials seam 解析（无 seam 时环境变量兜底）；
    - 重试策略在 settings 变更时原地重注册（replace，避免路由空窗）。
    """
    config = dict(config or {})
    # 配置来源 thunk：默认组合条目；install_settings_section 的 set_source
    # 会用活动作用域替换（可变容器避免闭包/global 问题）
    state: dict[str, Any] = {
        "current": (lambda cfg=config: cfg),
        "last_raw": None,
        "last_good": None,
    }

    def options() -> dict:
        raw = state["current"]()
        if raw == state["last_raw"] and state["last_good"] is not None:
            return state["last_good"]
        try:
            resolved = resolve_adapter_options(raw)
            state["last_raw"] = raw
            state["last_good"] = resolved
            return resolved
        except Exception as exc:  # noqa: BLE001
            # 静态装配在注册前解析（fail loud）；settings 快照非法时保留最后
            # 一份好配置，避免正在服务的请求被坏快照打断。
            if state["last_good"] is None:
                raise
            state["last_raw"] = raw
            return state["last_good"]

    async def resolve_api_key(connection: dict) -> str:
        # 1. 统一配置文件优先（llm.api_key，不依赖环境变量）
        if ctx.has_service("appConfig"):
            configured = ctx.appConfig.get("llm.api_key")
            if configured:
                return configured
        ref = connection["apiKeyEnv"]
        credentials = ctx.credentials if ctx.has_service("credentials") else None
        if credentials is not None:
            hit = await credentials.resolve(ref)
            if hit is not None:
                return hit["value"]
        ambient = os.environ.get(ref)
        if ambient:
            return ambient
        raise LlmError(
            f'llm-deepseek: no API key for provider route "{PROVIDER}"; set llm.api_key in '
            f"the config file, store {ref} through the credentials service, or export "
            f"{ref} in the launching environment",
            "MISSING_CREDENTIAL",
        )

    adapter = DeepSeekAdapter(options, resolve_api_key)
    options()  # 启动即解析一次（fail loud）
    registered_policy = options()["retryPolicy"]
    ctx.llm.register_adapter([PROVIDER], adapter, retry=registered_policy)

    def ensure_registration_facts() -> None:
        """重试策略变化时原地重注册路由（replace 原子，不发布空路由集）。"""
        nonlocal registered_policy
        policy = options()["retryPolicy"]
        if policy == registered_policy:
            return
        ctx.llm.register_adapter([PROVIDER], adapter, replace=True, retry=policy)
        registered_policy = policy

    install_settings_section(ctx, settings_namespace("llm-deepseek"), Config, config, {
        "set_source": lambda source: state.update(current=source),
        "on_change": ensure_registration_facts,
    })


name = "llm-deepseek"
apply.inject = ["llm"]        # 依赖：llm 服务先就绪（拓扑自动排序）
apply.provides = []           # 不提供新服务，只注册适配器

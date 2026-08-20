"""通用 pi-ai 后端 LLM 适配器插件（对标 dsh 的 ``llm-pi-ai``）。

一个插件实例拥有多个 provider 路由：路由命名一个内置目录供应商时，该供应商
的端点/协议/模型目录作为默认，配置按字段逐项覆盖；路由名目录不认得的，整个
供应商由配置声明。配置事实按请求解析（profile 快照 + 凭据同源冻结），改配置
对下一次请求立即可见；改路由集（或路由的重试策略）原地重注册同一适配器实例。

与 dsh 的差异（均已在对应位置标注）：

- **协议表**：dsh 支持 ``openai-completions`` / ``openai-responses`` /
  ``anthropic-messages`` 三种（Bedrock/Vertex/Azure/Codex 因认证形态无法表达而
  拒）；Python 版仅 wire 层实现 ``openai-completions``（复用
  ``openai_compatible.py`` 的 serialize/translate），其余协议名配置时 fail-loud
  拒绝并列出支持表——同为「窄协议表」哲学，子集更窄。
- **内置目录**：dsh 使用 ``@earendil-works/pi-ai`` 的完整内置供应商目录；Python
  版为核心子集（openai / deepseek / openrouter / ollama），模型为代表性样本，
  容量数字按官方文档填写但非完整枚举。
- **thinkingFormat**：dsh 提供 8 种推理分发格式（openai/deepseek/openrouter/
  together/zai/qwen/string-thinking/ant-ling）；Python 版仅实现 ``openai``
  （``reasoning_effort`` 字段）与 ``deepseek``（``thinking`` 结构 + effort）两种
  wire 映射，其余格式配置时拒绝。
- **replay state**：dsh 为 pi-ai 响应存储版本化私有投影（文本/推理签名，供缓存
  命中重放）；Python 版 wire 为 openai-completions，assistant 历史直接由持久化
  内容块重构，无需签名投影（``replay.ts`` 的 foreignAssistant 路径即全貌）。
- **transport 选项**：``transport`` / ``cacheRetention`` / ``thinkingBudgets`` /
  ``websocketConnectTimeoutMs`` 保留在配置解析中（校验枚举），wire 恒为 SSE，
  这些 pi-ai 专属选项无对应物。
- **attachments**：dsh 支持 durable image 输入；Python 版为纯文本路由，含图片
  块显式拒绝（``UNSUPPORTED_CONTENT``）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.services.adapters.deepseek import http_error_code, is_quota_exceeded
from dsh_py.services.adapters.openai_compatible import (
    DONE,
    map_usage,
    serialize_messages,
    translate,
)
from dsh_py.services.attribution import attribution_headers
from dsh_py.services.credentials import credential_ref
from dsh_py.services.llm import (
    GenerateOptions,
    LlmAdapter,
    LlmError,
    LlmProviderInfo,
    StreamChunk,
    normalize_api_key,
)
from dsh_py.services.retry_policy import resolve_retry_policy
from dsh_py.services.settings import install_settings_section, settings_namespace

# --------------------------------------------------------------------------- #
# 常量（对齐 dsh-llm-pi-ai 的 config.ts）
# --------------------------------------------------------------------------- #
DEFAULT_STREAM_IDLE_TIMEOUT_MS = 300_000   # 单次流读空闲上限（默认 5 分钟）
DEFAULT_CONTEXT_WINDOW = 262_144            # 目录/配置都没标容量时的上下文容量
DEFAULT_MAX_TOKENS = 32_768                 # 目录/配置都没标输出能力时的上限
DEFAULT_INPUT: tuple[str, ...] = ("text",)  # 缺省模态声明：纯文本
MAX_TIMER_DELAY_MS = 2**31 - 1
MAX_RESPONSE_BYTES = 4 * 1024 * 1024        # 模型发现响应的字节上限

PROVIDER = "llm-pi-ai"                       # 插件路由命名空间前缀（settings 用）
QUOTA_EXCEEDED_CODE = "QUOTA"
INVALID_CREDENTIAL_CODE = "INVALID_CREDENTIAL"

# pi-ai 的思考档位（升序；dsh 用 Record 键做漂移门，Python 用元组）
THINKING_LEVELS: tuple[str, ...] = ("off", "minimal", "low", "medium", "high", "xhigh", "max")

# 请求模态（pi-ai 的 Model.input 成员）
MODALITIES: tuple[str, ...] = ("text", "image")

# 推理分发格式：Python 版 wire 子集（dsh 8 种，此处仅实现 2 种）
SUPPORTED_THINKING_FORMATS: tuple[str, ...] = ("openai", "deepseek")

# 支持的路由协议：Python 版 wire 子集（仅 openai-completions）
SUPPORTED_PROTOCOLS: tuple[str, ...] = ("openai-completions",)

# 模型发现可读取的协议（OpenAI 的 GET /models 形态 + bearer 认证）
LISTABLE_PROTOCOLS: frozenset[str] = frozenset({"openai-completions"})


# --------------------------------------------------------------------------- #
# 内置供应商目录（对齐 dsh 的 catalog.ts，Python 版核心子集）
# --------------------------------------------------------------------------- #
# 目录模型字段：id/name/contextWindow/maxTokens/input/reasoning/reasoningEfforts/compat
BUILTIN_CATALOG: dict[str, dict] = {
    "openai": {
        "api": "openai-completions",
        "baseURL": "https://api.openai.com/v1",
        "models": [
            {"id": "gpt-4o", "name": "GPT-4o", "contextWindow": 128_000, "maxTokens": 16_384},
            {"id": "gpt-4o-mini", "name": "GPT-4o mini", "contextWindow": 128_000, "maxTokens": 16_384},
            {"id": "gpt-4.1", "name": "GPT-4.1", "contextWindow": 1_047_576, "maxTokens": 32_768},
            {
                "id": "o3-mini", "name": "O3 mini", "contextWindow": 200_000, "maxTokens": 100_000,
                "reasoning": True,
                "reasoningEfforts": {"off": None, "low": "low", "medium": "medium", "high": "high"},
            },
        ],
    },
    "deepseek": {
        "api": "openai-completions",
        "baseURL": "https://api.deepseek.com",
        "compat": {"thinkingFormat": "deepseek"},   # 官方端点思考参数为 thinking 结构
        "models": [
            {"id": "deepseek-v4-flash", "name": "DeepSeek-V4-Flash",
             "contextWindow": 1_000_000, "maxTokens": 256_000},
            {"id": "deepseek-v4-pro", "name": "DeepSeek-V4-Pro",
             "contextWindow": 1_000_000, "maxTokens": 256_000},
        ],
    },
    "openrouter": {
        "api": "openai-completions",
        "baseURL": "https://openrouter.ai/api/v1",
        "models": [
            {"id": "openrouter/auto", "name": "OpenRouter Auto",
             "contextWindow": 128_000, "maxTokens": 16_384},
        ],
    },
    "ollama": {
        "api": "openai-completions",
        "baseURL": "http://localhost:11434/v1",
        "models": [
            {"id": "llama3.1:8b", "name": "Llama 3.1 8B", "contextWindow": 128_000, "maxTokens": 8_192},
            {"id": "qwen2.5:7b", "name": "Qwen 2.5 7B", "contextWindow": 32_768, "maxTokens": 8_192},
        ],
    },
}


def catalog_provider_ids() -> list[str]:
    """内置目录供应商路由清单。"""
    return list(BUILTIN_CATALOG)


def catalog_models(provider: str) -> dict[str, dict]:
    """内置目录模型按 id 索引（路由不在目录时为空）。"""
    return {m["id"]: m for m in BUILTIN_CATALOG.get(provider, {}).get("models", [])}


def catalog_shared_api(provider: str) -> Optional[str]:
    """目录路由各模型的共同协议（目录内所有模型协议一致时才有答案）。"""
    entry = BUILTIN_CATALOG.get(provider)
    if entry is None:
        return None
    return entry.get("api")


def catalog_base_url(provider: str) -> Optional[str]:
    """目录路由的默认端点。"""
    entry = BUILTIN_CATALOG.get(provider)
    return entry.get("baseURL") if entry is not None else None


def catalog_compat(provider: str) -> Optional[dict]:
    """目录路由的默认推理分发开关（如 deepseek 的 thinkingFormat）。"""
    entry = BUILTIN_CATALOG.get(provider)
    return entry.get("compat") if entry is not None else None


# --------------------------------------------------------------------------- #
# 配置解析（对齐 dsh-llm-pi-ai 的 config.ts + catalog.ts）
# --------------------------------------------------------------------------- #
def _invalid(provider: str, detail: str) -> None:
    """报告一个不可服务的路由配置（命名出错键）。"""
    raise ValueError(f"llm-pi-ai: provider {provider!r} {detail}")


def _declared_input(configured: Optional[list]) -> Optional[list]:
    """条目声明的模态；缺省/空数组都表示「无答案」交给下一层。"""
    return list(configured) if configured else None


def _reject_removed_fields(provider: str, source: dict) -> None:
    """拒绝已移除的预发布字段并命名替代。"""
    if "provider" in source:
        _invalid(provider, 'sets "provider", which moved to the providers dict key')
    if "maxRetries" in source or "maxRetryDelayMs" in source:
        _invalid(provider, 'sets maxRetries or maxRetryDelayMs, which were removed; '
                           "compose agent recovery with dsh-llm-retry")


def _resolve_model_reasoning(provider: str, entry: dict, base: Optional[dict]) -> dict:
    """解析一个模型的推理能力（对齐 resolveModelReasoning）。

    显式 dict → wire 映射（未声明档位钉死为不支持；off 可留空值=「支持、不发」）；
    ``False`` → 非推理；缺省 → 继承目录条目。
    """
    efforts = entry.get("reasoningEfforts")
    if efforts is None:
        # 推理能力随目录条目（base）或缺失：目录条目的档位映射随 `...base` 语义
        # 一并继承（o3-mini 等目录推理模型的 reasoningEfforts 在目录里）
        result: dict[str, Any] = {"reasoning": bool(base and base.get("reasoning"))}
        if base and isinstance(base.get("reasoningEfforts"), dict):
            result["reasoningEfforts"] = dict(base["reasoningEfforts"])
        return result
    if efforts is False:
        return {"reasoning": False}
    if not isinstance(efforts, dict) or not efforts:
        _invalid(provider, f'model {entry["id"]!r} has an empty reasoningEfforts; declare the offered '
                            "levels, set false for a non-reasoning model, or omit the field to keep "
                            "the installed catalog's capability")
    for level, wire in efforts.items():
        if level not in THINKING_LEVELS:
            _invalid(provider, f'model {entry["id"]!r} reasoningEfforts names unknown level {level!r}')
        if wire is None:
            if level != "off":
                _invalid(provider, f'model {entry["id"]!r} reasoningEfforts.{level} needs the wire value '
                                   "dispatch should send; only \"off\" may leave it empty")
        elif not isinstance(wire, str) or not wire:
            _invalid(provider, f'model {entry["id"]!r} reasoningEfforts.{level} must not be an empty string')
    if not any(level != "off" for level in efforts):
        _invalid(provider, f'model {entry["id"]!r} reasoningEfforts offers no level beyond "off"; '
                           "declare a thinking level, or set reasoningEfforts to false for a "
                           "non-reasoning model")
    return {"reasoning": True, "reasoningEfforts": dict(efforts)}


def _resolve_model_compat(
    provider: str,
    entry: dict,
    route_compat: Optional[dict],
    base: Optional[dict],
    api: str,
) -> dict:
    """解析一个模型的推理分发开关（仅 openai-completions 模型接受）。"""
    entry_compat = entry.get("compat") or {}
    thinking_format = entry_compat.get("thinkingFormat", route_compat.get("thinkingFormat") if route_compat else None)
    supports_effort = entry_compat.get(
        "supportsReasoningEffort",
        route_compat.get("supportsReasoningEffort") if route_compat else None,
    )
    if thinking_format is None and supports_effort is None:
        return {}
    if api != "openai-completions":
        if entry_compat:
            _invalid(provider, f'model {entry["id"]!r} sets compat reasoning switches, but its api is '
                               f"{api!r}; thinkingFormat and supportsReasoningEffort exist only on "
                               "openai-completions")
        return {}
    inherited = dict(base.get("compat") or {}) if base and base.get("api") == api else {}
    compat = dict(inherited)
    if thinking_format is not None:
        compat["thinkingFormat"] = thinking_format
    if supports_effort is not None:
        compat["supportsReasoningEffort"] = supports_effort
    return {"compat": compat}


def resolve_route_models(provider: str, route: dict) -> dict:
    """材料化一条路由的模型目录（对齐 resolveRouteModels）。

    缺省 ``models`` 时服务内置目录；``modelOverrides`` 仅目录路由、无 models 列表、
    且目录内 id 有效时生效；每处 miss 都拒绝而非跳过。
    """
    defaults = catalog_models(provider)
    provider_base_url = catalog_base_url(provider)
    configured = route.get("models") or []
    overrides = route.get("modelOverrides") or {}
    route_compat = route.get("compat")

    for oid, override in overrides.items():
        if not oid:
            _invalid(provider, "has a modelOverrides entry with an empty model id")
        if not defaults:
            _invalid(provider, f"sets modelOverrides for {oid!r}, but the installed catalog does not "
                               "describe this route; a declared route spells every model out in its "
                               "models list")
        if configured:
            _invalid(provider, f"sets modelOverrides for {oid!r} beside a models list; models already "
                               "replaces the served catalog, so declare the fields on its entries")
        if oid not in defaults:
            _invalid(provider, f"modelOverrides names {oid!r}, which the installed catalog does not describe")
        if "id" in override:
            _invalid(provider, f'modelOverrides entry {oid!r} sets "id", which is the dict key')

    if configured:
        entries: list[dict] = [dict(e) for e in configured]
    else:
        entries = [{"id": mid, **(overrides.get(mid) or {})} for mid in defaults]

    if not entries:
        _invalid(provider, "resolves no models; the installed catalog does not describe this route, "
                           "so its models must be listed in configuration")

    route_api = catalog_shared_api(provider)
    default_context = route.get("defaultContextWindow", DEFAULT_CONTEXT_WINDOW)
    default_max_tokens = route.get("defaultMaxTokens", DEFAULT_MAX_TOKENS)
    default_input = route.get("defaultInput") or list(DEFAULT_INPUT)
    seen: set[str] = set()
    configured_max_tokens: dict[str, int] = {}
    models: list[dict] = []
    for entry in entries:
        mid = entry.get("id", "")
        if not mid:
            _invalid(provider, "has a model with an empty id")
        if mid in seen:
            _invalid(provider, f"lists model {mid!r} more than once")
        seen.add(mid)
        base = defaults.get(mid)
        api = route.get("api") or (base.get("api") if base else None) or route_api
        if api is None:
            _invalid(provider, f'model {mid!r} needs an api; the installed catalog does not describe '
                               "it, so set the route's api to the wire protocol its endpoint speaks")
        if api not in SUPPORTED_PROTOCOLS:
            _invalid(provider, f'model {mid!r} resolves api {api!r}, which this build cannot serve; '
                               f"supported protocols are {', '.join(SUPPORTED_PROTOCOLS)}")
        base_url = route.get("baseURL") or (base.get("baseUrl") if base else None) or provider_base_url
        if base_url is None:
            _invalid(provider, f'model {mid!r} needs a baseURL; the installed catalog does not '
                               "describe this route")
        context_window = entry.get("contextWindow") or (base.get("contextWindow") if base else None) or default_context
        if not isinstance(context_window, int) or context_window <= 0:
            _invalid(provider, f"model {mid!r} contextWindow must be a positive integer")
        max_tokens = entry.get("maxTokens") or (base.get("maxTokens") if base else None) or default_max_tokens
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            _invalid(provider, f"model {mid!r} maxTokens must be a positive integer")
        # 只有配置显式命名的 maxTokens 才是部署选择的请求默认；目录能力不进请求默认
        if entry.get("maxTokens") is not None:
            configured_max_tokens[mid] = entry["maxTokens"]
        model: dict[str, Any] = {
            "id": mid,
            "name": entry.get("name") or (base.get("name") if base else None) or mid,
            "api": api,
            "provider": provider,
            "baseUrl": base_url,
            "input": _declared_input(entry.get("input")) or (base.get("input") if base else None) or list(default_input),
            "contextWindow": context_window,
            "maxTokens": max_tokens,
        }
        model.update(_resolve_model_reasoning(provider, entry, base))
        model.update(_resolve_model_compat(provider, entry, route_compat, base, api))
        models.append(model)

    return {"models": models, "configuredMaxTokens": configured_max_tokens}


def resolve_profiles(providers: Any) -> dict[str, dict]:
    """校验 provider 路由集并返回按路由键索引的已解析 profile（对齐 resolveProfiles）。

    这是唯一显式解析点：缺省 dict → 空（dormant）路由集；每个路由的模型目录与
    协议在此一次材料化。不可服务的配置在此 fail-loud，保留上一份好路由集服务。
    """
    if providers is None:
        providers = {}
    if isinstance(providers, list):
        raise ValueError("llm-pi-ai: providers is now a dict keyed by provider route, not an array "
                         "of profiles")
    if not isinstance(providers, dict):
        raise ValueError("llm-pi-ai: providers must be a dict keyed by provider route")
    resolved: dict[str, dict] = {}
    for provider, source in providers.items():
        if not isinstance(provider, str) or not provider:
            raise ValueError("llm-pi-ai: provider names must be non-empty")
        if not isinstance(source, dict):
            _invalid(provider, "profile must be an object")
        _reject_removed_fields(provider, source)
        if source.get("baseURL") == "":
            _invalid(provider, "has an empty baseURL")
        if source.get("displayName") == "":
            _invalid(provider, "has an empty displayName")
        idle = source.get("streamIdleTimeoutMs", DEFAULT_STREAM_IDLE_TIMEOUT_MS)
        if not isinstance(idle, (int, float)) or isinstance(idle, bool) or not (0 < idle <= MAX_TIMER_DELAY_MS):
            _invalid(provider, f"streamIdleTimeoutMs must be a positive finite number no greater than "
                               f"{MAX_TIMER_DELAY_MS}")
        default_input = list(source.get("defaultInput") or DEFAULT_INPUT)
        if not default_input:
            _invalid(provider, "defaultInput must name at least one modality")
        for modality in default_input:
            if modality not in MODALITIES:
                _invalid(provider, f"defaultInput names unknown modality {modality!r}")
        display_name = source.get("displayName") or provider
        api_key_env = source.get("apiKeyEnv")
        if api_key_env is not None:
            api_key_env = credential_ref(api_key_env)
        api = source.get("api")
        if api is not None and api not in SUPPORTED_PROTOCOLS:
            _invalid(provider, f"names api {api!r}, which this build cannot serve; supported protocols "
                               f"are {', '.join(SUPPORTED_PROTOCOLS)}")
        reasoning = source.get("reasoning")
        if reasoning is not None and reasoning not in THINKING_LEVELS:
            _invalid(provider, f"names unknown reasoning level {reasoning!r}")
        transport = source.get("transport")
        if transport is not None and transport not in ("sse", "websocket", "websocket-cached", "auto"):
            _invalid(provider, f"names unknown transport {transport!r}")
        retention = source.get("cacheRetention")
        if retention is not None and retention not in ("none", "short", "long"):
            _invalid(provider, f"names unknown cacheRetention {retention!r}")
        route_compat = source.get("compat")
        if route_compat is not None:
            for key in route_compat:
                if key not in ("thinkingFormat", "supportsReasoningEffort"):
                    _invalid(provider, f"compat names unknown key {key!r}")
            if route_compat.get("thinkingFormat") is not None and \
                    route_compat["thinkingFormat"] not in SUPPORTED_THINKING_FORMATS:
                _invalid(provider, f"compat.thinkingFormat {route_compat['thinkingFormat']!r} is not "
                                   f"supported here; wire formats are {', '.join(SUPPORTED_THINKING_FORMATS)}")
        catalog = resolve_route_models(provider, {
            "api": api,
            "baseURL": source.get("baseURL"),
            "models": source.get("models"),
            "modelOverrides": source.get("modelOverrides"),
            "compat": route_compat,
            "defaultContextWindow": source.get("defaultContextWindow", DEFAULT_CONTEXT_WINDOW),
            "defaultMaxTokens": source.get("defaultMaxTokens", DEFAULT_MAX_TOKENS),
            "defaultInput": default_input,
        })
        resolved[provider] = {
            "provider": provider,
            "displayName": display_name,
            "apiKeyEnv": api_key_env,
            "baseURL": source.get("baseURL") or catalog_base_url(provider),
            "api": api,
            "headers": dict(source["headers"]) if source.get("headers") else None,
            "reasoning": reasoning,
            "thinkingBudgets": dict(source["thinkingBudgets"]) if source.get("thinkingBudgets") else None,
            "cacheRetention": retention,
            "transport": transport,
            "timeoutMs": source.get("timeoutMs"),
            "websocketConnectTimeoutMs": source.get("websocketConnectTimeoutMs"),
            "streamIdleTimeoutMs": idle,
            "retryPolicy": resolve_retry_policy(source.get("retryPolicy"), f'llm-pi-ai: provider "{provider}" retryPolicy'),
            "configuredMaxTokens": catalog["configuredMaxTokens"],
            "models": catalog["models"],
        }
    return resolved


def assert_serviceable(config: dict) -> None:
    """拒绝本适配器无法服务的配置区（settings 命名空间校验器）。"""
    resolve_profiles(config.get("providers"))


# 顶层 schema：providers dict 的逐项校验在 resolve_profiles 完成（程序化构造可能
# 绕过 schema，因此边界与默认在解析点重判——与 dsh 的 assertServiceable 同为
# validator 而非 schema transform）
Config = z.object({
    "providers": z.any().optional(),
})


# --------------------------------------------------------------------------- #
# 错误分类（对齐 dsh-llm-pi-ai 的 stream.ts classifyPiAiError）
# --------------------------------------------------------------------------- #
_ERR_AUTH = re.compile(r"\b(?:401|403)\b")
_ERR_RATE_LIMIT = re.compile(r"\b429\b|rate.?limit", re.I)
_ERR_INVALID = re.compile(r"\b400\b|invalid.?request", re.I)
_ERR_SERVER = re.compile(r"\b5\d\d\b")
_ERR_TIMEOUT = re.compile(r"\btime(?:d)?\s*out\b|timeout", re.I)
_ERR_TRUNCATED = re.compile(r"stream ended (?:before|without)\b", re.I)
_ERR_TRANSPORT = re.compile(
    r"\b(?:network|connection|socket|fetch)\b|\bECONN[A-Z]+\b"
    r"|other side closed|HTTP2 request did not get a response|WebSocket closed unexpectedly"
    r"|\bterminated\b|premature close", re.I)


def classify_error(message: str) -> str:
    """把 provider 错误文本归类为稳定错误码（对齐 classifyPiAiError）。"""
    if _ERR_AUTH.search(message):
        return "AUTH"
    if is_quota_exceeded(message):
        return QUOTA_EXCEEDED_CODE
    if _ERR_RATE_LIMIT.search(message):
        return "RATE_LIMIT"
    if _ERR_INVALID.search(message):
        return "INVALID_REQUEST"
    if _ERR_SERVER.search(message):
        return "SERVER"
    if _ERR_TIMEOUT.search(message):
        return "TIMEOUT"
    if _ERR_TRUNCATED.search(message) or _ERR_TRANSPORT.search(message):
        return "TRANSPORT"
    return "PI_AI_ERROR"


# --------------------------------------------------------------------------- #
# wire 请求构造（对齐 dsh-llm-pi-ai 的 context.ts + adapter.ts）
# --------------------------------------------------------------------------- #
def _content_has_image(message: Any) -> bool:
    """检查一条消息是否含图片内容块（Python 版为纯文本路由）。"""
    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
    if isinstance(content, str):
        return False
    for block in content or ():
        if isinstance(block, dict) and block.get("type") == "image":
            return True
        if not isinstance(block, dict) and getattr(block, "type", None) == "image":
            return True
    return False


def _resolve_wire_reasoning(options: GenerateOptions, model: dict, profile_reasoning: Optional[str]) -> dict:
    """把请求/profile 的思考档位解析成 wire 字段（openai / deepseek 两种格式）。

    - ``thinkingFormat=deepseek``：off → ``thinking: disabled``；高档 → enabled +
      ``reasoning_effort``（wire 值优先取模型声明的映射）；
    - ``thinkingFormat=openai``：off/缺省 → 不发；其余 → ``reasoning_effort``；
    - 模型非推理（``reasoning=False``）→ 任何 effort 都报 ``UNSUPPORTED_REASONING_EFFORT``。
    """
    if not model.get("reasoning"):
        if options.reasoning_effort is not None or profile_reasoning is not None:
            effort = options.reasoning_effort or profile_reasoning
            if effort != "off":
                raise LlmError(
                    f'pi-ai model "{model["id"]}" does not support reasoning effort "{effort}"',
                    "UNSUPPORTED_REASONING_EFFORT",
                )
        return {}
    effort = options.reasoning_effort or profile_reasoning
    if effort is None or effort == "off":
        if _thinking_format_of(model) == "deepseek":
            return {"thinking": {"type": "disabled"}}
        return {}
    declared = model.get("reasoningEfforts")
    wire_value: Optional[str] = None
    if isinstance(declared, dict) and effort in declared:
        wire_value = declared[effort]
    if wire_value is None:
        wire_value = effort
    if _thinking_format_of(model) == "deepseek":
        return {"thinking": {"type": "enabled"}, "reasoning_effort": wire_value}
    return {"reasoning_effort": wire_value}


def _thinking_format_of(model: dict) -> str:
    """模型/路由的推理分发格式（缺省 openai）。"""
    compat = model.get("compat") or {}
    return compat.get("thinkingFormat", "openai")


def build_wire_request(options: GenerateOptions, model: dict, profile: dict) -> dict:
    """构造 openai-completions wire 请求体（复用 openai_compatible 序列化语义）。"""
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
            {"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""),
                "parameters": t.get("parameters", {}),
            }}
            for t in options.tools
        ]
    if options.temperature is not None:
        body["temperature"] = options.temperature
    # 请求未命名输出上限时，仅 profile 显式配置的 maxTokens 作为请求默认
    if options.max_tokens is None:
        configured = profile["configuredMaxTokens"].get(options.model)
        if configured is not None:
            body["max_tokens"] = configured
    else:
        body["max_tokens"] = options.max_tokens
    if options.stop:
        body["stop"] = options.stop
    body.update(_resolve_wire_reasoning(options, model, profile.get("reasoning")))
    return body


def request_headers(profile_headers: Optional[dict]) -> dict:
    """合并部署头，attribution 保留名获胜（对齐 requestHeaders）。"""
    attribution = attribution_headers()
    reserved = {name.lower() for name in attribution}
    merged = {
        name: value
        for name, value in (profile_headers or {}).items()
        if name.lower() not in reserved
    }
    merged.update(attribution)
    return merged


# --------------------------------------------------------------------------- #
# 适配器（对齐 dsh-llm-pi-ai 的 adapter.ts）
# --------------------------------------------------------------------------- #
Transport = Callable[[str, dict, dict], Awaitable[AsyncIterator[str]]]


class PiAiAdapter(LlmAdapter):
    """通用多供应商适配器（快照语义）。

    每次操作读取当前 profiles（配置变更对下一请求立即可见）；一次 stream 调用
    捕获一个 profile+model 快照并连同凭据一起冻结到请求结束。
    """

    def __init__(
        self,
        profiles: Callable[[], dict[str, dict]],
        resolve_api_key: Callable[[str, dict], Awaitable[Optional[str]]],
        transport: Optional[Transport] = None,
    ) -> None:
        self._profiles = profiles
        self._resolve_api_key = resolve_api_key
        self._transport = transport

    # ------------------------------------------------------------------ #
    # 元信息
    # ------------------------------------------------------------------ #
    def provider_info(self, provider: str) -> LlmProviderInfo:
        profile = self._profiles().get(provider)
        return LlmProviderInfo(id=provider, name=(profile or {}).get("displayName") or provider)

    def provider_retry_policy(self, provider: str):
        """该路由注册时捕获的重试策略。"""
        profile = self._profiles().get(provider)
        return (profile or {}).get("retryPolicy")

    def _profile_of(self, provider: str) -> dict:
        profile = self._profiles().get(provider)
        if profile is None:
            raise LlmError(f'pi-ai adapter does not own provider "{provider}"', "NO_ADAPTER")
        return profile

    def _model_of(self, profile: dict, model: str) -> dict:
        resolved = next((m for m in profile["models"] if m["id"] == model), None)
        if resolved is None:
            raise LlmError(
                f'pi-ai provider "{profile["provider"]}" has no configured model "{model}"',
                "UNKNOWN_MODEL",
            )
        return resolved

    @staticmethod
    def _supported_efforts(model: dict) -> list[str]:
        """模型可选档位（升序）；非推理模型无档位。"""
        if not model.get("reasoning"):
            return []
        efforts = model.get("reasoningEfforts")
        if isinstance(efforts, dict):
            return [level for level in THINKING_LEVELS if level in efforts]
        return list(THINKING_LEVELS)

    def _describable_level(self, model: dict, effort: Optional[str]) -> Optional[str]:
        """模型确实支持的档位才作描述（坏配置不毁掉整个路由的模型目录）。"""
        if effort is None:
            return None
        return effort if effort in self._supported_efforts(model) else None

    def _reasoning_info(self, model: dict, default_level: Optional[str]) -> dict:
        """模型可选的推理档位描述；off-only 的模型不提供控制（与 dsh 一致）。"""
        levels = self._supported_efforts(model)
        if not levels:
            return {}
        info: dict[str, Any] = {
            "reasoning": {
                "efforts": [
                    {"id": level, "name": f"{level[0].upper()}{level[1:]}"}
                    for level in levels
                ],
            },
        }
        if default_level is not None:
            info["reasoning"]["defaultEffort"] = default_level
        return info

    async def list_models(self, provider: str) -> list[dict]:
        profile = self._profile_of(provider)
        return [
            {"provider": provider, "id": m["id"], "name": m["name"], "inputModalities": list(m["input"])}
            for m in profile["models"]
        ]

    async def resolve_model(self, provider: str, model: str) -> dict:
        profile = self._profile_of(provider)
        resolved = self._model_of(profile, model)
        default_level = self._describable_level(resolved, profile.get("reasoning"))
        configured_max_tokens = profile["configuredMaxTokens"].get(model)
        info: dict[str, Any] = {
            "provider": provider,
            "id": model,
            "name": resolved["name"],
            "inputModalities": list(resolved["input"]),
            "context": {"contextWindow": resolved["contextWindow"]},
        }
        if configured_max_tokens is not None:
            info["defaultMaxTokens"] = configured_max_tokens
        info.update(self._reasoning_info(resolved, default_level))
        return info

    # ------------------------------------------------------------------ #
    # 流式调用
    # ------------------------------------------------------------------ #
    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        if options.stop is not None:
            raise LlmError("llm-pi-ai does not support GenerateOptions.stop", "UNSUPPORTED_OPTION")
        # 一次调用一个快照：profile、model 描述与凭据同源冻结
        profile = self._profile_of(options.provider)
        model = self._model_of(profile, options.model)
        api_key = await self._resolve_api_key(options.provider, profile)

        contains_image = any(_content_has_image(m) for m in options.messages)
        if contains_image and "image" not in model["input"]:
            raise LlmError(f'pi-ai model "{model["id"]}" does not support image input', "UNSUPPORTED_CONTENT")

        body = build_wire_request(options, model, profile)
        headers = {"content-type": "application/json", "accept": "text/event-stream"}
        if api_key is not None:
            headers["authorization"] = f"Bearer {api_key}"
        headers.update(request_headers(profile.get("headers")))
        url = f"{model['baseUrl'].rstrip('/')}/chat/completions"

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

        timeout = profile["streamIdleTimeoutMs"]
        signal = options.signal
        try:
            async for chunk in self._iter_with_idle_timeout(translate(payloads()), timeout, signal):
                yield chunk
        except LlmError as exc:
            if exc.code == "TIMEOUT":
                raise LlmError(
                    f"pi-ai stream idle timeout after {timeout}ms", "TIMEOUT", cause=exc) from exc
            raise
        except Exception as exc:  # noqa: BLE001
            if signal is not None and getattr(signal, "aborted", False):
                raise LlmError("pi-ai request aborted by caller", "ABORTED", cause=exc) from exc
            raise LlmError(
                f"pi-ai API stream from {model['baseUrl']} failed", "TRANSPORT", cause=exc) from exc

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
                raise LlmError("pi-ai request aborted by caller", "ABORTED")
            try:
                if timeout > 0:
                    chunk = await asyncio.wait_for(anext(iterator), timeout=timeout)
                else:
                    chunk = await anext(iterator)
            except asyncio.TimeoutError as exc:
                raise LlmError(
                    f"pi-ai stream idle timeout after {timeout_ms}ms", "TIMEOUT") from exc
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
                        message = f"pi-ai API error (HTTP {resp.status_code})"
                        try:
                            parsed = json.loads(await resp.aread())
                            provider_error = parsed.get("error") if isinstance(parsed, dict) else None
                            if provider_error and provider_error.get("message"):
                                message = provider_error["message"]
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            pass  # 错误体解析失败不掩盖 HTTP 状态
                        code = http_error_code(resp.status_code, provider_error)
                        if code.startswith("HTTP_") and provider_error:
                            # 状态未命中任何语义时，错误文本再兜底分类一次
                            code = classify_error(message)
                        exc = LlmError(message, code)
                        exc.status = resp.status_code  # type: ignore[attr-defined]
                        exc.request_id = (  # type: ignore[attr-defined]
                            resp.headers.get("x-request-id") or None)
                        raise exc
                    async for line in resp.aiter_lines():
                        yield line
        except LlmError:
            raise
        except Exception as exc:  # noqa: BLE001 - DNS/拒绝连接/TLS 等传输失败
            raise LlmError(
                f"pi-ai API request to {url} failed", "TRANSPORT", cause=exc) from exc


# --------------------------------------------------------------------------- #
# 模型发现（对齐 dsh-llm-pi-ai 的 discovery.ts）
# --------------------------------------------------------------------------- #
def _listing_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/models"


async def discover_models(
    request: dict,
    stored_api_key: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
) -> list[dict]:
    """回答「该供应商能服务哪些模型」（配置面的"获取可用模型"动作）。

    目录路由直接答内置目录（无需网络）；网关/自托管路由 GET /models 探测。
    只读、不存储：答案是对草稿的建议，settings 仍是唯一裁决。
    """
    # 目录短路：内置条目携带清单端点不会披露的容量
    provider = request.get("provider")
    if provider is not None:
        installed = catalog_models(provider)
        if installed:
            return [
                {"id": m["id"], "name": m["name"],
                 "contextWindow": m.get("contextWindow"), "maxTokens": m.get("maxTokens")}
                for m in installed.values()
            ]
    base_url = request.get("baseURL")
    if not base_url:
        raise LlmError(
            f'pi-ai ships no catalog for provider "{provider or ""}", so its models can only come from '
            "its endpoint; set a baseURL, or enter this provider's models by hand",
            "DISCOVERY_FAILED",
        )
    api = request.get("api") or "openai-completions"
    if api not in LISTABLE_PROTOCOLS:
        raise LlmError(
            f'pi-ai protocol "{api}" has no model listing this build can read; enter this provider\'s '
            "models by hand",
            "DISCOVERY_UNSUPPORTED",
        )
    url = _listing_url(base_url)
    supplied = request.get("apiKey")
    if supplied is None and stored_api_key is not None:
        supplied = await stored_api_key()
    api_key: Optional[str] = None
    if supplied is not None:
        verdict, value = normalize_api_key(supplied)
        if verdict != "ok":
            reason = "is blank" if verdict == "empty" else "contains characters no HTTP header can carry"
            raise LlmError(
                f"this provider's API key {reason}; paste the raw key only",
                INVALID_CREDENTIAL_CODE,
            )
        api_key = value
    headers = {"accept": "application/json"}
    if api_key is not None:
        headers["authorization"] = f"Bearer {api_key}"
    headers.update(attribution_headers())

    import httpx  # 延迟导入
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        if (request.get("signal") is not None
                and getattr(request["signal"], "aborted", False)):
            raise LlmError("model discovery aborted by caller", "ABORTED", cause=exc) from exc
        raise LlmError(f"could not reach {url}", "DISCOVERY_FAILED", cause=exc) from exc
    if resp.status_code >= 400:
        raise LlmError(
            f"{url} answered {resp.status_code}"
            + ("; check the API key" if resp.status_code in (401, 403) else ""),
            "DISCOVERY_FAILED",
        )
    if len(resp.content) > MAX_RESPONSE_BYTES:
        raise LlmError(f"{url} answered with more than {MAX_RESPONSE_BYTES} bytes", "DISCOVERY_FAILED")
    try:
        body = resp.json()
    except ValueError as exc:
        raise LlmError(f"{url} did not answer with JSON", "DISCOVERY_FAILED", cause=exc) from exc
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise LlmError(
            'the endpoint\'s model listing has no "data" array; enter this provider\'s models by hand',
            "DISCOVERY_FAILED",
        )
    models: list[dict] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        mid = raw.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        entry: dict[str, Any] = {"id": mid}
        for key in ("name", "display_name"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                entry["name"] = value
                break
        for target, candidates in (("contextWindow", ("context_window", "context_length")),
                                   ("maxTokens", ("max_output_tokens", "max_tokens"))):
            for key in candidates:
                value = raw.get(key)
                if isinstance(value, int) and value > 0:
                    entry[target] = value
                    break
        models.append(entry)
    return models


# --------------------------------------------------------------------------- #
# 插件入口（对齐 dsh-llm-pi-ai 的 index.ts）
# --------------------------------------------------------------------------- #
NS = settings_namespace("llm-pi-ai")


def registration_facts(profiles: dict[str, dict]) -> list[dict]:
    """路由注册时捕获的事实（重试策略变化须重注册；按键排序免误报）。"""
    return sorted(
        [
            {"provider": p, "displayName": profile["displayName"], "retryPolicy": profile["retryPolicy"]}
            for p, profile in profiles.items()
        ],
        key=lambda item: item["provider"],
    )


def directory_entries(profiles: dict[str, dict]) -> list[dict]:
    """可配置供应商目录：内置目录中可鉴权的路由 + 当前声明的路由（后者恒在，保可删）。"""
    entries: dict[str, dict] = {}
    for provider in BUILTIN_CATALOG:
        entries[provider] = {
            "provider": provider, "displayName": provider,
            "settingsNs": NS.value if hasattr(NS, "value") else str(NS),
            "settingsPath": ["providers", provider],
            "declared": False,
        }
    for provider, profile in profiles.items():
        entries[provider] = {
            "provider": provider, "displayName": profile["displayName"],
            "settingsNs": NS.value if hasattr(NS, "value") else str(NS),
            "settingsPath": ["providers", provider],
            "declared": provider not in BUILTIN_CATALOG,
        }
    return list(entries.values())


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：为一个配置的全部 provider 路由注册一个通用 pi-ai 适配器。

    - 空路由集为 dormant 挂载：settings 区提供 profile 时才注册，区清空时撤下；
    - 配置事实按请求解析（settings 变更下一请求生效）；路由集/重试策略变化
      原地重注册（replace 原子，冲突时旧路由继续服务）；
    - 可配置供应商目录从挂载即提供全部内置路由（供配置面展示/编辑）。
    """
    config = dict(config or {})
    state: dict[str, Any] = {
        "current": (lambda cfg=config: cfg),
        "last_raw": None,
        "memoized": None,
    }

    def profiles() -> dict[str, dict]:
        raw = state["current"]()
        if raw == state["last_raw"] and state["memoized"] is not None:
            return state["memoized"]
        next_profiles = resolve_profiles(raw.get("providers"))
        state["last_raw"] = raw
        state["memoized"] = next_profiles
        return next_profiles

    profiles()  # 启动即解析一次（fail loud）

    async def resolve_api_key(provider: str, profile: dict) -> Optional[str]:
        ref = profile.get("apiKeyEnv")
        if ref is None:
            return None  # 未命名凭据 → 不认证（dsh 留给 pi-ai 环境发现，Python 版无此）
        if ctx.has_service("appConfig"):
            configured = ctx.appConfig.get("llm.api_key")
            if configured and provider in ("openai", "deepseek"):
                return configured
        credentials = ctx.credentials if ctx.has_service("credentials") else None
        if credentials is not None:
            hit = await credentials.resolve(ref)
            if hit is not None and hit["value"]:
                verdict, value = normalize_api_key(hit["value"])
                if verdict != "ok":
                    raise LlmError(
                        f"llm-pi-ai: API key from {ref} contains characters no HTTP header can carry",
                        INVALID_CREDENTIAL_CODE,
                    )
                return value
        ambient = os.environ.get(ref)
        if ambient:
            return ambient
        raise LlmError(
            f'llm-pi-ai: no credential for provider route "{provider}"; its profile resolves {ref}, '
            f"which is not set — store {ref} through the credentials service or export it",
            "MISSING_CREDENTIAL",
        )

    adapter = PiAiAdapter(profiles, resolve_api_key)

    directory: Optional[Any] = None
    directory_facts: Optional[list] = None

    def ensure_directory() -> None:
        nonlocal directory, directory_facts
        entries = directory_entries(profiles())
        if entries == directory_facts:
            return
        if directory is None:
            directory = ctx.llm.register_configurable_providers(entries)
        else:
            directory.replace(entries)
        directory_facts = entries

    ensure_directory()

    # 模型发现：目录路由直接答内置目录，网关路由探测端点；已配置路由的凭据
    # 在草稿无 key 时补上（配置面编辑的是脱敏描述符，从不持有已存密钥）
    async def stored_api_key(provider: Optional[str]) -> Optional[str]:
        if provider is None:
            return None
        profile = profiles().get(provider)
        if profile is None:
            return None
        return await resolve_api_key(provider, profile)

    def discovery_handler(request: dict) -> Awaitable[list[dict]]:
        return discover_models(request, lambda: stored_api_key(request.get("provider")))

    discovery = ctx.llm.register_model_discovery(NS, discovery_handler)

    registration: Optional[Any] = None
    registered_facts: Optional[list] = None

    def ensure_registration_facts() -> None:
        nonlocal registration, registered_facts
        facts = registration_facts(profiles())
        if facts == registered_facts:
            return
        routes = list(profiles().keys())
        if registration is None:
            if not routes:
                registered_facts = facts  # dormant：空路由集不注册
                return
            registration = ctx.llm.register_adapter(routes, adapter,
                                                    retry=profiles()[routes[0]]["retryPolicy"])
        else:
            registration.replace(routes)
        registered_facts = facts

    ensure_registration_facts()

    def on_change() -> None:
        try:
            ensure_registration_facts()
        except Exception as exc:  # noqa: BLE001 - 拒绝保留旧路由，仅记诊断
            if ctx.has_service("logger"):
                ctx.logger.warn(f"llm-pi-ai: keeping the previously registered routes after a refused update: {exc}")
        try:
            ensure_directory()
        except Exception as exc:  # noqa: BLE001
            if ctx.has_service("logger"):
                ctx.logger.warn(f"llm-pi-ai: keeping the previous configurable-provider directory after a refused update: {exc}")

    install_settings_section(ctx, NS, Config, config, {
        "validate": assert_serviceable,
        "set_source": lambda source: state.update(current=source),
        "on_change": on_change,
    })


name = "llm-pi-ai"
apply.inject = ["llm"]        # 依赖：llm 服务先就绪（拓扑自动排序）
apply.provides = []           # 不提供新服务，只注册适配器

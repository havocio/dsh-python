"""``web-search-deepseek``：向 ``ctx.web`` 注册 DeepSeek 搜索 provider。

对齐 dsh 的 ``@deepseek-ai/dsh-web-search-deepseek``：走 Anthropic 兼容 Messages
API + 原生 ``web_search_20250305`` 服务端工具。每次搜索花费一次模型调用，但返回
结构化结果块；缺失该块视为错误而非散文抓取兜底。**复用** ``DEEPSEEK_API_KEY`` 但
**不复用** ``DEEPSEEK_BASE_URL``（搜索与 chat-completions 使用不同端点）。

适配（dsh_py 差异，均已在模块 docstring / README 注明）：
- 传输以懒加载 ``httpx`` 承担；取消在检查点经 ``throw_if_aborted`` 执行，
  无 ``DOMException`` 概念——网络层 ``asyncio.CancelledError`` 映射为 ``WEB_ABORTED``。
- 请求记录（``record_request``）：dsh 经 ``agents.currentInitiator()?.session``
  落库；dsh_py 无 initiator 概念，改为写第一个活跃根 agent 的会话（无则跳过，
  仅观测性日志，不影响执行）。
- settings 区注册沿用 ``install_settings_section``（schema 置 None 表示不校验，
  配置默认值在 ``apply`` 侧补全）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from dsh_py.core.context import AppContext
from dsh_py.services.credentials import credential_ref
from dsh_py.services.settings import install_settings_section, settings_namespace
from dsh_py.services.web import (
    WebError,
    WebSearchProvider,
    WebSearchRequest,
    WebSearchResult,
    WebSearchSource,
    is_positive_integer,
    service_or_none,
    throw_if_aborted,
    url_can_parse,
    web_aborted,
)
from dsh_py.util.launch_environment import launch_environment_of

DEEPSEEK_PROVIDER_ID = "deepseek-official"

# DeepSeek 的 Anthropic 兼容端点（含 /v1；/messages 追加）。不是 chat-completions
# 的 base（https://api.deepseek.com），因此本 provider 不复用 $DEEPSEEK_BASE_URL。
DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic/v1"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"
DEEPSEEK_DEFAULT_API_VERSION = "2023-06-01"
DEEPSEEK_DEFAULT_MAX_TOKENS = 4096
DEEPSEEK_DEFAULT_MAX_USES = 5

_USER_AGENT = "deepseek-harness/0.0.1"
_DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"
# 搜索端点专属环境变量：与 $DEEPSEEK_BASE_URL 刻意区分（不同 API 族）
_SEARCH_BASE_URL_ENV = "DEEPSEEK_SEARCH_BASE_URL"

# 该 provider 的 settings 区命名空间（端点 / 模型 / key 引用）
WEB_SEARCH_DEEPSEEK_SETTINGS_NAMESPACE = settings_namespace("web-search-deepseek")

# 类型别名：Anthropic Messages 响应 / 错误（provider 私有线格式，均为裸 dict）
_ContentBlock = dict
_AnthropicResponse = dict
_AnthropicError = dict


def citation_snippets(blocks: list[_ContentBlock]) -> dict[str, str]:
    """从每个 ``text`` 块的 ``citations[]`` 构建 ``url → cited_text`` 映射。

    Anthropic ``web_search_result`` 条目通常无内联 snippet——摘录存在于独立
    ``text`` 块的 citation 里，按 url 键合（首次出现获胜）。
    """
    snippets: dict[str, str] = {}
    for block in blocks:
        if block.get("type") != "text":
            continue
        for cite in block.get("citations") or []:
            cite_url = cite.get("url")
            cited = cite.get("cited_text")
            if cite_url and cited and cite_url not in snippets:
                snippets[cite_url] = cited
    return snippets


def map_anthropic_response(response: _AnthropicResponse) -> WebSearchResult:
    """把 DeepSeek Anthropic Messages 响应映射为归一化搜索结果。

    遍历 ``web_search_tool_result`` 块取可引用 ``web_search_result`` 条目，按 url
    去重（``max_uses > 1`` 的请求可能跨搜索重复同一 url）。截断由 seam 负责，
    故此处 ``truncated`` 恒 False。无结果块抛 ``WEB_PROVIDER_ERROR``。
    """
    blocks = response.get("content") or []
    result_blocks = [b for b in blocks if b.get("type") == "web_search_tool_result"]
    if not result_blocks:
        raise WebError(
            "DeepSeek returned no web_search_tool_result blocks; the request may not have triggered native web search",
            "WEB_PROVIDER_ERROR",
        )
    snippets = citation_snippets(blocks)
    seen: set[str] = set()
    sources: list[WebSearchSource] = []
    for block in result_blocks:
        for item in block.get("content") or []:
            if item.get("type") != "web_search_result":
                continue
            url = item.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            snippet = snippets.get(url)
            title = item.get("title")
            page_age = item.get("page_age")
            sources.append(WebSearchSource(
                url=url,
                title=title if title else None,
                snippet=snippet if snippet else None,
                published_at=page_age if page_age else None,
            ))
    return WebSearchResult(sources=sources, truncated=False)


def _env_value(ctx: AppContext, key: str) -> Optional[str]:
    """读 launcher 环境快照的取值（``{"value", ...}`` 或 None）。"""
    snapshot = launch_environment_of(ctx)
    entry = snapshot["get"](key)
    if entry is None:
        return None
    return entry.get("value")


class DeepSeekSearchProvider(WebSearchProvider):
    """DeepSeek 搜索 provider（id = ``deepseek-official``）。HTTP 重定向视为错误。"""

    id = DEEPSEEK_PROVIDER_ID

    def __init__(self, resolve_options: Callable[[], dict]) -> None:
        """构造时接收**下一操作**的选项 thunk：每次操作入口快照一次，绝不让
        一次搜索混用两个配置区（settings 区可在搜索之间变更）。"""
        self._resolve_options = resolve_options

    def available(self) -> bool:
        options = self._resolve_options()
        has_key = bool((options.get("apiKey") or "") and len(options["apiKey"]) > 0) or options.get("resolveApiKey") is not None
        return (
            has_key
            and url_can_parse(options.get("baseURL", ""))
            and is_positive_integer(options.get("maxTokens"))
            and is_positive_integer(options.get("maxUses"))
        )

    async def search(self, request: WebSearchRequest, signal: Any = None) -> WebSearchResult:
        # 一次快照供整次操作：凭据解析是 await，落在其间的 settings 写入绝不能
        # 把旧区解析的 key 发往新区命名的端点。
        options = self._resolve_options()
        api_key = await self._api_key(options, signal)
        throw_if_aborted(signal)
        endpoint = f"{options['baseURL']}/messages"
        body = {
            "model": options["model"],
            "max_tokens": options["maxTokens"],
            "messages": [{
                "role": "user",
                "content": [{"type": "text", "text": f"Perform a web search for the query: {request.query}"}],
            }],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": options["maxUses"]}],
        }
        record = options.get("recordRequest")
        if record is not None:
            record({"endpoint": endpoint, "apiVersion": options["apiVersion"], "body": body})
        throw_if_aborted(signal)

        import httpx  # 懒加载：缺依赖仅在使用时报错

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    endpoint,
                    json=body,
                    headers={
                        "x-api-key": api_key,
                        "authorization": f"Bearer {api_key}",
                        "anthropic-version": options["apiVersion"],
                        "accept": "application/json",
                        "user-agent": _USER_AGENT,
                    },
                )
        except asyncio.CancelledError as exc:
            raise web_aborted(signal, exc) from exc
        except Exception as exc:  # noqa: BLE001
            if signal is not None and getattr(signal, "aborted", False):
                raise web_aborted(signal, exc) from exc
            raise WebError(f"DeepSeek search request failed: {exc}", "WEB_PROVIDER_ERROR", cause=exc) from exc

        if response.status_code < 200 or response.status_code >= 300:
            message = f"DeepSeek API error (HTTP {response.status_code})"
            try:
                parsed = response.json()
                err = parsed.get("error")
                detail = err if isinstance(err, str) else (err or {}).get("message") if isinstance(err, dict) else parsed.get("message")
                if detail:
                    message = detail
            except Exception:  # noqa: BLE001 -- 非 JSON 错误体（网关 5xx/429 常见）只损失更丰富的文案
                pass
            raise WebError(message, "WEB_PROVIDER_ERROR")

        try:
            payload = response.json()
            return map_anthropic_response(payload)
        except WebError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise WebError(
                f"DeepSeek returned an unprocessable response body: {exc}",
                "WEB_PROVIDER_ERROR",
                cause=exc,
            ) from exc

    async def _api_key(self, options: dict, signal: Any) -> str:
        """解析一次操作的凭据，不在 provider 上留存。"""
        throw_if_aborted(signal)
        literal = options.get("apiKey")
        if literal:
            return literal
        resolved: Optional[str] = None
        resolver = options.get("resolveApiKey")
        if resolver is not None:
            try:
                resolved = await resolver()
            except asyncio.CancelledError as exc:
                raise web_aborted(signal, exc) from exc
            except Exception as exc:  # noqa: BLE001
                raise WebError(
                    f"DeepSeek search credential resolution failed: {exc}",
                    "WEB_PROVIDER_ERROR",
                    cause=exc,
                ) from exc
        if resolved:
            return resolved
        ref = options.get("apiKeyEnv") or _DEFAULT_API_KEY_ENV
        raise WebError(
            f'DeepSeek search has no API key for "{ref}"; store it through the credentials service, '
            "export it in the launching environment, or set a literal \"apiKey\" in the web-search-deepseek config",
            "WEB_PROVIDER_CREDENTIAL_MISSING",
        )


def _record_deepseek_request(ctx: AppContext, request: dict) -> None:
    """把无密钥的 DeepSeek Messages 请求落到当前活跃根 agent 的会话（观测性）。"""
    loop = service_or_none(ctx, "agentLoop")
    if loop is None:
        return
    try:
        roots = loop.roots()
    except Exception:  # noqa: BLE001 -- 记录失败绝不阻断搜索
        return
    for root in roots or []:
        session = getattr(root, "session", None)
        if session is not None:
            session.append("web/deepseek-search-llm-request", request)
            return


def _resolve_options(ctx: AppContext, config: dict) -> dict:
    """把一个已解析的配置区投影为 provider 下一操作的选项。环境兜底留在这里。"""
    api_key_env = credential_ref(config.get("apiKeyEnv") or _DEFAULT_API_KEY_ENV)
    literal_key = config.get("apiKey")
    options: dict = {
        "resolveApiKey": _make_resolver(ctx, api_key_env),
        "apiKeyEnv": api_key_env,
        "baseURL": config.get("baseURL") or _env_value(ctx, _SEARCH_BASE_URL_ENV) or DEEPSEEK_DEFAULT_BASE_URL,
        "model": config.get("model") or DEEPSEEK_DEFAULT_MODEL,
        "apiVersion": config.get("apiVersion") or DEEPSEEK_DEFAULT_API_VERSION,
        "maxTokens": config.get("maxTokens") or DEEPSEEK_DEFAULT_MAX_TOKENS,
        "maxUses": config.get("maxUses") or DEEPSEEK_DEFAULT_MAX_USES,
        "recordRequest": lambda req: _record_deepseek_request(ctx, req),
    }
    if literal_key:
        options["apiKey"] = literal_key
    return options


def _make_resolver(ctx: AppContext, ref: str) -> Callable[[], Any]:
    """凭据解析器：credentials seam 优先，其次 launcher 环境（与 api-key 解析链一致）。"""

    async def resolve() -> Optional[str]:
        credentials = service_or_none(ctx, "credentials")
        if credentials is not None:
            resolved = await credentials.resolve(ref)
            if resolved is not None:
                return resolved.get("value")
        ambient = _env_value(ctx, ref)
        return ambient if ambient else None

    return resolve


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：注册 settings 区并挂 DeepSeek 搜索 provider。"""
    config = config or {}
    current: Callable[[], dict] = lambda: config

    install_settings_section(ctx, WEB_SEARCH_DEEPSEEK_SETTINGS_NAMESPACE, None, config, {
        "set_source": lambda source: None,  # dsh_py 的 settings 区直接提供当前值 thunk
        "on_change": lambda: None,          # 注册不携带已解析值：每次搜索现投影
    })

    def resolve_current() -> dict:
        section = ctx.settings.get(WEB_SEARCH_DEEPSEEK_SETTINGS_NAMESPACE) if ctx.has_service("settings") else None
        merged = dict(config)
        if isinstance(section, dict):
            merged.update(section)
        return _resolve_options(ctx, merged)

    web = service_or_none(ctx, "web")
    if web is None:
        raise RuntimeError("web-search-deepseek: the web service is not mounted (add dsh_py.services.web:apply)")
    web.register_search_provider(DeepSeekSearchProvider(resolve_current))


apply.inject = ["web"]  # 声明：本插件需要 web 服务（供 loader 拓扑排序）

__all__ = [
    "DEEPSEEK_PROVIDER_ID",
    "DEEPSEEK_DEFAULT_BASE_URL",
    "DEEPSEEK_DEFAULT_MODEL",
    "DEEPSEEK_DEFAULT_API_VERSION",
    "DEEPSEEK_DEFAULT_MAX_TOKENS",
    "DEEPSEEK_DEFAULT_MAX_USES",
    "WEB_SEARCH_DEEPSEEK_SETTINGS_NAMESPACE",
    "DeepSeekSearchProvider",
    "citation_snippets",
    "map_anthropic_response",
    "apply",
]

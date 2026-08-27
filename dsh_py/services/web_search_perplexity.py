"""``web-search-perplexity``：向 ``ctx.web`` 注册 Perplexity 搜索 provider。

对齐 dsh 的 ``@deepseek-ai/dsh-web-search-perplexity``：走 OpenAI 兼容
chat-completions 端点。生成答案映射为 ``content``；来源优先结构化
``search_results[]``，无则回退仅 URL 的 ``citations[]``。

适配（dsh_py 差异）：传输以懒加载 ``httpx`` 承担；取消经检查点 + 捕获
``asyncio.CancelledError`` → ``WEB_ABORTED``；``URL.canParse`` 以本地
``url_can_parse`` 近似。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from dsh_py.core.context import AppContext
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

PERPLEXITY_PROVIDER_ID = "perplexity"
PERPLEXITY_DEFAULT_BASE_URL = "https://api.perplexity.ai"
PERPLEXITY_DEFAULT_MODEL = "sonar"
PERPLEXITY_DEFAULT_MAX_TOKENS = 1024

_USER_AGENT = "deepseek-harness/0.0.1"
_DEFAULT_API_KEY_ENV = "PERPLEXITY_API_KEY"


def _env_value(ctx: AppContext, key: str) -> Optional[str]:
    """读 launcher 环境快照的取值（``{"value", ...}`` 或 None）。"""
    snapshot = launch_environment_of(ctx)
    entry = snapshot["get"](key)
    if entry is None:
        return None
    return entry.get("value")


def map_perplexity_result(result: dict) -> WebSearchSource:
    """映射一条结构化 Perplexity 搜索结果；空白字段省略而非置空。"""
    title = result.get("title")
    snippet = result.get("snippet")
    date = result.get("date")
    return WebSearchSource(
        url=result["url"],
        title=title if title else None,
        snippet=snippet if snippet else None,
        published_at=date if date else None,
    )


def map_perplexity_response(response: dict) -> WebSearchResult:
    """映射 Perplexity 响应包络。优先结构化 ``search_results[]``，仅当其缺席时
    回退到仅 URL 的 ``citations[]``；答案为空时省略 ``content``。"""
    choices = response.get("choices") or []
    content = ""
    if choices:
        content = (choices[0].get("message") or {}).get("content") or ""
    if response.get("search_results") is not None:
        sources = [map_perplexity_result(r) for r in response["search_results"]]
    else:
        sources = [WebSearchSource(url=u) for u in (response.get("citations") or [])]
    return WebSearchResult(
        sources=sources,
        truncated=False,
        content=content if content else None,
    )


class PerplexitySearchProvider(WebSearchProvider):
    """Perplexity 搜索 provider（id = ``perplexity``）。HTTP 重定向视为错误。"""

    id = PERPLEXITY_PROVIDER_ID

    def __init__(self, options: dict) -> None:
        self.options = options

    def available(self) -> bool:
        return (
            bool(self.options.get("apiKey"))
            and url_can_parse(self.options.get("baseURL", ""))
            and is_positive_integer(self.options.get("maxTokens"))
        )

    async def search(self, request: WebSearchRequest, signal: Any = None) -> WebSearchResult:
        throw_if_aborted(signal)

        import httpx  # 懒加载：缺依赖仅在使用时报错

        body: dict = {
            "model": self.options["model"],
            "max_tokens": self.options["maxTokens"],
            "messages": [{"role": "user", "content": request.query}],
        }
        if self.options.get("searchRecency") is not None:
            body["search_recency_filter"] = self.options["searchRecency"]
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.options['baseURL']}/chat/completions",
                    json=body,
                    headers={
                        "authorization": f"Bearer {self.options['apiKey']}",
                        "accept": "application/json",
                        "user-agent": _USER_AGENT,
                    },
                )
        except asyncio.CancelledError as exc:
            raise web_aborted(signal, exc) from exc
        except Exception as exc:  # noqa: BLE001
            raise WebError(f"Perplexity search request failed: {exc}", "WEB_PROVIDER_ERROR", cause=exc) from exc

        if response.status_code < 200 or response.status_code >= 300:
            message = f"Perplexity API error (HTTP {response.status_code})"
            try:
                parsed = response.json()
                err = parsed.get("error")
                detail = err if isinstance(err, str) else (err or {}).get("message") if isinstance(err, dict) else parsed.get("message")
                if detail:
                    message = detail
            except Exception:  # noqa: BLE001
                pass
            raise WebError(message, "WEB_PROVIDER_ERROR")

        try:
            payload = response.json()
            return map_perplexity_response(payload)
        except WebError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise WebError(
                f"Perplexity returned an unprocessable response body: {exc}",
                "WEB_PROVIDER_ERROR",
                cause=exc,
            ) from exc


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：向 ``ctx.web`` 注册 Perplexity 搜索 provider。"""
    config = config or {}
    options = {
        "apiKey": config.get("apiKey") or _env_value(ctx, _DEFAULT_API_KEY_ENV) or "",
        "baseURL": config.get("baseURL") or PERPLEXITY_DEFAULT_BASE_URL,
        "model": config.get("model") or PERPLEXITY_DEFAULT_MODEL,
        "maxTokens": config.get("maxTokens") or PERPLEXITY_DEFAULT_MAX_TOKENS,
    }
    if config.get("searchRecency") is not None:
        options["searchRecency"] = config["searchRecency"]
    web = service_or_none(ctx, "web")
    if web is None:
        raise RuntimeError("web-search-perplexity: the web service is not mounted (add dsh_py.services.web:apply)")
    web.register_search_provider(PerplexitySearchProvider(options))


apply.inject = ["web"]  # 声明：本插件需要 web 服务（供 loader 拓扑排序）

__all__ = [
    "PERPLEXITY_PROVIDER_ID",
    "PERPLEXITY_DEFAULT_BASE_URL",
    "PERPLEXITY_DEFAULT_MODEL",
    "PERPLEXITY_DEFAULT_MAX_TOKENS",
    "PerplexitySearchProvider",
    "map_perplexity_result",
    "map_perplexity_response",
    "apply",
]

"""``web-search-exa``：向 ``ctx.web`` 注册 Exa 搜索 provider。

对齐 dsh 的 ``@deepseek-ai/dsh-web-search-exa``：Exa 搜索 API（``POST /search`` +
highlight contents）。把第一条非空 highlight 映射为 ``snippet``，``publishedDate``
映射为 ``published_at``，无 snippet 的条目丢弃；Exa 不返回生成答案，故省略
``content``。

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

EXA_PROVIDER_ID = "exa"
EXA_DEFAULT_BASE_URL = "https://api.exa.ai"
EXA_DEFAULT_SEARCH_TYPE = "auto"
EXA_DEFAULT_HIGHLIGHTS_PER_RESULT = 1

_USER_AGENT = "deepseek-harness/0.0.1"
_DEFAULT_API_KEY_ENV = "EXA_API_KEY"


def _env_value(ctx: AppContext, key: str) -> Optional[str]:
    """读 launcher 环境快照的取值（``{"value", ...}`` 或 None）。"""
    snapshot = launch_environment_of(ctx)
    entry = snapshot["get"](key)
    if entry is None:
        return None
    return entry.get("value")


def map_exa_result(result: dict) -> Optional[WebSearchSource]:
    """映射一条 Exa 结果；无 snippet 条目返回 None（seam 无其他字段可派生，
    编造即为撒谎）。"""
    snippet = next((h for h in result.get("highlights") or [] if h.strip()), None)
    if snippet is None:
        return None
    title = result.get("title")
    published = result.get("publishedDate")
    return WebSearchSource(
        url=result["url"],
        title=title if title else None,
        snippet=snippet,
        published_at=published if published else None,
    )


def map_exa_response(response: dict) -> WebSearchResult:
    """映射 Exa 响应包络；snippet 缺失条目经 :func:`map_exa_result` 丢弃。"""
    sources = [s for s in (map_exa_result(r) for r in response.get("results") or []) if s is not None]
    return WebSearchResult(sources=sources, truncated=False)


class ExaSearchProvider(WebSearchProvider):
    """Exa 搜索 provider（id = ``exa``）。HTTP 重定向视为错误。"""

    id = EXA_PROVIDER_ID

    def __init__(self, options: dict) -> None:
        self.options = options

    def available(self) -> bool:
        return (
            bool(self.options.get("apiKey"))
            and url_can_parse(self.options.get("baseURL", ""))
            and is_positive_integer(self.options.get("highlightsPerResult"))
            and (self.options.get("numResults") is None or is_positive_integer(self.options["numResults"]))
        )

    async def search(self, request: WebSearchRequest, signal: Any = None) -> WebSearchResult:
        # 单次请求上限优先于配置默认；两者皆可缺省
        num_results = request.max_results if request.max_results is not None else self.options.get("numResults")
        throw_if_aborted(signal)

        import httpx  # 懒加载：缺依赖仅在使用时报错

        body: dict = {
            "query": request.query,
            "type": self.options["searchType"],
            "contents": {"highlights": {"highlightsPerUrl": self.options["highlightsPerResult"]}},
        }
        if num_results is not None:
            body["numResults"] = num_results
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.options['baseURL']}/search",
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
            raise WebError(f"Exa search request failed: {exc}", "WEB_PROVIDER_ERROR", cause=exc) from exc

        if response.status_code < 200 or response.status_code >= 300:
            message = f"Exa API error (HTTP {response.status_code})"
            try:
                parsed = response.json()
                detail = parsed.get("error") or parsed.get("message")
                if detail:
                    message = detail
            except Exception:  # noqa: BLE001
                pass
            raise WebError(message, "WEB_PROVIDER_ERROR")

        try:
            payload = response.json()
            return map_exa_response(payload)
        except WebError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise WebError(
                f"Exa returned an unprocessable response body: {exc}",
                "WEB_PROVIDER_ERROR",
                cause=exc,
            ) from exc


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：向 ``ctx.web`` 注册 Exa 搜索 provider。"""
    config = config or {}
    options = {
        "apiKey": config.get("apiKey") or _env_value(ctx, _DEFAULT_API_KEY_ENV) or "",
        "baseURL": config.get("baseURL") or EXA_DEFAULT_BASE_URL,
        "searchType": config.get("searchType") or EXA_DEFAULT_SEARCH_TYPE,
        "highlightsPerResult": config.get("highlightsPerResult") or EXA_DEFAULT_HIGHLIGHTS_PER_RESULT,
    }
    if config.get("numResults") is not None:
        options["numResults"] = config["numResults"]
    web = service_or_none(ctx, "web")
    if web is None:
        raise RuntimeError("web-search-exa: the web service is not mounted (add dsh_py.services.web:apply)")
    web.register_search_provider(ExaSearchProvider(options))


apply.inject = ["web"]  # 声明：本插件需要 web 服务（供 loader 拓扑排序）

__all__ = [
    "EXA_PROVIDER_ID",
    "EXA_DEFAULT_BASE_URL",
    "EXA_DEFAULT_SEARCH_TYPE",
    "EXA_DEFAULT_HIGHLIGHTS_PER_RESULT",
    "ExaSearchProvider",
    "map_exa_result",
    "map_exa_response",
    "apply",
]

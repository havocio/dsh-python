"""``web`` 能力 seam（``ctx.web``）：搜索与抓取的 provider 注册表 + 执行期选择。

对齐 dsh 的 ``@deepseek-ai/dsh-web``（Service Definition 角色）：本包只拥有
seam 的契约与选择语义，不拥有任何具体 provider。抓取 / 搜索共用同一条 seam，
因此 provider 选择、取消、错误与产品配置只有一个归属者，请求 / 结果类型各自独立。

选择语义（执行期解析，绝不依赖注册顺序）：
- 配置了 id 且已注册且 ``available()`` → 该 provider；
- 配置了 id 未注册 → ``WEB_PROVIDER_CONFIGURED_MISSING``；
- 配置了 id 已注册但不可用 → ``WEB_PROVIDER_CONFIGURED_UNAVAILABLE``；
- 未配置 id，恰好一个可用 provider → 自动选中；
- 未配置 id，多个可用 provider → ``WEB_PROVIDER_AMBIGUOUS``；
- 未配置 id，无可用 provider → ``WEB_PROVIDER_UNAVAILABLE``。

插件装配：``dsh_py.services.web:apply`` 实例化 ``WebRuntime``（经
:class:`dsh_py.core.service.Service` 基类自动提供 ``web`` 服务）。
``searchProvider`` / ``fetchProvider`` 配置可被环境变量 ``DSH_WEB_SEARCH_PROVIDER`` /
``DSH_WEB_FETCH_PROVIDER`` 覆盖（两者等价，不是隐藏优先级链）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, Union

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.llm import HarnessError


# ---------------------------------------------------------------------------
# 错误与词汇
# ---------------------------------------------------------------------------

class WebError(HarnessError):
    """类型化 web 错误：机器可路由的开放字符串 ``code`` + 可选 ``cause``。

    消费者必须容忍 provider 特定的 code；共享 code 覆盖不可用 / 缺失 / 无法
    使用 / 歧义 / 重复 provider、取消与 provider 失败。本地 fetch provider
    额外区分无效 / 被拦截 URL、重定向、尺寸与超时上限、不支持的 Content-Type。
    工具执行把 code 放进结构化错误文本回流给模型。
    """

    def __init__(self, message: str, code: str, cause: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.cause = cause


@dataclass(frozen=True)
class WebSearchRequest:
    """一次搜索请求。``max_results`` 是 seam 层下发的上限，返回时强制执行。"""

    query: str
    max_results: Optional[int] = None


@dataclass(frozen=True)
class WebSearchSource:
    """一条可引用来源。URL 恒有；其余字段按 provider 是否返回而存在。"""

    url: str
    title: Optional[str] = None
    snippet: Optional[str] = None
    published_at: Optional[str] = None


@dataclass(frozen=True)
class WebSearchResult:
    """归一化搜索结果。``content`` 为 provider 生成的答案文本（可选）。"""

    sources: list[WebSearchSource]
    truncated: bool = False
    content: Optional[str] = None


@dataclass(frozen=True)
class WebFetchRequest:
    """一次抓取请求。超时 / 格式 / 提取控制不属于本 seam（取消是执行参数）。"""

    url: str


@dataclass(frozen=True)
class WebFetchBody:
    """解码后的抓取正文：封闭判别联合（html / text），新 kind 需联动修改。"""

    kind: str  # 'html' | 'text'
    content: str


@dataclass(frozen=True)
class WebFetchResult:
    """归一化抓取结果。非 2xx 响应是结果而非错误（状态码属于资源状态）。"""

    url: str
    status_code: int
    body: WebFetchBody
    truncated: bool = False


class WebSearchProvider(Protocol):
    """一个搜索后端。``id`` 在搜索能力 kind 内唯一。"""

    id: str

    def available(self) -> bool:
        """廉价本地可用性检查；必须不做网络调用。"""
        ...

    async def search(self, request: WebSearchRequest, signal: Any = None) -> WebSearchResult:
        """执行一次搜索；尊重 ``signal`` 取消。"""
        ...


class WebFetchProvider(Protocol):
    """一个抓取后端。``id`` 在抓取能力 kind 内唯一。"""

    id: str

    def available(self) -> bool:
        """廉价本地可用性检查；必须不做网络调用。"""
        ...

    async def fetch(self, request: WebFetchRequest, signal: Any = None) -> WebFetchResult:
        """抓取一个 URL；尊重 ``signal`` 取消。"""
        ...


# ---------------------------------------------------------------------------
# 共享小工具（provider / 工具复用）
# ---------------------------------------------------------------------------

def throw_if_aborted(signal: Any) -> None:
    """在异步检查点抛出取消；``signal`` 为空或没有该方法时静默。"""
    if signal is not None:
        check = getattr(signal, "throw_if_aborted", None)
        if check is not None:
            check()


def web_aborted(signal: Any, fallback: Any = None) -> WebError:
    """构造 provider 的稳定取消错误，保留调用方 reason。"""
    reason = getattr(signal, "reason", None) if signal is not None else None
    cause = reason if reason is not None else fallback
    return WebError("web operation aborted", "WEB_ABORTED", cause=cause)


def is_positive_integer(value: Any) -> bool:
    """True 当 value 是正整数值（可发送给搜索 API 的请求上限）。"""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def url_can_parse(value: str) -> bool:
    """廉价本地配置检查：字符串可解析为绝对 URL。"""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme != "" and parts.netloc != ""


def service_or_none(ctx: AppContext, name: str) -> Any:
    """按名取已挂载服务；未挂载返回 None（消费者可选依赖守卫）。"""
    return getattr(ctx, name) if ctx.has_service(name) else None


# ---------------------------------------------------------------------------
# WebRuntime
# ---------------------------------------------------------------------------

class WebRuntime(Service):
    """web 访问服务。注册为 ``ctx.web``（每个 context 一个实例）。

    配置（``WebRuntimeConfig``）：``searchProvider`` / ``fetchProvider`` 钉死每个
    能力获胜的 provider id，均可省略（单可用 provider 自动选中）。操作级覆盖
    （环境变量）喂进同一批字段，不引入隐藏优先级链。
    """

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx, "web")
        config = config or {}
        self.search_provider_id: Optional[str] = config.get("searchProvider") or os.environ.get("DSH_WEB_SEARCH_PROVIDER")
        self.fetch_provider_id: Optional[str] = config.get("fetchProvider") or os.environ.get("DSH_WEB_FETCH_PROVIDER")
        self._search_providers: dict[str, WebSearchProvider] = {}
        self._fetch_providers: dict[str, WebFetchProvider] = {}

    # -- 注册 ---------------------------------------------------------------

    def register_search_provider(self, provider: WebSearchProvider) -> Any:
        """注册一个搜索 provider；重名抛 ``WEB_DUPLICATE_PROVIDER``。返回注销器。"""
        return self._register_provider(self._search_providers, provider)

    def register_fetch_provider(self, provider: WebFetchProvider) -> Any:
        """注册一个抓取 provider；重名抛 ``WEB_DUPLICATE_PROVIDER``。返回注销器。"""
        return self._register_provider(self._fetch_providers, provider)

    def _register_provider(self, store: dict, provider: Any) -> Any:
        pid = provider.id
        if pid in store:
            raise WebError(f'a web provider with id "{pid}" is already registered', "WEB_DUPLICATE_PROVIDER")
        store[pid] = provider

        def undo() -> None:
            if store.get(pid) is provider:
                store.pop(pid, None)

        # 挂到调用 fiber 的回收清单：作用域销毁时自动注销（与 dsh 的 effect 语义一致）。
        try:
            self.ctx.effect(undo, f"web.registerProvider({pid})")
        except Exception:  # noqa: BLE001 -- 无 fiber 环境（测试直挂）时仅手动注销
            pass
        return undo

    # -- 执行 ---------------------------------------------------------------

    async def search(self, request: WebSearchRequest, signal: Any = None) -> WebSearchResult:
        """经选中的 provider 执行一次搜索；结果按 ``request.max_results`` 封顶。"""
        provider = _resolve_provider(self._search_providers, self.search_provider_id)
        result = await provider.search(request, signal)
        return _cap_sources(result, request.max_results)

    async def fetch(self, request: WebFetchRequest, signal: Any = None) -> WebFetchResult:
        """经选中的 provider 抓取一个 URL；非 2xx 响应是结果不是抛出。"""
        provider = _resolve_provider(self._fetch_providers, self.fetch_provider_id)
        return await provider.fetch(request, signal)


def _resolve_provider(providers: dict, configured_id: Optional[str]) -> Any:
    """解析选中的 provider，否则抛对应的 :class:`WebError`。"""
    if configured_id is not None:
        provider = providers.get(configured_id)
        if provider is None:
            raise WebError(
                f'configured web provider "{configured_id}" is not registered',
                "WEB_PROVIDER_CONFIGURED_MISSING",
            )
        if not provider.available():
            raise WebError(
                f'configured web provider "{configured_id}" is registered but unavailable',
                "WEB_PROVIDER_CONFIGURED_UNAVAILABLE",
            )
        return provider
    usable = [p for p in providers.values() if p.available()]
    if not usable:
        raise WebError("no usable web provider is registered", "WEB_PROVIDER_UNAVAILABLE")
    if len(usable) > 1:
        ids = ", ".join(p.id for p in usable)
        raise WebError(
            f"multiple usable web providers are registered ({ids}); configure one explicitly",
            "WEB_PROVIDER_AMBIGUOUS",
        )
    return usable[0]


def _cap_sources(result: WebSearchResult, max_results: Optional[int]) -> WebSearchResult:
    """执行 ``max_results``：截断 ``sources[]`` 并置 ``truncated``。"""
    if max_results is None or len(result.sources) <= max_results:
        return result
    return WebSearchResult(
        sources=result.sources[:max_results],
        truncated=True,
        content=result.content,
    )


# ---------------------------------------------------------------------------
# 插件入口
# ---------------------------------------------------------------------------

def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：实例化 ``WebRuntime`` 并挂为 ``ctx.web``（基类自动 provide）。"""
    WebRuntime(ctx, config or {})


apply.provides = ["web"]  # 声明：本插件提供 web 服务（供 loader 拓扑排序）

__all__ = [
    "WebError",
    "WebSearchRequest",
    "WebSearchSource",
    "WebSearchResult",
    "WebFetchRequest",
    "WebFetchBody",
    "WebFetchResult",
    "WebSearchProvider",
    "WebFetchProvider",
    "WebRuntime",
    "throw_if_aborted",
    "web_aborted",
    "is_positive_integer",
    "url_can_parse",
    "service_or_none",
    "apply",
]

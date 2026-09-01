"""Web HTTP / 升级路由载体（host/webserver seam，对标 dsh 的 ``dsh-host-webserver``）。

提供 ``ctx.webServer``：一个轻量 HTTP 路由表 + WebSocket 升级路由表 + 唯一 fallback +
``index.html`` 变换管。它本身不伺服任何文件、不持有 harness 概念——其它插件在它之上注册
路由（``/api`` 桥、前端静态服务、HMR 等）。底层复用 ``websockets``（与网关同依赖），
``process_request`` 钩子负责 HTTP 分流、握手后由主 handler 处理升级路由。

匹配顺序（对齐 dsh）：精确路由 > 最长前缀路由 > fallback；升级路由精确匹配、未命中即关闭。

- :meth:`WebServer.register` —— 注册 ``exact``/``prefix`` HTTP 路由，返回 disposer；
- :meth:`WebServer.register_upgrade` —— 注册精确路径的 WebSocket 升级路由，返回 disposer；
- :meth:`WebServer.register_fallback` —— 注册唯一 fallback（重复注册抛错；未注册时未命中返回 404）；
- :meth:`WebServer.tap_index` —— 注册 ``index.html`` 变换；:meth:`WebServer.apply_index_taps` 顺序执行；
- :meth:`WebServer.start` / :meth:`WebServer.stop` —— 启动/停止监听（仅绑定 ``127.0.0.1`` 或 ``0.0.0.0``）；
- ``port`` / ``host`` 为只读属性：``host`` 读配置绑定地址、``port`` 读实际监听端口（``port=0`` 时由 OS 分配）。

与 dsh 的偏差：dsh 在激活时即监听；dsh_py 暴露服务后由调用方显式 :meth:`start`（网关接线属
apiproxy 范畴、暂未接），避免常规 boot 误绑端口。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service

# 允许绑定的地址（对齐 dsh 的默认 posture + 显式开放）
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "0.0.0.0"})

# HTTP 状态码 → 原因短语（websockets Response 需要 reason 串）
_REASON_PHRASES = {
    200: "OK",
    201: "Created",
    204: "No Content",
    301: "Moved Permanently",
    302: "Found",
    304: "Not Modified",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


@dataclass
class Request:
    """交给路由 handler 的请求视图（不含升级握手原始细节）。"""
    method: str = "GET"
    path: str = "/"          # 去掉 query 后的路径
    query: str = ""          # 原始 query 串（含前导 ``?``，可能为空）
    headers: dict = field(default_factory=dict)  # 小写键
    raw: Any = None          # websockets 原始 request


@dataclass
class Response:
    """路由 handler 返回的响应视图。"""
    status: int = 200
    headers: dict = field(default_factory=dict)
    body: Any = b""          # ``bytes`` 或 ``str``


RouteHandler = Callable[[Request], Any]          # 同步或异步，返回 Response
UpgradeHandler = Callable[[Any], Any]            # 接收 websocket，同步或异步


class WebServer(Service):
    """``ctx.webServer``：HTTP/升级路由载体。"""

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx, "webServer")
        config = config or {}
        self._config_host = str(config.get("host") or "127.0.0.1")
        self._config_port = int(config.get("port", 0) or 0)
        self._host = self._config_host
        self._port = self._config_port

        self._exact: dict[str, RouteHandler] = {}
        self._prefix: dict[str, RouteHandler] = {}
        self._upgrades: dict[str, UpgradeHandler] = {}
        self._fallback: Optional[RouteHandler] = None
        self._index_taps: list[Callable[[str], str]] = []

        self._server: Any = None
        self._cm: Any = None
        self._sockets: set[Any] = set()

    # ------------------------------------------------------------------ #
    # 路由注册
    # ------------------------------------------------------------------ #
    def register(self, kind: str, path: str, handler: RouteHandler) -> Callable[[], None]:
        """注册一条 HTTP 路由；返回 disposer（调用即注销）。

        :param kind: ``"exact"``（精确路径）或 ``"prefix"``（前缀匹配）。
        :param path: 路由路径（精确或前缀，如 ``"/api"``）。
        :param handler: ``(Request) -> Response``（可同步或异步）。
        :raises ValueError: kind 非法或同表内路径重复（路由表是组合级契约，碰撞即误配置）。
        """
        if kind not in ("exact", "prefix"):
            raise ValueError(f'route kind must be "exact" or "prefix", got {kind!r}')
        if not path or not path.startswith("/"):
            raise ValueError(f"route path must start with '/', got {path!r}")
        table = self._exact if kind == "exact" else self._prefix
        if path in table:
            raise ValueError(f"duplicate {kind} route: {path}")
        table[path] = handler

        def dispose() -> None:
            table.pop(path, None)

        return dispose

    def register_upgrade(self, path: str, handler: UpgradeHandler) -> Callable[[], None]:
        """注册一条 WebSocket 升级路由（精确路径）；返回 disposer。

        :raises ValueError: 路径重复。
        """
        if not path or not path.startswith("/"):
            raise ValueError(f"upgrade path must start with '/', got {path!r}")
        if path in self._upgrades:
            raise ValueError(f"duplicate upgrade route: {path}")
        self._upgrades[path] = handler

        def dispose() -> None:
            self._upgrades.pop(path, None)

        return dispose

    def register_fallback(self, handler: RouteHandler) -> None:
        """注册唯一 fallback handler（处理所有未命中命名路由的请求）。

        :raises RuntimeError: 已注册过 fallback。
        """
        if self._fallback is not None:
            raise RuntimeError("fallback handler is already registered")
        self._fallback = handler

    def tap_index(self, transform: Callable[[str], str]) -> None:
        """注册一个 ``index.html`` 变换；顺序在 :meth:`apply_index_taps` 中执行。"""
        self._index_taps.append(transform)

    def apply_index_taps(self, html: str) -> str:
        """把 html 体依次过一遍已注册的 index 变换。"""
        for transform in self._index_taps:
            html = transform(html)
        return html

    # ------------------------------------------------------------------ #
    # 属性（只读）
    # ------------------------------------------------------------------ #
    @property
    def port(self) -> int:
        """实际监听端口（``port=0`` 时由 OS 分配，start 后生效）。"""
        return self._port

    @property
    def host(self) -> str:
        """配置绑定地址（组合期事实；``127.0.0.1`` 默认，``0.0.0.0`` 显式开放）。"""
        return self._host

    # ------------------------------------------------------------------ #
    # 生命周期：启动 / 停止
    # ------------------------------------------------------------------ #
    async def start(self, host: Optional[str] = None, port: Optional[int] = None) -> Any:
        """启动监听；返回底层 websockets server（常驻，由 :meth:`stop` 关闭）。

        :param host: 覆盖配置绑定地址；仅 ``127.0.0.1`` / ``0.0.0.0``。
        :param port: 覆盖配置端口；``0`` 表示 OS 分配。
        :raises ValueError: 绑定地址不在允许集合。
        """
        import websockets

        bind_host = host or self._config_host
        if bind_host not in _ALLOWED_HOSTS:
            raise ValueError(
                f"webserver binds only 127.0.0.1 or 0.0.0.0, got {bind_host!r}"
            )
        bind_port = port if port is not None else self._config_port
        self._host = bind_host

        # websockets 的 ``serve`` 是 async context manager，需走 ``__aenter__`` 才真正
        # start_serving；直接 ``await`` 返回的 server 底层 sockets 未就绪。这里显式进入
        # CM 并保持开启，由 :meth:`stop` 走 ``__aexit__`` 关闭（与网关用法同源）。
        self._cm = websockets.serve(
            self._ws_handler,
            bind_host,
            bind_port,
            process_request=self._process_request,
            max_size=1 << 20,
        )
        self._server = await self._cm.__aenter__()
        # 读取实际监听端口（port=0 时为 OS 分配）
        sock = self._server.sockets[0]
        self._port = sock.getsockname()[1]
        return self._server

    async def stop(self) -> None:
        """关闭监听并断开所有升级 socket。"""
        if self._server is None:
            return
        for sock in list(self._sockets):
            try:
                await sock.close()
            except Exception:  # noqa: BLE001
                pass
        self._sockets.clear()
        try:
            await self._cm.__aexit__(None, None, None)
        finally:
            self._server = None
            self._cm = None
            self._port = 0  # 停止后不再监听

    def dispose(self) -> None:
        """服务释放：尽力异步关闭监听（dispose 为同步，故排入事件循环）。"""
        if self._server is not None:
            try:
                asyncio.ensure_future(self.stop())
            except RuntimeError:
                # 无运行中的事件循环（如解释器关闭期）→ 同步尽力关闭
                try:
                    self._server.close()
                except Exception:  # noqa: BLE001
                    pass
                self._server = None

    # ------------------------------------------------------------------ #
    # 内部：请求分流
    # ------------------------------------------------------------------ #
    def _is_upgrade(self, request: Any) -> bool:
        headers = getattr(request, "headers", None)
        if headers is None:
            return False
        if hasattr(headers, "get"):
            value = headers.get("upgrade")
            if value is None:
                value = headers.get("Upgrade")
            return bool(value) and "websocket" in str(value).lower()
        return False

    async def _process_request(self, connection: Any, request: Any) -> Any:
        """``websockets`` 的 ``process_request`` 钩子：HTTP 分流。

        升级请求返回 ``None`` 放行给握手；命中路由返回 ``Response`` 短路；否则走 fallback
        或 404。仅对 index 路径（``/`` / ``/index.html``）应用 index 变换。
        """
        from websockets.datastructures import Headers as WSHeaders
        from websockets.http11 import Response as WSResponse

        raw_path = request.path if not isinstance(request.path, bytes) else request.path.decode()
        path, _, query = raw_path.partition("?")

        # WebSocket 升级请求：放行给 _ws_handler 处理升级路由
        if self._is_upgrade(request):
            return None

        # 1) 精确路由
        handler = self._exact.get(path)
        if handler is not None:
            return self._build_response(await self._call(handler, path, query, request))

        # 2) 最长前缀路由
        best: Optional[str] = None
        for p in self._prefix:
            if path == p or path.startswith(p):
                if best is None or len(p) > len(best):
                    best = p
        if best is not None:
            return self._build_response(await self._call(self._prefix[best], path, query, request))

        # 3) fallback（未注册则 404）
        if self._fallback is not None:
            resp = await self._call(self._fallback, path, query, request)
            if path in ("/", "/index.html") and isinstance(resp.body, (bytes, str)):
                html = resp.body.decode() if isinstance(resp.body, bytes) else resp.body
                # 注意：此处用 dataclass Response（模块级），_build_response 负责转 websockets 类型
                resp = Response(resp.status, headers=resp.headers,
                                body=self.apply_index_taps(html).encode())
            return self._build_response(resp)
        return WSResponse(404, "Not Found",
                          WSHeaders([("Content-Type", "text/plain; charset=utf-8")]),
                          b"not found")

    async def _ws_handler(self, websocket: Any) -> None:
        """升级握手后的主 handler：按精确路径派发升级路由，未命中即关闭。"""
        raw_path = websocket.request.path if hasattr(websocket, "request") else "/"
        if isinstance(raw_path, bytes):
            raw_path = raw_path.decode()
        path = raw_path.partition("?")[0]
        handler = self._upgrades.get(path)
        if handler is None:
            await websocket.close()
            return
        self._sockets.add(websocket)
        try:
            result = handler(websocket)
            if asyncio.iscoroutine(result):
                await result
        finally:
            self._sockets.discard(websocket)

    async def _call(self, handler: RouteHandler, path: str, query: str, request: Any) -> Response:
        headers = getattr(request, "headers", None)
        header_dict = dict(headers) if hasattr(headers, "items") else {}
        req = Request(method=getattr(request, "method", "GET"),
                      path=path, query=query, headers=header_dict, raw=request)
        result = handler(req)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    def _build_response(self, resp: Response) -> Any:
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        body = resp.body
        if isinstance(body, str):
            body = body.encode()
        elif body is None:
            body = b""
        return Response(
            resp.status,
            _REASON_PHRASES.get(resp.status, "OK"),
            Headers(list(resp.headers.items())),
            body,
        )


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：注册 ``ctx.webServer`` 服务（不自动监听，由调用方显式 start）。

    dsh 在激活时即监听；dsh_py 暴露服务后留待网关接线（apiproxy 范畴）再 start，
    避免常规 boot 误绑端口。
    """
    WebServer(ctx, config or {})


apply.provides = ["webServer"]
apply.inject: list[str] = []

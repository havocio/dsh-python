"""Web 网关入口：把 harness 作为常驻网络服务暴露（对标 dsh 的 ``api/gateway``）。

用法::

    python -m dsh_py.gateway --port 8080                 # 真实模型（配置文件取 key）
    python -m dsh_py.gateway --port 8080 --mock          # 离线演示
    python -m dsh_py.gateway --port 8080 --mock --webui  # 同端口伺服浏览器前端

客户端连接 ``ws://localhost:8080``，说话与 stdio 版相同的 newline JSON-RPC
（JSON 文本消息帧）：``initialize`` / ``session/prompt`` / ``shutdown`` 请求 +
``session.event`` / ``session.status`` 通知。协议细节见
:mod:`dsh_py.api.protocol`（方法面见 :class:`dsh_py.api.server.HarnessSdkJsonRpcServer`）。

``--webui`` 用 ``websockets.serve`` 的 ``process_request`` 钩子在**同一端口**
伺服 :file:`dsh_py/webui/index.html`（单页浏览器前端，零依赖）：
浏览器打开 ``http://localhost:8080/`` 即得聊天界面，页面自动连
``ws://localhost:8080/ws``。

**装配与 CLI 同源**：boot 管线（bundle 核心服务 → 用户层 profile → --patch），
统一配置文件（--config）注入 ``appConfig``。依赖 ``websockets``（应用层）。
"""

from __future__ import annotations

import argparse
import asyncio
import http
import importlib.util
import logging
import os
import sys
from typing import Any

from dsh_py.api.websocket import WebSocketGatewayServer
from dsh_py.cli import MockAdapter, _DEFAULT_PROFILE, _load_profile_module
from dsh_py.config import load_app_config
from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, boot

#: webui 静态页（惰性加载；``--webui`` 时随请求读取）。
_WEBUI_INDEX: bytes | None = None


def _webui_index() -> bytes:
    """读取内置浏览器前端（:file:`dsh_py/webui/index.html`）。"""
    global _WEBUI_INDEX
    if _WEBUI_INDEX is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui", "index.html")
        with open(path, "rb") as file:
            _WEBUI_INDEX = file.read()
    return _WEBUI_INDEX


async def _webui_request(connection: Any, request: Any) -> Any:
    """``websockets.serve`` 的 ``process_request`` 钩子：同一端口分流 HTTP。

    - ``/`` 与 ``/index.html`` → 返回内置前端页面；
    - ``/ws`` → 返回 None，继续 WebSocket 握手；
    - 其余 → 404。

    :param connection: 服务器连接（websockets ≥16 的钩子第一参，未使用）。
    :param request: :class:`websockets.http11.Request`，取 ``.path`` 分流。
    :returns: ``websockets.http11.Response`` 或 None（放行 WebSocket）。
    """
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    path = request.path if not isinstance(request, str) else request
    if path in ("/", "/index.html"):
        return Response(
            200,
            "OK",
            Headers([("Content-Type", "text/html; charset=utf-8")]),
            _webui_index(),
        )
    if path == "/ws":
        return None
    return Response(
        404,
        "Not Found",
        Headers([("Content-Type", "text/plain; charset=utf-8")]),
        b"not found",
    )


async def run_server(
    port: int = 8080,
    host: str = "127.0.0.1",
    mock: bool = False,
    profile: str | None = None,
    config_file: str | None = None,
    patches: list[str] | None = None,
    max_connections: int = 64,
    webui: bool = False,
) -> None:
    """装配 harness 并启动常驻 WebSocket 网关。

    :param port: 监听端口。
    :param host: 绑定地址（默认仅本机；``0.0.0.0`` 开放局域网）。
    :param mock: 用内置 MockAdapter 替换默认 provider 与 deepseek-official 路由。
    :param profile: 用户层 profile 路径（缺省内置装配点）。
    :param config_file: 配置文件路径（缺省分层默认）。
    :param patches: overlay patch 文件列表。
    :param max_connections: 并发连接上限（超出拒绝）。
    :param webui: 同端口伺服浏览器前端（``http://host:port/``）。
    """
    ctx = AppContext()
    config = load_app_config(config_file)
    ctx.provide("appConfig", config)
    layers = [CORE_PROFILE]
    profile_path = profile or _DEFAULT_PROFILE
    if os.path.exists(profile_path):
        layers.append(_load_profile_module(profile_path))
    for patch_file in patches or []:
        if not os.path.exists(patch_file):
            raise FileNotFoundError(f"--patch 文件不存在：{patch_file}")
        layers.append(_load_profile_module(patch_file))
    boot(ctx, *layers)

    if mock:
        llm_cfg = config.get("llm") or {}
        provider = llm_cfg.get("provider") or "deepseek"
        for route in list(dict.fromkeys([provider, "deepseek-official"])):
            ctx.llm.register_adapter([route], MockAdapter(), replace=True)

    gateway = WebSocketGatewayServer(ctx)

    import websockets

    async def handler(websocket: Any) -> None:
        if len(gateway._connections) >= max_connections:
            await websocket.close(code=1013, reason="too many connections")
            return
        await gateway.handle_connection(websocket)

    kwargs: dict = {"max_size": 1 << 20}
    if webui:
        kwargs["process_request"] = _webui_request
    async with websockets.serve(handler, host, port, **kwargs):
        logger = logging.getLogger("dsh_py.gateway")
        logger.info("gateway listening on ws://%s:%s (mock=%s, webui=%s)", host, port, mock, webui)
        print(f"dsh_py gateway listening on ws://{host}:{port} (mock={mock})", file=sys.stderr)
        if webui:
            print(f"  webui: open http://{host}:{port}/ in a browser", file=sys.stderr)
        await asyncio.Future()  # 常驻，直到被信号终止


def main() -> None:
    parser = argparse.ArgumentParser(description="dsh_py Web 网关（常驻后端服务）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（0.0.0.0 开放局域网）")
    parser.add_argument("--mock", action="store_true", help="离线 mock 模型")
    parser.add_argument("--webui", action="store_true", help="同端口伺服浏览器前端（http://host:port/）")
    parser.add_argument("--profile", default=None, help="用户层 profile .py 路径")
    parser.add_argument("--config", default=None, help="配置文件 .py 路径")
    parser.add_argument("--patch", action="append", default=[], help="overlay patch .py 文件")
    parser.add_argument("--max-connections", type=int, default=64, help="并发连接上限")
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    args = parser.parse_args()

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(run_server(
            port=args.port,
            host=args.host,
            mock=args.mock,
            profile=args.profile,
            config_file=args.config,
            patches=args.patch,
            max_connections=args.max_connections,
            webui=args.webui,
        ))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

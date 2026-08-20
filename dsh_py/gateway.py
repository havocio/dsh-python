"""Web 网关入口：把 harness 作为常驻网络服务暴露（对标 dsh 的 ``api/gateway``）。

用法::

    python -m dsh_py.gateway --port 8080                 # 真实模型（配置文件取 key）
    python -m dsh_py.gateway --port 8080 --mock          # 离线演示

客户端连接 ``ws://localhost:8080``，说话与 stdio 版相同的 newline JSON-RPC
（JSON 文本消息帧）：``initialize`` / ``session/prompt`` / ``shutdown`` 请求 +
``session.event`` / ``session.status`` 通知。协议细节见
:mod:`dsh_py.api.protocol`（方法面见 :class:`dsh_py.api.server.HarnessSdkJsonRpcServer`）。

**装配与 CLI 同源**：boot 管线（bundle 核心服务 → 用户层 profile → --patch），
统一配置文件（--config）注入 ``appConfig``。依赖 ``websockets``（应用层）。
"""

from __future__ import annotations

import argparse
import asyncio
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


async def run_server(
    port: int = 8080,
    host: str = "127.0.0.1",
    mock: bool = False,
    profile: str | None = None,
    config_file: str | None = None,
    patches: list[str] | None = None,
    max_connections: int = 64,
) -> None:
    """装配 harness 并启动常驻 WebSocket 网关。

    :param port: 监听端口。
    :param host: 绑定地址（默认仅本机；``0.0.0.0`` 开放局域网）。
    :param mock: 用内置 MockAdapter 替换默认 provider 与 deepseek-official 路由。
    :param profile: 用户层 profile 路径（缺省内置装配点）。
    :param config_file: 配置文件路径（缺省分层默认）。
    :param patches: overlay patch 文件列表。
    :param max_connections: 并发连接上限（超出拒绝）。
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

    async with websockets.serve(handler, host, port, max_size=1 << 20):
        logger = logging.getLogger("dsh_py.gateway")
        logger.info("gateway listening on ws://%s:%s (mock=%s)", host, port, mock)
        print(f"dsh_py gateway listening on ws://{host}:{port} (mock={mock})", file=sys.stderr)
        await asyncio.Future()  # 常驻，直到被信号终止


def main() -> None:
    parser = argparse.ArgumentParser(description="dsh_py Web 网关（常驻后端服务）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（0.0.0.0 开放局域网）")
    parser.add_argument("--mock", action="store_true", help="离线 mock 模型")
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
        ))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

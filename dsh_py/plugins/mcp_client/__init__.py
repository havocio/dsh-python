"""MCP 客户端桥接插件（对标 dsh 的 ``mcp-client``）。

连接一个外部 MCP 服务器，把它的工具注册到 ``ctx.tools``，公开名为
``mcp__<serverName>__<rawName>``。每个插件实例连接一个服务器；在 profile 里
加载多个实例即可连接多个服务器。

生命周期 effect-scoped：插件卸载时断开连接、注销全部工具、释放 serverName
命名空间保留。dispose 后同一 serverName 可重新加载（热替换）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from dsh_py.core import schema as z
from dsh_py.plugins.mcp_client.bridge import (
    RECONNECT_DEFAULTS,
    resolve_reconnect_policy,
    start_connection,
)

logger = logging.getLogger("dsh_py.mcp-client")

name = "mcp-client"
inject = ["tools"]

DEFAULT_TOOL_CALL_TIMEOUT_MS = 60_000

# 每个应用内 serverName 的活跃保留集；重复命名空间在加载期报错
_active_server_names: set[str] = set()

Reconnect = z.object({
    "enabled": z.boolean().default(RECONNECT_DEFAULTS["enabled"]),
    "initialDelayMs": z.number(minimum=1).default(RECONNECT_DEFAULTS["initialDelayMs"]),
    "maxDelayMs": z.number(minimum=1).default(RECONNECT_DEFAULTS["maxDelayMs"]),
    "maxAttempts": z.integer(minimum=1).default(RECONNECT_DEFAULTS["maxAttempts"]),
})

_common_fields = {
    "serverName": z.string(),
    "toolCallTimeoutMs": z.integer(minimum=1).default(DEFAULT_TOOL_CALL_TIMEOUT_MS),
    "failOnStartupError": z.boolean().default(False),
    "reconnect": Reconnect,
}

Config = z.union([
    z.object({
        "transport": z.const("stdio"),
        "serverName": z.string(),
        "command": z.string(),
        "args": z.array(z.string()).default([]),
        "env": z.any().default({}),
        "cwd": z.string().default(""),
        **_common_fields,
    }),
    z.object({
        "transport": z.const("streamable-http"),
        "serverName": z.string(),
        "url": z.string(),
        "headers": z.any().default({}),
        **_common_fields,
    }),
])


def apply(ctx: Any, config: Any = None) -> None:
    """连接一个 MCP 服务器并发布其初始工具代。

    :param ctx: 插件上下文（``tools`` 服务必须已就绪）。
    :param config: 已校验的传输与服务器命名空间配置。
    """
    config = dict(config or {})
    policy = resolve_reconnect_policy(
        config.get("reconnect"), f"mcp-client({config['serverName']}): reconnect")

    # 保留命名空间：重复 serverName 使本实例加载失败，且不影响先前的实例
    if config["serverName"] in _active_server_names:
        raise RuntimeError(
            f'mcp-client: serverName "{config["serverName"]}" is already in use by another '
            "mcp-client instance — pick a unique serverName in the profile")
    _active_server_names.add(config["serverName"])
    ctx.effect(lambda: _active_server_names.discard(config["serverName"]),
               label="mcp-client.serverName")

    # 监督器拥有客户端/传输世代、重连循环与活跃工具注册。
    # ctx.effect 的 disposer 是同步调用，async dispose 需包一层投递到事件循环。
    connection = start_connection(ctx, config, policy)

    def _dispose_connection() -> None:
        asyncio.ensure_future(connection.dispose())

    ctx.effect(_dispose_connection, label="mcp-client.connection")

    # 首次连接结果异步等待：插件激活后工具很快可见；failOnStartupError 时
    # 初始失败以日志形式暴露（dsh_py 的插件加载是同步 apply，无法在激活期同步抛）。
    asyncio.ensure_future(_await_ready(connection, config))


async def _await_ready(connection, config: dict) -> None:
    """等待首次连接尝试结束；failOnStartupError 时失败记入日志。"""
    outcome = await connection.ready
    error = outcome.get("error")
    if error is not None and config.get("failOnStartupError"):
        logger.error(
            "mcp-client(%s): initial connection or tool synchronization failed: %s",
            config["serverName"], error)


apply.provides = []          # 不提供新服务，只把工具注册进 tools

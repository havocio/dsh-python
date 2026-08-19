"""MCP 工具桥与连接监督器（对标 dsh mcp-client 的 tools.ts + connection.ts）。

- **工具命名**：``mcp__<serverName>__<rawName>``，非法字符归一为 ``_``；
  归一后超 64 字符或发生替换时追加 12 位 SHA-256 身份哈希，保证不同
  ``(serverName, rawName)`` 永不坍缩成同一个公开名。
- **工具同步**（``sync_tools``）：两阶段——先 fetch（分页拉取 + 构建新一代
  定义，失败不动注册表），再 swap（卸载上一代 + 注册新一代；注册冲突整体
  回滚，contain / throw 二选一）。
- **连接监督器**（``start_connection``）：管理客户端/传输世代、把工具注册表
  与活动世代保持同步，连接断开时按有界指数退避重连（``maxAttempts`` 连续
  失败后放弃并注销工具；断开前持续运行超过稳定窗口则重置尝试预算）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re as _re
from typing import Any, Awaitable, Callable, Optional

from dsh_py.plugins.mcp_client.client import McpClient, McpError
from dsh_py.plugins.mcp_client.client import StdioTransport, StreamableHttpTransport

logger = logging.getLogger("dsh_py.mcp-client")

# --------------------------------------------------------------------------- #
# 工具命名（对齐 dsh 的 publicToolName）
# --------------------------------------------------------------------------- #
MAX_PUBLIC_NAME_LENGTH = 64
_INVALID_NAME_CHARS = _re.compile(r"[^A-Za-z0-9_-]")
_HASH_LENGTH = 12


def public_tool_name(server_name: str, raw_name: str) -> str:
    """派生面向模型的公开工具名（确定性纯函数）。"""
    joined = f"mcp__{server_name}__{raw_name}"
    normalized = _INVALID_NAME_CHARS.sub("_", joined)
    if normalized == joined and len(normalized) <= MAX_PUBLIC_NAME_LENGTH:
        return normalized
    digest = hashlib.sha256(f"{server_name}\0{raw_name}".encode("utf-8")).hexdigest()
    hash_suffix = digest[:_HASH_LENGTH]
    return f"{normalized[:MAX_PUBLIC_NAME_LENGTH - _HASH_LENGTH - 1]}_{hash_suffix}"


# --------------------------------------------------------------------------- #
# 结果文本化（对齐 dsh 的 extractText）
# --------------------------------------------------------------------------- #
def extract_text(mcp_content: list, tool_name: str) -> str:
    """把 MCP 内容块数组拼成单段文本（图片/音频/资源用占位符替换）。"""
    parts: list[str] = []
    for value in mcp_content:
        if not isinstance(value, dict):
            parts.append("[unsupported content type: unknown]")
            continue
        block_type = value.get("type")
        if block_type == "text":
            text = value.get("text")
            if text is not None:
                parts.append(str(text))
        elif block_type == "image":
            parts.append(f"[image: {value.get('mimeType', 'unknown')}, content discarded]")
        elif block_type == "audio":
            parts.append(f"[audio: {value.get('mimeType', 'unknown')}, content discarded]")
        elif block_type in ("resource", "resource_link"):
            parts.append("[resource: content discarded]")
        else:
            parts.append(f"[unsupported content type: {block_type}]")
    return "\n".join(parts) or f"({tool_name} returned no text content)"


# --------------------------------------------------------------------------- #
# 工具同步（对齐 dsh 的 syncTools）
# --------------------------------------------------------------------------- #
async def sync_tools(
    client: McpClient,
    ctx: Any,
    server_name: str,
    tool_call_timeout_ms: int,
    previous: dict,
    registration_failure: str = "contain",
) -> dict:
    """把服务器的工具列表同步进 harness 工具注册表；返回公开名 → 注销器。

    两阶段保证替换安全：fetch 失败不动注册表；swap 阶段的注册冲突整体回滚
    （模型要么看到整代工具，要么一个都不见）。
    """
    # Phase 1: fetch（分页拉取，构建新一代定义）
    definitions: dict[str, dict] = {}
    cursor: Optional[str] = None
    while True:
        tools, cursor = await client.list_tools(cursor)
        for tool in tools:
            public_name = public_tool_name(server_name, tool.get("name", ""))
            if public_name in definitions:
                raise McpError(
                    f'mcp-client({server_name}): server listed tool "{tool.get("name")}" '
                    "more than once — invalid tool list")
            raw_name = tool.get("name", "")
            definitions[public_name] = {
                "name": public_name,
                "description": tool.get("description") or "",
                "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
                "handler": _make_handler(client, raw_name, tool_call_timeout_ms),
            }
        if not cursor:
            break

    # Phase 2: swap（先卸载上一代，再注册新一代）
    for dispose in previous.values():
        dispose()
    disposers: dict[str, Any] = {}
    try:
        for public_name, definition in definitions.items():
            ctx.tools.register(
                name=definition["name"],
                description=definition["description"],
                parameters=definition["parameters"],
                handler=definition["handler"],
            )
            disposers[public_name] = _make_unregisterer(ctx, public_name)
    except Exception as exc:  # noqa: BLE001 - 冲突整体回滚
        for dispose in disposers.values():
            dispose()
        logger.error("mcp-client(%s): tool registration failed, no tools registered: %s",
                     server_name, exc)
        if registration_failure == "throw":
            raise
        return {}
    return disposers


def _make_unregisterer(ctx: Any, public_name: str):
    """构造一个注销器：把同名工具从注册表移除（dsh_py 的 tools 无 unregister，
    用覆盖为空 handler 的方式实现「停用」，简化：直接删除内部表项）。"""
    tools_service = ctx.tools
    internal = getattr(tools_service, "_tools", None)

    def dispose() -> None:
        if internal is not None:
            internal.pop(public_name, None)

    return dispose


def _make_handler(client: McpClient, raw_name: str, tool_call_timeout_ms: int):
    """构造执行器：调 MCP tools/call（raw 名），结果文本化，isError → 错误回流。"""

    async def handler(arguments: dict) -> tuple[str, bool]:
        # 模型参数可能不是 dict（坏输出），兜底为 {} 让服务器报缺失参数
        args_obj = arguments if isinstance(arguments, dict) else {}
        try:
            result = await client.call_tool(
                raw_name, args_obj, timeout=tool_call_timeout_ms / 1000.0)
        except McpError as exc:
            return f"MCP 工具 {raw_name!r} 调用失败：{exc}", True
        content = result.get("content")
        if not isinstance(content, list):
            rendered = result.get("toolResult")
            text = json_dumps(rendered) if rendered is not None else "(no output)"
            if result.get("isError") is True:
                return text, True
            return text, False
        text = extract_text(content, raw_name)
        if result.get("isError") is True:
            return text, True
        return text, False

    return handler


def json_dumps(value: Any) -> str:
    """JSON 序列化（容错 fallback 到 str）。"""
    import json
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


# --------------------------------------------------------------------------- #
# 重连策略（对齐 dsh 的 resolveReconnectPolicy / RECONNECT_DEFAULTS）
# --------------------------------------------------------------------------- #
RECONNECT_DEFAULTS = {
    "enabled": True,
    "initialDelayMs": 500,
    "maxDelayMs": 30_000,
    "maxAttempts": 10,
}
MAX_TIMER_DELAY_MS = 2**31 - 1


def resolve_reconnect_policy(config: Optional[dict], path: str) -> dict:
    """把原始重连配置解析成冻结策略；非法配置在加载期 fail loud。"""
    if config is not None:
        for key in config:
            if key not in RECONNECT_DEFAULTS:
                raise ValueError(f"{path}.{key} is not a reconnect option")
    enabled = config.get("enabled", RECONNECT_DEFAULTS["enabled"]) if config else RECONNECT_DEFAULTS["enabled"]
    initial_delay = config.get("initialDelayMs", RECONNECT_DEFAULTS["initialDelayMs"]) if config else RECONNECT_DEFAULTS["initialDelayMs"]
    max_delay = config.get("maxDelayMs", RECONNECT_DEFAULTS["maxDelayMs"]) if config else RECONNECT_DEFAULTS["maxDelayMs"]
    max_attempts = config.get("maxAttempts", RECONNECT_DEFAULTS["maxAttempts"]) if config else RECONNECT_DEFAULTS["maxAttempts"]
    if not isinstance(initial_delay, (int, float)) or initial_delay <= 0 or initial_delay > MAX_TIMER_DELAY_MS:
        raise ValueError(f"{path}.initialDelayMs must be a positive finite number no greater than {MAX_TIMER_DELAY_MS}")
    if not isinstance(max_delay, (int, float)) or max_delay <= 0 or max_delay > MAX_TIMER_DELAY_MS:
        raise ValueError(f"{path}.maxDelayMs must be a positive finite number no greater than {MAX_TIMER_DELAY_MS}")
    if initial_delay > max_delay:
        raise ValueError(f"{path}.initialDelayMs must be less than or equal to maxDelayMs")
    if not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError(f"{path}.maxAttempts must be a positive integer")
    return {
        "enabled": enabled,
        "initialDelayMs": initial_delay,
        "maxDelayMs": max_delay,
        "maxAttempts": max_attempts,
    }


# --------------------------------------------------------------------------- #
# 连接监督器（对齐 dsh 的 startConnection）
# --------------------------------------------------------------------------- #
class ConnectionHandle:
    """一个插件实例的受监督连接句柄。"""

    def __init__(self, ready: Awaitable, dispose: Callable[[], Awaitable[None]]) -> None:
        self.ready = ready
        self._dispose = dispose

    async def dispose(self) -> None:
        await self._dispose()


def _create_client(config: dict) -> McpClient:
    """按配置构造传输 + 客户端。"""
    if config["transport"] == "stdio":
        transport = StdioTransport(
            command=config["command"],
            args=config.get("args", []),
            env=config.get("env", {}),
            cwd=config.get("cwd") or None,
        )
    else:
        transport = StreamableHttpTransport(
            url=config["url"],
            headers=config.get("headers", {}),
        )
    return McpClient(transport)


def start_connection(ctx: Any, config: dict, policy: dict) -> ConnectionHandle:
    """为一个 MCP 服务器启动受监督连接并按策略保持存活。

    :returns: ``ready``（首次连接尝试结束即 settle）与 ``dispose``（停止重连、
        关闭活动客户端、等待在途工作静默、注销本服务器全部工具）。
    """
    label = f"mcp-client({config['serverName']})"
    registration_failure = "throw" if config.get("failOnStartupError") else "contain"
    opts = {
        "server_name": config["serverName"],
        "tool_call_timeout_ms": config.get("toolCallTimeoutMs", 60_000),
        "registration_failure": registration_failure,
    }

    disposed = False
    client: Optional[McpClient] = None          # 当前世代（连接中或已连接）
    client_ready: Optional[asyncio.Event] = None
    disposers: dict = {}                        # 本服务器现存的工具注销器
    reconnect_timer: Optional[asyncio.Task] = None
    failed_attempts = 0
    connected_at: Optional[float] = None
    first_attempt_error: Optional[Exception] = None

    # 串行化所有 sync_tools 调用（避免两代 swap 交错双 dispose）：
    # sync_chain 记录「上一次同步的完成」，新同步先等它（失败也继续）。
    sync_chain: Optional[asyncio.Future] = None

    def enqueue_sync(gen_client: McpClient) -> asyncio.Future:
        nonlocal disposers, sync_chain
        previous = sync_chain

        async def run() -> None:
            nonlocal disposers
            if previous is not None:
                try:
                    await previous
                except Exception:
                    pass  # 前一次失败不影响本次同步
            if disposed or client is not gen_client:
                return
            disposers = await sync_tools(
                gen_client, ctx, opts["server_name"],
                opts["tool_call_timeout_ms"], disposers,
                opts["registration_failure"],
            )

        task = asyncio.ensure_future(run())
        # 链尾吞掉失败：后续同步 await 它不会连环抛
        sync_chain = asyncio.ensure_future(_swallow(task))
        return task

    async def _swallow(awaitable: Awaitable) -> None:
        try:
            await awaitable
        except Exception:
            pass

    def schedule_reconnect() -> None:
        nonlocal failed_attempts, connected_at, reconnect_timer
        lost_established = connected_at is not None
        if not policy["enabled"]:
            logger.error(
                "%s: %s", label,
                "connection lost and reconnect is disabled — registered tools will fail"
                if lost_established else
                "connection failed and reconnect is disabled — no tools were registered")
            return
        # 断开前持续超过稳定窗口（= maxDelayMs，最长退避间距）→ 结束上次故障
        if connected_at is not None and _now_ms() - connected_at >= policy["maxDelayMs"]:
            failed_attempts = 0
        connected_at = None
        failed_attempts += 1
        if failed_attempts > policy["maxAttempts"]:
            for dispose in disposers.values():
                dispose()
            disposers.clear()
            logger.error(
                "%s: giving up after %d consecutive failed reconnect attempts — tools unregistered; "
                "reload the plugin or restart to reconnect",
                label, policy["maxAttempts"])
            return
        delay_ms = min(policy["maxDelayMs"], policy["initialDelayMs"] * 2 ** (failed_attempts - 1))
        action = "connection lost; reconnecting" if lost_established else "connection failed; retrying"
        logger.warning("%s: %s in %dms (attempt %d/%d)",
                       label, action, delay_ms, failed_attempts, policy["maxAttempts"])

        async def _retry() -> None:
            await asyncio.sleep(delay_ms / 1000.0)
            await connect_generation(startup=False)

        reconnect_timer = asyncio.ensure_future(_retry())

    async def connect_generation(startup: bool) -> None:
        nonlocal client, client_ready, connected_at, first_attempt_error
        gen_client = _create_client(config)
        ready_event = asyncio.Event()
        client = gen_client
        client_ready = ready_event

        # 注册工具列表变更通知 → 重同步
        async def on_tools_changed(params: dict) -> None:
            if disposed or client is not gen_client:
                return
            logger.info("%s: tool list changed, re-syncing", label)
            try:
                await enqueue_sync(gen_client)
            except Exception:
                if not disposed:
                    logger.error("%s: tool re-sync failed: %s", label, _exc_text())

        gen_client.on_notification("notifications/tools/list_changed", on_tools_changed)

        try:
            await gen_client.connect()
            if client is not gen_client:
                return
            # 初始同步：失败即视为本次连接尝试失败（fetch 失败不动注册表）
            task = enqueue_sync(gen_client)
            await task
        except Exception as exc:  # noqa: BLE001
            if first_attempt_error is None:
                first_attempt_error = exc
            if client is gen_client:
                logger.warning("%s: connection attempt failed: %s", label, exc)
            try:
                await gen_client.close()
            except Exception:
                pass
            if client is not gen_client:
                return
            client = None
            client_ready = None
            schedule_reconnect()
            return
        if client is not gen_client:
            return
        connected_at = _now_ms()
        ready_event.set()

    def _now_ms() -> float:
        import time
        return time.time() * 1000

    # 首次连接（startup=true）——ready 在首次尝试结束后 settle
    settling: asyncio.Task = asyncio.ensure_future(connect_generation(startup=True))

    async def ready() -> dict:
        await settling
        if client is not None:
            return {}
        return {"error": first_attempt_error or Exception(f"{label}: initial connection failed")}

    async def dispose() -> None:
        nonlocal disposed, client, client_ready, reconnect_timer, disposers
        disposed = True
        if reconnect_timer is not None:
            reconnect_timer.cancel()
            reconnect_timer = None
        current = client
        client = None
        client_ready = None
        if current is not None:
            # 关闭活动客户端（加超时保护：即使 transport 卡死也继续注销工具）
            try:
                await asyncio.wait_for(current.close(), timeout=6.0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s: client close during dispose failed: %s", label, exc)
        try:
            await settling
            await sync_chain
        except Exception:
            pass
        for dispose in disposers.values():
            dispose()
        disposers.clear()

    return ConnectionHandle(ready=ready(), dispose=dispose)

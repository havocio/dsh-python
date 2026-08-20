"""WebSocket 网关：把 harness 的 SDK JSON-RPC 方法面暴露到网络上。

复用 :mod:`dsh_py.api.server` 的 :class:`HarnessSdkJsonRpcServer`（方法面不变：
initialize / session/prompt / shutdown + session.event / session.status 通知），
只把传输层从 stdio 行分隔换成 WebSocket 帧：

- :class:`JsonRpcWebSocketTransport` —— 与 :class:`~dsh_py.api.protocol.JsonRpcLineTransport`
  同款接口（``on_request`` / ``on_notification`` / ``notify`` / ``start`` /
  ``close``），帧即 JSON 文本消息；帧判定逻辑与行版本完全一致
  （id+method=请求 / id alone=响应 / method alone=通知，畸形帧忽略）。
- :class:`WebSocketGatewayServer` —— 每连接一个 transport + server 实例
  （各自订阅 ctx 事件并 notify 本连接），连接断开自动清理。

依赖 ``websockets``（仅应用层；框架本体仍零依赖）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

from dsh_py.api.protocol import _parse_frame, JsonRpcResponseError
from dsh_py.api.server import HarnessSdkJsonRpcServer

logger = logging.getLogger("dsh_py.gateway")

# 服务器→客户端请求（客户端一般不处理；未知方法回 -32601）
# 单帧大小上限（拒绝超大帧，保护内存）
MAX_FRAME_BYTES = 1 << 20


class JsonRpcWebSocketTransport:
    """WebSocket 版 JSON-RPC 端点（与行传输同接口，可互换）。

    :param send: 发送一条 JSON 文本消息的协程（``async (str) -> None``）。
    """

    def __init__(self, send: Callable[[str], Awaitable[None]]) -> None:
        self._send = send
        self._request_handler: Optional[Callable[[str, dict], Awaitable[Any]]] = None
        self._notification_handler: Optional[Callable[[str, dict], None]] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._closed = False
        self._reader_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """占位：WS 传输由外部接收循环驱动（:meth:`on_message`），无需自起任务。"""

    def on_message(self, raw: str) -> None:
        """接收一条消息（由 WebSocket 读循环调用；帧判定与行版本一致）。"""
        frame = _parse_frame(raw)
        if frame is None:
            return
        frame_id = frame.get("id")
        method = frame.get("method")
        params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
        if (isinstance(frame_id, (str, int)) and not isinstance(frame_id, bool)) and isinstance(method, str):
            asyncio.ensure_future(self._handle_incoming_request(frame_id, method, params))
            return
        if isinstance(frame_id, (str, int)) and not isinstance(frame_id, bool):
            self._handle_incoming_response(frame_id, frame)
            return
        if isinstance(method, str) and self._notification_handler is not None:
            self._notification_handler(method, params)

    async def close(self) -> None:
        """拒绝全部 pending 请求（连接已断开）。"""
        if self._closed:
            return
        self._closed = True
        self._fail_pending(RuntimeError("WebSocket transport closed"))

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    async def request(self, method: str, params: Optional[dict] = None,
                      timeout_ms: Optional[int] = None) -> Any:
        """发送请求并等待响应；对端 error 帧抛 :class:`JsonRpcResponseError`。"""
        if self._closed:
            raise RuntimeError("WebSocket transport closed")
        import uuid

        request_id = f"req_{uuid.uuid4().hex}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        try:
            if timeout_ms is not None:
                return await asyncio.wait_for(future, timeout_ms / 1000)
            return await future
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            raise

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        """发送通知（fire-and-forget；写失败记录日志）。"""
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        asyncio.ensure_future(self._write(message))

    async def _write(self, message: dict) -> None:
        try:
            await self._send(json.dumps(message, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - 写失败即连接断开
            self._fail_pending(RuntimeError(f"WebSocket write failed: {exc}"))
            raise RuntimeError(f"WebSocket write failed: {exc}") from exc

    # ------------------------------------------------------------------ #
    # 处理
    # ------------------------------------------------------------------ #
    def on_request(self, handler: Callable[[str, dict], Awaitable[Any]]) -> None:
        """注册请求 handler（替换式）。返回的 result 写为响应；抛错写 -32603。"""
        self._request_handler = handler

    def on_notification(self, handler: Callable[[str, dict], None]) -> None:
        """注册通知 handler（替换式）；未注册的通知丢弃。"""
        self._notification_handler = handler

    async def _handle_incoming_request(self, request_id: Any, method: str, params: dict) -> None:
        handler = self._request_handler
        if handler is None:
            await self._write_error(request_id, -32601, f"method not found: {method}")
            return
        try:
            result = await handler(method, params)
            await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as exc:  # noqa: BLE001 - 与 dsh 一致：handler 失败 → -32603
            await self._write_error(request_id, -32603, str(exc))

    def _handle_incoming_response(self, request_id: Any, frame: dict) -> None:
        future = self._pending.pop(str(request_id), None)
        if future is None or future.done():
            return
        error = frame.get("error")
        if isinstance(error, dict):
            future.set_exception(JsonRpcResponseError(
                error.get("code") if isinstance(error.get("code"), int) else None,
                error.get("message") if isinstance(error.get("message"), str) else "JSON-RPC error",
                error.get("data"),
            ))
            return
        future.set_result(frame.get("result"))

    async def _write_error(self, request_id: Any, code: int, message: str) -> None:
        await self._write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()


class WebSocketGatewayServer:
    """常驻 WebSocket 网关：每连接一个 :class:`HarnessSdkJsonRpcServer`。

    事件订阅是连接粒度的：每连接各自订阅 ctx 的 ``session/event`` 与
    ``agent/status`` 并推送到本连接（对齐 stdio 版语义——每个连接都能看到
    运行中的会话事件流）。连接断开自动退订并清理。

    :param ctx: 已装配的 :class:`AppContext`（含 agents / sessions / llm）。
    :param handler: 可选的连接级请求包装（如鉴权）；缺省直连 server。
    """

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._connections: dict[int, dict] = {}
        self._conn_serial = 0

    async def handle_connection(self, websocket: Any) -> None:
        """处理一条连接：握手后进入读循环，直到断开或 shutdown。"""
        transport = JsonRpcWebSocketTransport(send=websocket.send)
        server = HarnessSdkJsonRpcServer(self.ctx, transport)
        connection: dict = {"transport": transport, "server": server, "ws": websocket}
        connection_id = self._conn_serial
        self._conn_serial += 1
        self._connections[connection_id] = connection
        transport.on_request(server.handle_request)
        try:
            async for raw in websocket:
                transport.on_message(raw)
        except Exception as exc:  # noqa: BLE001 - 连接异常按断开处理
            logger.debug("gateway connection closed: %s", exc)
        finally:
            self._connections.pop(connection_id, None)
            await server.shutdown()
            await transport.close()

    def connection_count(self) -> int:
        return len(self._connections)

    async def close(self) -> None:
        """关闭全部连接（服务器停机）。"""
        for connection in list(self._connections.values()):
            ws = connection["ws"]
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._connections.clear()

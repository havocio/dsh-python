"""SDK 运行时协议：newline-delimited JSON-RPC 2.0（对标 dsh 的 ``sdk-protocol/transport``）。

帧判定与 dsh 完全一致：

- ``{"jsonrpc":"2.0","id":...,"method":...,"params":{...}}`` —— 请求；
- ``{"jsonrpc":"2.0","id":...,"result":...}`` / ``{"jsonrpc":"2.0","id":...,"error":{...}}`` —— 响应；
- ``{"jsonrpc":"2.0","method":...,"params":{...}}`` —— 通知。

畸形 JSON 行与空行忽略；未注册 handler 的请求回 ``-32601``，handler 抛错回
``-32603``（消息原样透传）。传输可注入 reader/writer 回调（生产用 stdin/stdout，
测试用内存管道）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import uuid
from typing import Any, Awaitable, Callable, Optional

# 请求 id 前缀（对齐 dsh：req_<uuid 去横线>）
REQUEST_ID_PREFIX = "req_"


class JsonRpcResponseError(Exception):
    """对端返回了 JSON-RPC error 帧。"""

    def __init__(self, code: Optional[int], message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class JsonRpcProtocolError(Exception):
    """传输层失败：写入失败或通道关闭。"""


# reader：async 返回一行（不含换行），None = EOF
Reader = Callable[[], Awaitable[Optional[str]]]
# writer：同步写一行（含换行）
Writer = Callable[[str], None]


def stdin_reader() -> Reader:
    """生产默认 reader：daemon 线程读 stdin 一行（不阻塞解释器退出）。

    不能用 ``run_in_executor``：线程池 worker 非 daemon，客户端在 shutdown 后
    仍保持 stdin 打开时，进程退出会被阻塞在 readline 上（已踩坑）。
    """
    queue: asyncio.Queue = asyncio.Queue()

    def _read() -> None:
        try:
            while True:
                line = sys.stdin.readline()
                queue.put_nowait(line)
                if not line:  # EOF
                    break
        except Exception:  # noqa: BLE001 - 读失败按 EOF 处理
            queue.put_nowait("")

    threading.Thread(target=_read, daemon=True, name="dsh-stdin").start()

    async def read_line() -> Optional[str]:
        line = await queue.get()
        return line if line else None  # '' = EOF

    return read_line


def stdout_writer() -> Writer:
    """生产默认 writer：写 stdout 并立即 flush（stdout 只承载协议帧）。"""

    def write(line: str) -> None:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    return write


def _parse_frame(line: str) -> Optional[dict]:
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(message, dict):
        return None
    return message


class JsonRpcLineTransport:
    """行分隔 JSON-RPC 2.0 端点（asyncio 版）。

    :param reader: 读取下一行（None = EOF）；缺省用 :func:`stdin_reader`。
    :param writer: 写一行；缺省用 :func:`stdout_writer`。
    """

    def __init__(self, reader: Optional[Reader] = None, writer: Optional[Writer] = None) -> None:
        self._reader = reader or stdin_reader()
        self._writer = writer or stdout_writer()
        self._request_handler: Optional[Callable[[str, dict], Awaitable[Any]]] = None
        self._notification_handler: Optional[Callable[[str, dict], None]] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._task: Optional[asyncio.Task] = None
        self._closed = False
        self._eof_seen = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """挂起后台读循环（幂等）。"""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._read_loop(), name="dsh-jsonrpc-read")

    @property
    def eof(self) -> bool:
        """输入已到 EOF（对端关闭 stdin）；主循环可据此退出。"""
        return self._eof_seen

    async def close(self) -> None:
        """停止读循环并拒绝全部 pending 请求。"""
        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._fail_pending(JsonRpcProtocolError("JSON-RPC transport closed"))

    async def _read_loop(self) -> None:
        while not self._closed:
            try:
                line = await self._reader()
            except Exception:
                break
            if line is None:  # EOF（reader 契约：None = 流结束）
                self._eof_seen = True
                break
            line = line.strip()
            if not line:
                continue  # 空行忽略（对齐 dsh 的 drainLines）
            await self._handle_line(line)
        if not self._closed:
            self._fail_pending(JsonRpcProtocolError("JSON-RPC input closed"))

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    async def request(self, method: str, params: Optional[dict] = None,
                      timeout_ms: Optional[int] = None) -> Any:
        """发送请求并等待响应；对端 error 帧抛 :class:`JsonRpcResponseError`。"""
        if self._closed:
            raise JsonRpcProtocolError("JSON-RPC transport closed")
        request_id = f"{REQUEST_ID_PREFIX}{uuid.uuid4().hex}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
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
        """发送通知（无 params 时不带 params 成员）。"""
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def _write(self, message: dict) -> None:
        try:
            self._writer(json.dumps(message, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - 写失败统一为传输错误
            self._fail_pending(JsonRpcProtocolError(f"JSON-RPC write failed: {exc}"))
            raise JsonRpcProtocolError(f"JSON-RPC write failed: {exc}") from exc

    # ------------------------------------------------------------------ #
    # 处理
    # ------------------------------------------------------------------ #
    def on_request(self, handler: Callable[[str, dict], Awaitable[Any]]) -> None:
        """注册请求 handler（替换式）。返回的 result 写为响应；抛错写 -32603。"""
        self._request_handler = handler

    def on_notification(self, handler: Callable[[str, dict], None]) -> None:
        """注册通知 handler（替换式）；未注册的通知丢弃。"""
        self._notification_handler = handler

    async def _handle_line(self, line: str) -> None:
        frame = _parse_frame(line)
        if frame is None:
            return
        frame_id = frame.get("id")
        method = frame.get("method")
        params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
        if (isinstance(frame_id, (str, int)) and not isinstance(frame_id, bool)) and isinstance(method, str):
            await self._handle_incoming_request(frame_id, method, params)
            return
        if isinstance(frame_id, (str, int)) and not isinstance(frame_id, bool):
            self._handle_incoming_response(frame_id, frame)
            return
        if isinstance(method, str) and self._notification_handler is not None:
            self._notification_handler(method, params)

    async def _handle_incoming_request(self, request_id: Any, method: str, params: dict) -> None:
        handler = self._request_handler
        if handler is None:
            self._write_error(request_id, -32601, f"method not found: {method}")
            return
        try:
            result = await handler(method, params)
            self._write({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as exc:  # noqa: BLE001 - 与 dsh 一致：handler 失败 → -32603
            self._write_error(request_id, -32603, str(exc))

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

    def _write_error(self, request_id: Any, code: int, message: str) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

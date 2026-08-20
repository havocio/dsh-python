"""最小 MCP streamable-http server（测试脚手架，零额外依赖）。

对标 MCP ``Streamable HTTP`` 传输的简化子集，仅供 dsh_py 的
``StreamableHttpTransport`` 端到端验证使用：

- **POST /mcp**：处理 ``initialize`` / ``notifications/initialized`` /
  ``tools/list`` / ``tools/call``，返回 JSON（带 ``mcp-session-id`` 响应头
  + 显式 ``Content-Length``）；无 id 的通知（POST 形式）返回 ``202``。
- **GET /mcp**：返回 SSE 长连接（``text/event-stream``），持续推送
  server → client 通知（``notifications/tools/list_changed`` 等），并周期
  发送保活注释。连接关闭即结束。

实现用标准库 ``http.server``（零第三方依赖）；server 在后台守护线程运行，
每个 ``MockStreamableHttpServer`` 实例拥有独立的会话表与通知队列，可并发
起多个互不干扰。
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO: dict[str, Any] = {
    "protocolVersion": PROTOCOL_VERSION,
    "capabilities": {},
    "serverInfo": {"name": "mock-streamable-http", "version": "1.0"},
}

# 工具清单（简化：仅 echo / boom）
TOOLS: list[dict] = [
    {
        "name": "echo",
        "description": "回显输入",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    },
]


class _Session:
    """单个 MCP 会话：持有 server → client 通知队列。"""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.notifications: "queue.Queue[dict]" = queue.Queue()


class _State:
    """server 实例的共享状态（线程安全）。"""

    def __init__(self) -> None:
        self.sessions: dict[str, _Session] = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()


class MockStreamableHttpHandler(BaseHTTPRequestHandler):
    """处理 /mcp 的 POST（JSON-RPC）与 GET（SSE 通知流）。"""

    protocol_version = "HTTP/1.1"  # 长连接 / SSE 必须

    def log_message(self, *args: Any) -> None:  # 静默：避免测试噪声
        pass

    # ------------------------------------------------------------------ #
    # 工具方法
    # ------------------------------------------------------------------ #
    @property
    def _state(self) -> _State:
        return self.server.state  # type: ignore[attr-defined]

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else None

    def _send_json(self, obj: Any, session_id: str | None = None, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if session_id:
            self.send_header("mcp-session-id", session_id)
        self.end_headers()
        self.wfile.write(body)

    def _get_or_create_session(self) -> _Session:
        sid = self.headers.get("mcp-session-id")
        state = self._state
        with state.lock:
            if sid and sid in state.sessions:
                return state.sessions[sid]
            new_id = sid or f"session-{uuid.uuid4().hex}"
            sess = _Session(new_id)
            state.sessions[new_id] = sess
            return sess

    # ------------------------------------------------------------------ #
    # POST：JSON-RPC 端点
    # ------------------------------------------------------------------ #
    def do_POST(self) -> None:
        if urlparse(self.path).path not in ("/mcp", "/"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        msg = self._read_json()
        if not isinstance(msg, dict):
            self.send_response(400)
            self.end_headers()
            return
        method = msg.get("method")
        msg_id = msg.get("id")

        if method == "initialize":
            sess = self._get_or_create_session()
            # 排入一次 list_changed 通知，用于验证 GET 通知流能送达
            sess.notifications.put({
                "jsonrpc": "2.0",
                "method": "notifications/tools/list_changed",
                "params": {},
            })
            self._send_json(
                {"jsonrpc": "2.0", "id": msg_id, "result": SERVER_INFO},
                session_id=sess.session_id,
            )
            return

        if method == "notifications/initialized":
            # 通知无响应体（202 Accepted，空 body；必须带 Content-Length 否则
            # HTTP/1.1 客户端无法界定响应结束而挂起）
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if method == "tools/list":
            sess = self._get_or_create_session()
            self._send_json(
                {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}},
                session_id=sess.session_id,
            )
            return

        if method == "tools/call":
            sess = self._get_or_create_session()
            name = (msg.get("params") or {}).get("name")
            args = (msg.get("params") or {}).get("arguments", {}) or {}
            if name == "echo":
                result = {
                    "content": [{"type": "text", "text": f"echo: {args.get('text', '')}"}],
                    "isError": False,
                }
            elif name == "boom":
                result = {"content": [{"type": "text", "text": "出错了"}], "isError": True}
            else:
                self._send_json(
                    {"jsonrpc": "2.0", "id": msg_id,
                     "error": {"code": -32602, "message": f"unknown tool {name}"}},
                    session_id=sess.session_id,
                )
                return
            self._send_json(
                {"jsonrpc": "2.0", "id": msg_id, "result": result},
                session_id=sess.session_id,
            )
            return

        self._send_json(
            {"jsonrpc": "2.0", "id": msg_id,
             "error": {"code": -32601, "message": f"method not found: {method}"}},
            status=404,
        )

    # ------------------------------------------------------------------ #
    # GET：SSE 通知流
    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:
        if urlparse(self.path).path not in ("/mcp", "/"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        sid = self.headers.get("mcp-session-id")
        state = self._state
        sess: _Session | None = None
        if sid:
            with state.lock:
                sess = state.sessions.get(sid)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        if sess is None:
            # 无 session 的 GET（客户端在握手前误连）：回一条提示后关闭
            try:
                self.wfile.write(b": no-session\n\n")
                self.wfile.flush()
            except (BrokenPipeError, OSError):
                pass
            return

        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while not state.stop.is_set():
                try:
                    notification = sess.notifications.get(timeout=0.5)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                data = json.dumps(notification, ensure_ascii=False).encode("utf-8")
                self.wfile.write(b"data: " + data + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 客户端断开：结束本连接
            pass


class MockStreamableHttpServer:
    """后台运行的 streamable-http MCP server 实例（测试用）。"""

    def __init__(self) -> None:
        self.state = _State()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockStreamableHttpHandler)
        self._httpd.state = self.state  # handler 经 self.server.state 访问
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        port = self._httpd.server_address[1]
        self._url = f"http://127.0.0.1:{port}/mcp"

    def start(self) -> None:
        self._thread.start()

    @property
    def url(self) -> str:
        return self._url

    def stop(self) -> None:
        self.state.stop.set()
        try:
            self._httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
        self._thread.join(timeout=2.0)

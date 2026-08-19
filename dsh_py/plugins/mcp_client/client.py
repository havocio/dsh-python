"""最小 MCP JSON-RPC 客户端（对标 ``@modelcontextprotocol/sdk`` 的 client 子集）。

只实现 mcp-client 桥接所需的协议面：
- ``initialize`` 握手 + ``notifications/initialized`` 确认；
- ``tools/list``（分页）/ ``tools/call``；
- server → client 通知分发（``notifications/tools/list_changed`` 触发重同步）。

两种传输（对标 SDK 的 ``StdioClientTransport`` / ``StreamableHTTPClientTransport``）：
- **stdio**：spawn 子进程，stdin/stdout 逐行 JSON-RPC（环境变量经敏感名清洗）；
- **streamable-http**：POST 请求（JSON 或 SSE 响应）+ 后台 GET 通知流。

框架本体零依赖：httpx 仅 http 传输内部懒加载。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Optional

# MCP 协议版本（2025-06-18 为广泛兼容的稳定版）
PROTOCOL_VERSION = "2025-06-18"

# 子进程环境清洗（对齐 dsh-subprocess 的 scrubbedParentEnv）
SENSITIVE_ENV_PATTERN = re.compile(r"KEY|PASSWORD|SECRET|TOKEN", re.I)
DSH_ENV_PREFIX = "DSH_"


class McpError(Exception):
    """MCP 传输 / JSON-RPC 错误。"""

    def __init__(self, message: str, code: Optional[int] = None, cause: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.cause = cause


def scrubbed_parent_env() -> dict:
    """父进程环境减去凭据形名称与全部 ``DSH_*`` 名称（对齐 dsh 的 scrub 定义）。"""
    env = {}
    for key, value in os.environ.items():
        if SENSITIVE_ENV_PATTERN.search(key):
            continue
        if key.upper().startswith(DSH_ENV_PREFIX):
            continue
        env[key] = value
    return env


# --------------------------------------------------------------------------- #
# 传输
# --------------------------------------------------------------------------- #
class Transport(ABC):
    """MCP 传输：负责发送 JSON-RPC 消息并把收到的消息交给回调。"""

    @abstractmethod
    async def start(self, on_message: Callable[[dict], None]) -> None:
        """启动传输（spawn / 握手前准备）；消息到达时调用 ``on_message``。"""

    @abstractmethod
    async def send(self, payload: dict) -> None:
        """发送一条 JSON-RPC 消息。"""

    @abstractmethod
    async def close(self) -> None:
        """关闭传输。"""


class StdioTransport(Transport):
    """子进程 stdio 传输（JSON-RPC 逐行换行分隔）。"""

    def __init__(
        self,
        command: str,
        args: Optional[list[str]] = None,
        env: Optional[dict] = None,
        cwd: Optional[str] = None,
    ) -> None:
        self._command = command
        self._args = list(args or [])
        self._env = dict(env or {})
        self._cwd = cwd or None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.Task] = None

    async def start(self, on_message: Callable[[dict], None]) -> None:
        child_env = os.environ.copy()
        child_env.update(scrubbed_parent_env())
        child_env.update(self._env)
        self._proc = await asyncio.create_subprocess_exec(
            self._command, *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
            env=child_env,
            cwd=self._cwd,
        )
        self._reader = asyncio.create_task(self._read_loop(on_message))

    async def _read_loop(self, on_message: Callable[[dict], None]) -> None:
        """逐行读 stdout，反序列化并分发。EOF 视为连接关闭。"""
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            line = line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue  # 容忍非 JSON 行（如子进程日志打到 stdout）
            if isinstance(payload, dict):
                on_message(payload)

    async def send(self, payload: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def close(self) -> None:
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        reader, self._reader = self._reader, None
        # 先关 stdin → 子进程读到 EOF 自行退出；分级回收（wait→terminate→kill）
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await proc.wait()
                except (ProcessLookupError, asyncio.CancelledError):
                    pass
        # reader 任务随进程退出自然结束；取消后 await 回收，避免管道残留
        if reader is not None:
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


class StreamableHttpTransport(Transport):
    """Streamable HTTP 传输（POST 请求 + 后台 GET 通知流）。"""

    def __init__(self, url: str, headers: Optional[dict] = None) -> None:
        self._url = url
        self._headers = dict(headers or {})
        self._session_id: Optional[str] = None
        self._notify_task: Optional[asyncio.Task] = None
        self._closed = False

    async def start(self, on_message: Callable[[dict], None]) -> None:
        # 后台 GET 长连接接收 server → client 通知（tools/list_changed 等）
        self._notify_task = asyncio.create_task(self._notification_loop(on_message))

    async def _notification_loop(self, on_message: Callable[[dict], None]) -> None:
        while not self._closed:
            try:
                import httpx  # 懒加载：框架本体不依赖 httpx

                headers = {"accept": "text/event-stream"}
                if self._session_id is not None:
                    headers["mcp-session-id"] = self._session_id
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", self._url, headers=headers) as resp:
                        if resp.status_code >= 400:
                            raise McpError(f"MCP GET 通知流失败（HTTP {resp.status_code}）")
                        async for line in resp.aiter_lines():
                            if self._closed:
                                return
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            try:
                                payload = json.loads(line[5:].strip())
                            except json.JSONDecodeError:
                                continue
                            # 通知无 id；带 id 的是响应，不应出现在 GET 流
                            if isinstance(payload, dict) and "id" not in payload:
                                on_message(payload)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - 通知流断线后重连
                if self._closed:
                    return
                await asyncio.sleep(1.0)

    async def send(self, payload: dict) -> None:
        import httpx  # 懒加载

        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }
        headers.update(self._headers)
        if self._session_id is not None:
            headers["mcp-session-id"] = self._session_id
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream(
                    "POST", self._url, json=payload, headers=headers
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread())[:200]
                        raise McpError(
                            f"MCP POST 失败（HTTP {resp.status_code}）：{body!r}")
                    self._session_id = resp.headers.get("mcp-session-id") or self._session_id
                    content_type = resp.headers.get("content-type", "")
                    if "text/event-stream" in content_type:
                        # SSE 响应：取第一个 message 事件作为本次请求的响应
                        async for line in resp.aiter_lines():
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                continue
                            try:
                                result = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            self._dispatch_result(payload.get("id"), result)
                            return
                        raise McpError("MCP SSE 响应在 message 事件前结束")
                    body = await resp.aread()
                    try:
                        result = json.loads(body)
                    except json.JSONDecodeError as exc:
                        raise McpError(f"MCP 响应不是 JSON：{body[:120]!r}") from exc
                    self._dispatch_result(payload.get("id"), result)
        except McpError:
            raise
        except Exception as exc:  # noqa: BLE001 - 传输失败
            raise McpError(f"MCP 请求 {payload.get('method')!r} 传输失败", cause=exc) from exc

    def _dispatch_result(self, msg_id, result: dict) -> None:
        """HTTP 传输收到的是单个请求的响应（无需走回调分发）。"""
        if self._on_response is not None:
            self._on_response(msg_id, result)

    _on_response: Optional[Callable[[Any, dict], None]] = None

    async def close(self) -> None:
        self._closed = True
        if self._notify_task is not None:
            self._notify_task.cancel()
            self._notify_task = None


# --------------------------------------------------------------------------- #
# MCP 客户端
# --------------------------------------------------------------------------- #
class McpClient:
    """MCP 客户端：JSON-RPC 请求/响应匹配 + 通知分发 + 高层工具方法。"""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._notification_handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}
        self._closed = False
        self.server_info: Optional[dict] = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def connect(self) -> dict:
        """建立连接：start 传输 → initialize 握手 → initialized 通知。"""
        # StreamableHttpTransport 通过回调收响应；普通传输走 on_message 分发
        if isinstance(self._transport, StreamableHttpTransport):
            self._transport._on_response = self._on_http_response
        await self._transport.start(self._on_message)
        result = await self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "dsh-mcp-client", "version": "0.0.1"},
        })
        self.server_info = result.get("serverInfo")
        await self.notify("notifications/initialized")
        return result

    async def close(self) -> None:
        """关闭传输并取消所有等待中的请求。"""
        if self._closed:
            return
        self._closed = True
        for fut in self._pending.values():
            fut.cancel()
        self._pending.clear()
        await self._transport.close()

    # ------------------------------------------------------------------ #
    # JSON-RPC
    # ------------------------------------------------------------------ #
    def on_notification(self, method: str, handler: Callable[[dict], Awaitable[None]]) -> None:
        """注册 server → client 通知处理器。"""
        self._notification_handlers[method] = handler

    async def request(self, method: str, params: Optional[dict] = None, timeout: float = 60.0) -> dict:
        """发一条 request 并等待响应（超时抛 McpError）。"""
        if self._closed:
            raise McpError("MCP 客户端已关闭")
        self._next_id += 1
        msg_id = self._next_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[msg_id] = fut
        try:
            await self._transport.send(payload)
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise McpError(f"MCP 请求 {method!r} 超时（{timeout}s）") from exc
        finally:
            self._pending.pop(msg_id, None)

    async def notify(self, method: str, params: Optional[dict] = None) -> None:
        """发一条通知（无 id，不等待响应）。"""
        if self._closed:
            raise McpError("MCP 客户端已关闭")
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._transport.send(payload)

    # ------------------------------------------------------------------ #
    # 消息分发
    # ------------------------------------------------------------------ #
    def _on_message(self, payload: dict) -> None:
        """stdio 等全双工传输的消息回调：按 id 匹配响应，无 id 走通知。"""
        if "id" in payload:
            self._resolve_response(payload["id"], payload)
            return
        method = payload.get("method")
        handler = self._notification_handlers.get(method)
        if handler is not None:
            asyncio.ensure_future(handler(payload.get("params") or {}))

    def _on_http_response(self, msg_id: Any, result: dict) -> None:
        """Streamable HTTP 的单请求响应回调。"""
        if msg_id is not None:
            self._resolve_response(int(msg_id), result)

    def _resolve_response(self, msg_id: Any, payload: dict) -> None:
        fut = self._pending.get(int(msg_id)) if msg_id is not None else None
        if fut is None or fut.done():
            return
        if "error" in payload:
            err = payload["error"]
            fut.set_exception(McpError(
                err.get("message", "MCP 错误"),
                code=err.get("code"),
            ))
        else:
            fut.set_result(payload.get("result"))

    # ------------------------------------------------------------------ #
    # MCP 工具方法
    # ------------------------------------------------------------------ #
    async def list_tools(self, cursor: Optional[str] = None) -> tuple[list[dict], Optional[str]]:
        """分页拉取服务器工具列表，返回 ``(tools, next_cursor)``。"""
        params = {"cursor": cursor} if cursor is not None else None
        result = await self.request("tools/list", params)
        return result.get("tools", []), result.get("nextCursor")

    async def call_tool(self, name: str, arguments: dict, timeout: float = 60.0) -> dict:
        """调用一个工具（raw 名字），返回 MCP 结果对象。"""
        return await self.request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout=timeout,
        )

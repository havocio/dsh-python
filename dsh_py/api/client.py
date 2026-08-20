"""SDK 跨进程客户端（对标 ``@deepseek-ai/dsh-sdk-client`` 的 HarnessClient / DeepSeekHarness）。

:class:`HarnessClient` 负责拉起运行时子进程（默认 ``python -m dsh_py.cli --jsonrpc``），
用 newline JSON-RPC 与它通信，把服务器通知扇出到订阅者；关闭时走
shutdown → 等待退出 → terminate → kill 的阶梯。

高层 :class:`DeepSeekHarness` / :class:`HarnessSession` 与进程内版
（``dsh_py.sdk``）同名同 API：``start()`` 幂等握手、``session(id)`` 拿会话句柄、
``session.run(text)`` 发一条 prompt 并等到该会话下次 idle，返回
:class:`RunResult`。二者可互换——进程内版适合单进程内嵌，本版适合跨进程/
跨语言客户端。
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from typing import Any, Awaitable, Callable, Optional

from dsh_py.api.protocol import JsonRpcLineTransport, JsonRpcResponseError, JsonRpcProtocolError
from dsh_py.services.message import TextBlock

# 退出阶梯超时（对齐 dsh 的 disposeRuntimeProcess）
SHUTDOWN_TIMEOUT_MS = 5000
TERMINATE_TIMEOUT_MS = 3000


class TransportClosedError(Exception):
    """运行时子进程已退出 / stdio 关闭 / 无法拉起。"""


class RequestTimeoutError(Exception):
    """请求超过超时时间未响应。"""


class SdkProtocolError(Exception):
    """运行时回答不符合协议（例如响应缺字段）。"""


# --------------------------------------------------------------------------- #
# 通知订阅
# --------------------------------------------------------------------------- #
class NotificationSubscription:
    """客户端侧单次通知流：可逐个取或异步等待下一个匹配通知。"""

    def __init__(self, state: dict, unsubscribe: Callable[[], None]) -> None:
        self._state = state
        self._unsubscribe = unsubscribe

    def try_next(self) -> Optional[dict]:
        """取一条已入队通知；无则返回 None（不等待）。"""
        queue = self._state["queue"]
        return queue.pop(0) if queue else None

    async def next(self) -> dict:
        """等待下一条通知；流失败时抛底层错误。"""
        queue = self._state["queue"]
        if queue:
            return queue.pop(0)
        if self._state.get("failure") is not None:
            raise self._state["failure"]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._state["waiters"].append(future)
        return await future

    def close(self) -> None:
        """断开订阅：丢弃已入队项，pending 等待者立即抛错。"""
        self._unsubscribe()
        self._state["queue"].clear()
        self._fail(TransportClosedError("notification subscription closed"))

    def _fail(self, error: Exception) -> None:
        self._state["failure"] = error
        for future in self._state["waiters"]:
            if not future.done():
                future.set_exception(error)
        self._state["waiters"].clear()


# --------------------------------------------------------------------------- #
# 底层 JSON-RPC 客户端（拥有子进程）
# --------------------------------------------------------------------------- #
class HarnessClient:
    """SDK 运行时子进程的 JSON-RPC 客户端。

    :param command: 启动命令；缺省 ``[sys.executable, "-m", "dsh_py.cli", "--jsonrpc"]``。
    :param cwd: 子进程工作目录（缺省继承）。
    :param request_timeout_ms: 请求超时（缺省 None = 不超时）。
    """

    def __init__(self, command: Optional[list[str]] = None, cwd: Optional[str] = None,
                 request_timeout_ms: Optional[int] = None) -> None:
        self.command = command or [sys.executable, "-m", "dsh_py.cli", "--jsonrpc"]
        self.cwd = cwd
        self.request_timeout_ms = request_timeout_ms
        self._proc: Any = None
        self._transport: Optional[JsonRpcLineTransport] = None
        self._stderr_tail: list[str] = []
        self._subscriptions: dict[str, dict] = {}
        self._sub_serial = 0
        self._close_task: Optional[asyncio.Task] = None
        self._exit_code: Optional[int] = None
        self._spawn_error: Optional[Exception] = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """拉起子进程并挂起读循环（幂等）。"""
        if self._proc is not None or self._spawn_error is not None:
            return
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=self.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:  # noqa: BLE001
            self._spawn_error = exc
            raise TransportClosedError(f"failed to spawn SDK runtime: {exc}") from exc
        proc = self._proc

        async def read_line() -> Optional[str]:
            assert proc.stdout is not None
            line = await proc.stdout.readline()
            if not line:
                # EOF：运行时已退出 → 通知全部订阅者
                self._exit_code = proc.returncode
                self._fail_all_subscriptions(TransportClosedError(
                    self._closed_error("DeepSeek Harness runtime closed")))
                return None
            return line.decode("utf-8", "replace")

        def write_line(line: str) -> None:
            assert proc.stdin is not None
            try:
                proc.stdin.write((line + "\n").encode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                raise JsonRpcProtocolError(f"write failed: {exc}") from exc

        self._transport = JsonRpcLineTransport(reader=read_line, writer=write_line)
        self._transport.on_request(self._on_incoming_request)
        self._transport.on_notification(self._on_notification)
        self._transport.start()
        self._stderr_task = asyncio.create_task(self._drain_stderr(), name="dsh-sdk-stderr")

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    if len(self._stderr_tail) > 400:
                        self._stderr_tail.pop(0)
        except Exception:  # noqa: BLE001
            pass

    def _fail_all_subscriptions(self, error: Exception) -> None:
        """运行时退出/关闭：让全部订阅者失败（保留已入队项可排空）。"""
        for state in list(self._subscriptions.values()):
            if state.get("failure") is None:
                state["failure"] = error
            for future in state["waiters"]:
                if not future.done():
                    future.set_exception(error)
            state["waiters"].clear()

    async def _on_incoming_request(self, method: str, params: dict) -> Any:
        # 客户端一般不处理 server→client 请求；未知方法回 -32601
        raise JsonRpcProtocolError(f"unexpected server request: {method}")

    def _on_notification(self, method: str, params: dict) -> None:
        notification = {"method": method, "params": params}
        for state in list(self._subscriptions.values()):
            if state.get("filter") is not None and state["filter"](notification) is False:
                continue
            state["queue"].append(notification)
            while state["waiters"]:
                future = state["waiters"].pop(0)
                if not future.done():
                    future.set_result(notification)
                    break

    # ------------------------------------------------------------------ #
    # 请求 / 通知
    # ------------------------------------------------------------------ #
    async def request(self, method: str, params: Optional[dict] = None,
                      timeout_ms: Optional[int] = None) -> Any:
        if self._exit_code is not None or self._spawn_error is not None:
            raise self._closed_error("DeepSeek Harness runtime closed")
        assert self._transport is not None
        try:
            return await self._transport.request(
                method, params, timeout_ms if timeout_ms is not None else self.request_timeout_ms)
        except JsonRpcResponseError as exc:
            raise exc
        except asyncio.TimeoutError as exc:
            raise RequestTimeoutError(f"{method} timed out") from exc
        except JsonRpcProtocolError as exc:
            raise TransportClosedError(str(exc)) from exc

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        if self._transport is not None:
            self._transport.notify(method, params)

    # ------------------------------------------------------------------ #
    # 订阅
    # ------------------------------------------------------------------ #
    def subscribe(self, filter_fn: Optional[Callable[[dict], bool]] = None) -> NotificationSubscription:
        """订阅通知流；``filter_fn`` 返回 False 的跳过（None = 全收）。"""
        subscription_id = str(self._sub_serial)
        self._sub_serial += 1
        state: dict = {"queue": [], "waiters": [], "filter": filter_fn, "failure": None}
        self._subscriptions[subscription_id] = state
        if self._exit_code is not None or self._spawn_error is not None:
            state["failure"] = self._closed_error("DeepSeek Harness runtime closed")
        return NotificationSubscription(state, lambda: self._subscriptions.pop(subscription_id, None))

    def subscribe_session_tree(self, session_id: str) -> NotificationSubscription:
        """订阅单个会话的事件/状态通知（对标 dsh 的 subscribeSessionTree）。"""

        def keep(notification: dict) -> bool:
            method = notification["method"]
            params = notification.get("params") or {}
            if params.get("sessionId") != session_id:
                return False
            return method in ("session.event", "session.status")

        return self.subscribe(keep)

    # ------------------------------------------------------------------ #
    # 关闭
    # ------------------------------------------------------------------ #
    async def close(self) -> None:
        """关闭到静默：shutdown 请求 → 等退出 → terminate → kill。"""
        if self._close_task is not None:
            await self._close_task
            return
        self._close_task = asyncio.ensure_future(self._perform_close())
        await self._close_task

    async def _perform_close(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            # 1. 协议级 shutdown（失败不阻断清理）
            try:
                await asyncio.wait_for(
                    self._transport.request("shutdown", {}, timeout_ms=SHUTDOWN_TIMEOUT_MS),
                    timeout=SHUTDOWN_TIMEOUT_MS / 1000,
                )
            except Exception:  # noqa: BLE001
                pass
            # 2. 等进程自行退出
            try:
                await asyncio.wait_for(proc.wait(), timeout=TERMINATE_TIMEOUT_MS / 1000)
            except asyncio.TimeoutError:
                # 3. terminate → 再等 → kill
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=TERMINATE_TIMEOUT_MS / 1000)
                except asyncio.TimeoutError:
                    proc.kill()
                    try:
                        await proc.wait()
                    except ProcessLookupError:
                        pass
        if proc is not None:
            self._exit_code = proc.returncode
        # 先停 stderr 读取任务：communicate 也会读 stderr，避免两者竞争同一 StreamReader
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None
        # 显式关闭子进程管道：进程已退出时 communicate 安全（drain + 关 stdin/stdout/stderr），
        # 消除 Windows 退出期 GC 触碰失效匿名管道句柄产生的 ResourceWarning / OSError(WinError 6)。
        if proc is not None and proc.returncode is not None:
            try:
                await asyncio.wait_for(proc.communicate(), timeout=TERMINATE_TIMEOUT_MS / 1000)
            except Exception:  # noqa: BLE001
                pass
        # 停止读循环（transport 内部任务），拒绝 pending 请求
        if self._transport is not None:
            await self._transport.close()
            self._transport = None
        # 所有订阅失败
        for state in list(self._subscriptions.values()):
            state["queue"].clear()
            state["failure"] = TransportClosedError("DeepSeek Harness runtime closed")
            for future in state["waiters"]:
                if not future.done():
                    future.set_exception(state["failure"])
            state["waiters"].clear()

    def _closed_error(self, message: str) -> TransportClosedError:
        tail = "\n".join(self._stderr_tail[-20:])
        detail = f" (exit code {self._exit_code})" if self._exit_code is not None else ""
        if tail:
            return TransportClosedError(f"{message}{detail}\nstderr tail:\n{tail}")
        return TransportClosedError(f"{message}{detail}")


# --------------------------------------------------------------------------- #
# 高层 API（与 dsh_py.sdk 同名同 API，跨进程版）
# --------------------------------------------------------------------------- #
class RunResult:
    """一次 ``run()`` 的结果（对标 dsh sdk 的 ``RunResult``）。"""

    def __init__(self, session_id: str, final_response: str, events: list) -> None:
        self.session_id = session_id
        self.final_response = final_response
        self.events = events


def final_response(events: list) -> str:
    """从会话事件提取最后一条 assistant 文本（对标 dsh 的 finalResponse）。"""
    for event in reversed(events):
        if event.get("type") != "assistant/message":
            continue
        data = event.get("data") or {}
        message = data.get("message") or {}
        # encode_payload 把 Message 包成 {"__msg__": {...}}（跨进程 wire 形态）
        if isinstance(message, dict) and "__msg__" in message:
            message = message["__msg__"]
        content = message.get("content") or []
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            # wire 块形态：encode_payload 用 {"__block__": "text", ...} 标记
            kind = block.get("type") or block.get("__block__")
            if kind == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


class HarnessSession:
    """一个具名会话的跨进程句柄（对标 dsh 的 ``HarnessSession``）。"""

    def __init__(self, harness: "DeepSeekHarness", session_id: str) -> None:
        self.harness = harness
        self.id = session_id

    async def run(self, input_: Any, on_notification: Optional[Callable[[dict], None]] = None) -> RunResult:
        """发一条 prompt，观察整个会话直到下次 idle，返回 :class:`RunResult`。

        :param input_: 文本或 contentBlocks 列表（逐字发送）。
        """
        await self.harness.start()
        client = self.harness.client
        content_blocks = _normalize_input(input_)
        events: list = []
        notifications: list = []

        subscription = client.subscribe_session_tree(self.id)

        def collect(notification: dict) -> None:
            notifications.append(notification)
            if on_notification is not None:
                on_notification(notification)
            if notification["method"] == "session.event" and (
                    notification.get("params") or {}).get("sessionId") == self.id:
                events.append(notification["params"]["event"])

        try:
            message_id = await client.request("session/prompt", {
                "sessionId": self.id,
                "contentBlocks": content_blocks,
            })
            if not isinstance(message_id, dict) or not isinstance(message_id.get("messageId"), str):
                raise SdkProtocolError(f"session/prompt returned no message id: {message_id!r}")
            message_id = message_id["messageId"]
            received = False
            while True:
                notification = await subscription.next()
                if not received:
                    if not _is_inbox_receipt(notification, self.id, message_id):
                        continue
                    received = True
                collect(notification)
                if (notification["method"] == "session.status"
                        and (notification.get("params") or {}).get("sessionId") == self.id
                        and (notification.get("params") or {}).get("status") == "idle"):
                    break
        finally:
            subscription.close()

        return RunResult(self.id, final_response(events), events)


class DeepSeekHarness:
    """跨进程 Harness：拥有一个运行时子进程，跨多会话复用。

    :param command: 运行时启动命令（缺省 ``python -m dsh_py.cli --jsonrpc``）。
    :param cwd: 记录到每个 SDK 会话 header 的工作目录（绝对化后握手）。
    :param provider: 默认供应商（缺省 ``deepseek-official``）。
    :param model: 默认模型（缺省 ``deepseek-v4-flash``）。
    """

    def __init__(self, command: Optional[list[str]] = None, cwd: Optional[str] = None,
                 provider: Optional[str] = None, model: Optional[str] = None,
                 max_tokens: Optional[int] = None,
                 request_timeout_ms: Optional[int] = None) -> None:
        self._launch = command
        # 握手 cwd 记录到每个 SDK 会话 header；子进程 spawn 目录保持当前进程
        # 目录（保证 ``-m dsh_py.cli`` 可导入），与握手 cwd 相互独立。
        self.cwd = os.path.abspath(os.path.expanduser(cwd or os.getcwd()))
        self.provider = provider or "deepseek-official"
        self.model = model or "deepseek-v4-flash"
        self.max_tokens = max_tokens
        self._client = HarnessClient(command=command, cwd=os.getcwd(),
                                     request_timeout_ms=request_timeout_ms)
        self._initialized: Optional[asyncio.Task] = None
        self._closed = False

    @property
    def client(self) -> HarnessClient:
        """底层 JSON-RPC 客户端（低层访问用）。"""
        return self._client

    def start(self) -> Awaitable[None]:
        """拉起子进程并完成 initialize 握手（幂等；失败后重试换新子进程）。"""
        if self._initialized is None:
            self._initialized = asyncio.ensure_future(self._perform_start())
        return self._initialized

    async def _perform_start(self) -> None:
        try:
            await self._client.start()
            await self._client.request("initialize", {
                "cwd": self.cwd,
                "provider": self.provider,
                "model": self.model,
                **({} if self.max_tokens is None else {"maxTokens": self.max_tokens}),
            })
        except Exception:
            self._initialized = None
            await self._client.close()
            if not self._closed:
                self._client = HarnessClient(command=self._launch, cwd=None,
                                             request_timeout_ms=self._client.request_timeout_ms)
            raise

    def session(self, session_id: Optional[str] = None) -> HarnessSession:
        """拿一个会话句柄；缺省生成 ``session-<uuid>``。"""
        sid = session_id or f"session-{uuid.uuid4().hex}"
        return HarnessSession(self, sid)

    async def close(self) -> None:
        """关闭子进程（shutdown → terminate → kill 阶梯）。"""
        self._closed = True
        await self._client.close()


def _normalize_input(input_: Any) -> list:
    """run 入参归一：字符串 → 单个 text 块；列表原样直通。"""
    if isinstance(input_, str):
        return [{"type": "text", "text": input_}]
    if isinstance(input_, list):
        return input_
    raise TypeError("run input must be a string or content block array")


def _is_inbox_receipt(notification: dict, session_id: str, message_id: str) -> bool:
    """判断通知是否为 messageId 的入箱回执（agent/inbox/spliced 含该消息）。"""
    if notification["method"] != "session.event":
        return False
    params = notification.get("params") or {}
    if params.get("sessionId") != session_id:
        return False
    event = params.get("event") or {}
    if event.get("type") != "agent/inbox/spliced":
        return False
    data = event.get("data") or {}
    inserted = data.get("inserted")
    if not isinstance(inserted, list):
        return False
    for message in inserted:
        if not isinstance(message, dict):
            continue
        # encode_payload 把 Message 包成 {"__msg__": {...}}；裸形态直接读 id
        inner = message.get("__msg__", message)
        if isinstance(inner, dict) and inner.get("id") == message_id:
            return True
    return False

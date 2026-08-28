"""acp（Agent Client Protocol）服务器（对标 dsh 的 ``@deepseek-ai/dsh-acp``）。

自动化的 ACP 服务器：把可信的程序化客户端暴露给新鲜 harness 会话。携带 prompt、
已提交的 assistant 文本、取消、一次性权限决策；展示与人类交互特征留在 harness 的 UI 模块。

本实现用纯 Python 的 stdio 换行分隔 JSON-RPC；不依赖第三方 ACP SDK，协议消息形状
沿用 ACP 规范（initialize / authenticate / sessions/new / prompt / cancel + session/update 通知）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Any

from dsh_py.core.context import AppContext
from dsh_py.services.message import create_user_message

PROTOCOL_VERSION = {"protocolVersion": "2025-05-01", "agentInfo": {"name": "deepseek-harness-acp", "version": "0.0.1"},
                     "agentCapabilities": {"promptCapabilities": {"image": False, "audio": False, "embeddedContext": False}},
                     "authMethods": []}


def acp_prompt_to_text(prompt: Any) -> str:
    """把 ACP prompt 内容（text / resource_link）拼为纯文本；不支持类型视为非法。"""
    parts: list[str] = []
    for item in prompt:
        if isinstance(item, dict):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "resource_link":
                parts.append(f"[{item.get('uri', '')}]")
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


def prompt_has_unsupported_content(prompt: Any) -> bool:
    """仅允许 text 与 resource_link。"""
    for item in prompt:
        if isinstance(item, dict) and item.get("type") not in ("text", "resource_link"):
            return True
    return False


def turn_end_to_stop_reason(reason: Any) -> str:
    """把 harness 的 turn 结束原因映射为 ACP 的 stop reason。"""
    kind = reason.get("kind") if isinstance(reason, dict) else None
    if kind == "max-tokens":
        return "end_turn"
    if kind == "error":
        return "error"
    return "end_turn"


class AcpServer:
    """ACP 桥接服务器：创建并拥有 agents，把会话事件桥接为 session/update。"""

    def __init__(self, ctx: AppContext, config: Any = None) -> None:
        self.ctx = ctx
        self.config = config or {}
        self.sessions: dict[str, dict] = {}
        self.closed = False
        self._register_listeners()

    def _register_listeners(self) -> None:
        ctx = self.ctx

        @ctx.on("session/event")
        def on_session_event(session: Any, event: Any) -> None:
            record = self.sessions.get(getattr(session.header, "id", None))
            if record is None or record["agent"].session is not session:
                return
            if event.type == "assistant/message":
                for block in event.data.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block.get("text"):
                        self._notify({
                            "sessionId": record["agent"].session.id,
                            "update": {"sessionUpdate": "agent_message_chunk",
                                       "content": {"type": "text", "text": block["text"]}},
                        })
            if event.type == "turn/end" and record["inflight"] is not None:
                inflight = record["inflight"]
                if inflight["turn"] == event.data.get("turn"):
                    if event.data.get("reason", {}).get("kind") == "error":
                        self._settle_error(record, event.data["reason"])
                    else:
                        inflight["endReason"] = event.data.get("reason")

        @ctx.on("agent/inbox/claimed")
        def on_claimed(payload: Any) -> None:
            record = self._owned(payload.get("agent"))
            if record and record["inflight"] and record["inflight"]["messageId"] == payload.get("message", {}).get("id"):
                record["inflight"]["turn"] = payload.get("turn")

        @ctx.on("agent/error")
        def on_error(payload: Any) -> None:
            record = self._owned(payload.get("agent"))
            inflight = record["inflight"] if record else None
            if record is None or inflight is None or inflight["turn"] == payload.get("turn"):
                return
            self._settle_error(record, {"kind": "error", "error": payload.get("error")})

    def _owned(self, agent: Any) -> dict | None:
        if agent is None:
            return None
        record = self.sessions.get(getattr(agent.session, "id", None))
        return record if record and record["agent"] is agent else None

    def _notify(self, notification: dict) -> None:
        try:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "session/update",
                                         "params": notification}) + "\n")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001 - 断开的客户端不应让 agent turn 失败
            pass

    def _settle_error(self, record: dict, reason: Any) -> None:
        inflight = record["inflight"]
        if inflight is None:
            return
        record["inflight"] = None
        inflight["reject"](RuntimeError(f"turn failed: {reason}"))

    # ---- ACP 方法 ----

    async def initialize(self, params: Any) -> dict:
        return PROTOCOL_VERSION

    async def authenticate(self, params: Any) -> None:
        return None

    async def new_session(self, params: Any) -> dict:
        if self.closed:
            raise RuntimeError("the ACP bridge has been disposed")
        if not params.get("cwd") or not _is_absolute(params["cwd"]):
            raise ValueError(f"cwd must be an absolute path: {params.get('cwd')}")
        if params.get("additionalDirectories"):
            raise ValueError("additionalDirectories is not supported")
        if params.get("mcpServers"):
            raise ValueError("mcpServers is not *supported")
        session_id = str(uuid.uuid4())
        handle = await self.ctx.agents.create(
            sessionId=session_id,
            meta={"cwd": params["cwd"]},
            agentOptions={k: self.config[k] for k in ("provider", "model") if k in self.config},
        )
        self.sessions[session_id] = {
            "agent": handle.agent,
            "dispose": handle.dispose,
            "inflight": None,
        }
        return {"sessionId": session_id}

    async def prompt(self, params: Any) -> dict:
        if self.closed:
            raise RuntimeError("the ACP bridge has been disposed")
        record = self.sessions.get(params["sessionId"])
        if record is None:
            raise ValueError(f"unknown session: {params['sessionId']}")
        if record["inflight"] is not None:
            raise ValueError("a prompt is already in flight for this session")
        if prompt_has_unsupported_content(params.get("prompt", [])):
            raise ValueError("only text and resource_link prompt content is supported")
        text = acp_prompt_to_text(params.get("prompt", []))
        if text.strip() == "":
            raise ValueError("empty prompt")
        if self.ctx.agents.get(record["agent"].id) is not record["agent"]:
            raise RuntimeError("prompt was not queued: the agent was disposed outside the bridge")

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        inflight = {"resolve": lambda r: future.set_result(r), "reject": lambda e: future.set_exception(e),
                    "messageId": None, "turn": None, "endReason": None}
        record["inflight"] = inflight
        message = create_user_message({"content": [{"type": "text", "text": text}],
                                        "source": {"kind": "user"}})
        inflight["messageId"] = message.id
        try:
            record["agent"].followup(message)
        except Exception as exc:  # noqa: BLE001
            record["inflight"] = None
            raise RuntimeError(f"prompt was not queued: {exc}")
        await record["agent"].when_idle()
        inflight = record["inflight"]
        record["inflight"] = None
        if inflight is None:
            return {"stopReason": "cancelled"}
        end = inflight["endReason"]
        if end is None:
            return {"stopReason": "cancelled"}
        return {"stopReason": turn_end_to_stop_reason(end)}

    async def cancel(self, params: Any) -> None:
        record = self.sessions.get(params["sessionId"])
        if record is None:
            return
        record["agent"].cancel({"kind": "user"})
        if record["inflight"] is not None:
            record["inflight"] = None


def _is_absolute(p: str) -> bool:
    import os
    return os.path.isabs(p)


async def serve_stdio(server: "AcpServer") -> None:
    """stdio JSON-RPC 传输循环：从 stdin 读换行分隔的 JSON-RPC，派发到 server 方法。"""
    loop = asyncio.get_event_loop()
    methods = {
        "initialize": server.initialize,
        "authenticate": server.authenticate,
        "sessions/new": server.new_session,
        "prompt": server.prompt,
        "cancel": server.cancel,
    }
    reader = sys.stdin
    while True:
        line = await loop.run_in_executor(None, reader.readline)
        if not line:
            break
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        handler = methods.get(method)
        if handler is None:
            continue
        try:
            result = await handler(msg.get("params") or {})
            if msg_id is not None:
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n")
                sys.stdout.flush()
        except Exception as exc:  # noqa: BLE001
            if msg_id is not None:
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id,
                                             "error": {"message": str(exc)}}) + "\n")
                sys.stdout.flush()


class AcpClientConnection:
    """ACP 客户端连接：在子进程 stdio 管道对上做换行分隔 JSON-RPC（客户端方向）。

    与 :class:`AcpServer`（服务端，读系统 stdin）相对——本类把 asyncio
    StreamReader/StreamWriter 当作对端 ACP agent 进程的管道：发送
    ``initialize``/``sessions/new``/``prompt``/``cancel`` 请求（id 配对），
    接收服务端响应与两种通知：

    - ``session/update`` → 转交 ``on_session_update`` 回调（subagent-acp 用它
      收集 ``agent_message_chunk`` 文本）；
    - ``request/permission`` → 按 ``permission`` 策略**自动应答**（不向人类
      展示任何提示）：``allow`` 选首个 ``allow_once``/``allow_always`` 选项，
      否则应答 ``cancelled`` 让子代理不继续。

    读循环常驻后台；对端关闭流或主动 :meth:`close` 后结束。
    """

    def __init__(
        self,
        reader: Any,
        writer: Any,
        *,
        on_session_update: Any = None,
        permission: str = "reject",
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._on_session_update = on_session_update
        self._permission = permission
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._closed = False
        self._read_task = asyncio.create_task(self._read_loop())

    async def _request(self, method: str, params: Any) -> Any:
        """发一个请求并等待配对响应；读循环异常以 RuntimeError 拒绝。"""
        if self._closed:
            raise RuntimeError("acp client connection is closed")
        self._next_id += 1
        msg_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        payload = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        try:
            self._writer.write((json.dumps(payload) + "\n").encode("utf-8"))
            await self._writer.drain()
        except Exception as exc:  # noqa: BLE001 - 写失败即请求失败
            self._pending.pop(msg_id, None)
            raise RuntimeError(f"acp client write failed: {exc}") from exc
        return await future

    async def initialize(self, client_capabilities: Any = None,
                         protocol_version: str = "2025-05-01") -> dict:
        """ACP 握手：声明协议版本与**空**客户端能力（子代理自给自足）。"""
        result = await self._request("initialize", {
            "protocolVersion": protocol_version,
            "clientCapabilities": client_capabilities or {},
        })
        return result or {}

    async def new_session(self, cwd: str, mcp_servers: Any = None) -> dict:
        """在子代理侧建立会话；返回 ``{"sessionId": ...}``。"""
        result = await self._request("sessions/new", {
            "cwd": cwd,
            "mcpServers": mcp_servers or [],
        })
        return result or {}

    async def prompt(self, session_id: str, prompt: list) -> dict:
        """跑一轮提示；返回含 ``stopReason`` 的结果。"""
        result = await self._request("prompt", {"sessionId": session_id, "prompt": prompt})
        return result or {}

    async def cancel(self, session_id: str) -> None:
        """尽力而为地取消子代理当前回合（进程回收仍由调用方负责）。"""
        await self._request("cancel", {"sessionId": session_id})

    def _dispatch(self, msg: dict) -> None:
        """分派一行 JSON：响应（带 id）结算 pending；通知走回调/自动应答。"""
        if "id" in msg:
            future = self._pending.pop(msg.get("id"), None)
            if future is None or future.done():
                return
            if "error" in msg:
                error = msg["error"] or {}
                future.set_exception(RuntimeError(str(error.get("message") or "acp error")))
            else:
                future.set_result(msg.get("result"))
            return
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "session/update":
            callback = self._on_session_update
            if callback is not None:
                try:
                    callback(params)
                except Exception:  # noqa: BLE001 - 消费者回调不得中断读循环
                    pass
        elif method == "request/permission":
            self._answer_permission(params)

    def _answer_permission(self, params: dict) -> None:
        """自动应答权限请求：allow → 首个可用选项；否则 cancelled。"""
        request_id = params.get("requestId")
        if request_id is None:
            return
        outcome: dict = {"outcome": "cancelled"}
        if self._permission == "allow":
            for option in params.get("options") or []:
                if option.get("kind") in ("allow_once", "allow_always"):
                    outcome = {"outcome": "selected", "optionId": option.get("optionId")}
                    break
        try:
            self._writer.write((json.dumps({
                "jsonrpc": "2.0", "id": request_id,
                "result": {"outcome": outcome},
            }) + "\n").encode("utf-8"))
        except Exception:  # noqa: BLE001 - 应答尽力而为
            pass

    async def _read_loop(self) -> None:
        """后台读循环：逐行解析 ndjson，直到对端关闭流。"""
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self._dispatch(msg)
        except (asyncio.CancelledError, asyncio.IncompleteReadError):  # noqa: PERF203
            pass
        except Exception:  # noqa: BLE001 - 流异常终止视同对端关闭
            pass
        finally:
            closed_by_us = self._closed
            self._closed = True
            if not closed_by_us:
                # 对端主动关闭：在途请求以明确错误结算（未检索异常即时消费）。
                exc = RuntimeError("acp child closed its protocol stream")
                for future in list(self._pending.values()):
                    if not future.done():
                        future.set_exception(exc)
                self._pending.clear()

    async def close(self) -> None:
        """主动关闭：取消读循环与全部在途请求（幂等、不抛）。"""
        self._closed = True
        for future in list(self._pending.values()):
            if not future.done():
                future.cancel()
        self._pending.clear()
        if not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


__all__ = [
    "AcpServer", "AcpClientConnection", "acp_prompt_to_text",
    "prompt_has_unsupported_content", "turn_end_to_stop_reason", "serve_stdio",
]

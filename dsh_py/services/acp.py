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


__all__ = [
    "AcpServer", "acp_prompt_to_text", "prompt_has_unsupported_content",
    "turn_end_to_stop_reason", "serve_stdio",
]

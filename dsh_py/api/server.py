"""SDK JSON-RPC 服务器（对标 ``@deepseek-ai/dsh-sdk-jsonrpc-server``）。

一个已装配的 harness 上下文 + 一个 :class:`JsonRpcLineTransport`，对外提供
进程外 SDK 协议：

- ``initialize`` —— 握手：记录 cwd / provider / model / maxTokens；目标 provider
  未注册且为 ``deepseek-official`` 时兜底挂载 llm-deepseek 插件。
- ``session/prompt`` —— 按 sessionId 取或建会话，把 contentBlocks 作为一条
  user 消息投递到 agent 收件箱，返回消息 id。
- ``shutdown`` —— 幂等：退订全部事件、取消全部会话 agent、卸载兜底插件。

同时订阅 ``session/event``（→ ``session.event`` 通知）与 ``agent/status``
（→ ``session.status`` 通知），把会话日志与 agent 生命周期流给客户端。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Callable, Optional

from dsh_py.core.context import AppContext
from dsh_py.api.protocol import JsonRpcLineTransport
from dsh_py.services.agent import AgentOptions
from dsh_py.services.adapters.deepseek import apply as apply_deepseek
from dsh_py.services.message import (
    MessageSource,
    TextBlock,
    ToolCallBlock,
    create_user_message,
)

# wire 稳定的服务器身份（对齐 dsh 的 serverInfo.name）
SERVER_NAME = "deepseek-harness-sdk-runtime"
SERVER_VERSION = "0.0.1"

# 默认路由（对齐 dsh 服务器：未握手时与 deepseek-official 一致）
DEFAULT_PROVIDER = "deepseek-official"
DEFAULT_MODEL = "deepseek-official"


def _abspath(path: str) -> str:
    """对齐 dsh 的 ``resolve()``：相对路径相对当前进程 cwd 解析。"""
    return os.path.abspath(os.path.expanduser(path))


def blocks_from_wire(content_blocks: Any) -> list:
    """把 wire 上的 contentBlocks 还原为消息块（支持 text 与 tool-call 块）。

    其余未知块按 ``{"type": ...}`` dict 透传（对齐 dsh：content 原样直通）。
    """
    if not isinstance(content_blocks, list):
        raise TypeError("session/prompt contentBlocks must be an array")
    blocks: list = []
    for block in content_blocks:
        if not isinstance(block, dict):
            raise TypeError("each content block must be an object")
        kind = block.get("type")
        if kind == "text":
            blocks.append(TextBlock(str(block.get("text", ""))))
        elif kind == "tool-call":
            blocks.append(ToolCallBlock(
                id=str(block.get("toolCallId", block.get("id", ""))),
                name=str(block.get("name", "")),
                arguments=block.get("arguments", {}),
            ))
        else:
            blocks.append(block)  # 未知块按 wire 原样保留
    return blocks


class HarnessSdkJsonRpcServer:
    """一个 booted harness 上下文上的 SDK 协议服务器。

    :param ctx: 已装配的 :class:`AppContext`（含 agents / sessions / llm 等服务）。
    :param transport: 行传输端点（生产接 stdin/stdout，测试接内存管道）。
    :param options: ``maxTokensAsSuccess`` 等部署选项（当前预留）。
    """

    def __init__(self, ctx: AppContext, transport: JsonRpcLineTransport,
                 options: Optional[dict] = None) -> None:
        self.ctx = ctx
        self.transport = transport
        self.options = options or {}
        self.cwd = os.getcwd()
        self.provider = DEFAULT_PROVIDER
        self.model = DEFAULT_MODEL
        self.max_tokens: Optional[int] = None
        self.llm_handle: Any = None  # 兜底挂载的 llm-deepseek 插件句柄
        self._records: dict[str, dict] = {}            # sessionId -> {"agent": ...}
        self._creations: dict[str, asyncio.Task] = {}  # 进行中的会话创建
        self._disposers: list[Callable[[], Any]] = []
        self._shutdown_task: Optional[asyncio.Task] = None
        self._shutting_down = False

        # 订阅：会话日志 → session.event；agent 生命周期 → session.status
        self._disposers.append(ctx.on("session/event", self._on_session_event))
        self._disposers.append(ctx.on("agent/status", self._on_agent_status))

    # ------------------------------------------------------------------ #
    # 通知（server → client）
    # ------------------------------------------------------------------ #
    def _on_session_event(self, session: Any, event: Any) -> None:
        payload = {
            "sessionId": str(session.header.id),
            "event": {
                "type": event.type,
                "seq": event.seq,
                "time": event.time,
                "data": _encode_payload(event.data),
            },
        }
        self.transport.notify("session.event", payload)

    def _on_agent_status(self, payload: Any) -> None:
        agent = payload["agent"]
        self.transport.notify("session.status", {
            "sessionId": str(agent.session.header.id),
            "status": payload["status"],  # "running" | "idle"
        })

    # ------------------------------------------------------------------ #
    # 请求处理
    # ------------------------------------------------------------------ #
    async def initialize(self, params: dict) -> dict:
        """握手：记录路由与 cwd；无适配器时兜底挂载 DeepSeek 插件。"""
        max_tokens = params.get("maxTokens")
        if max_tokens is not None and (
            not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0
        ):
            raise TypeError("initialize maxTokens must be a positive safe integer")
        self.cwd = _abspath(str(params.get("cwd") or os.getcwd()))
        self.provider = str(params.get("provider") or DEFAULT_PROVIDER)
        self.model = str(params.get("model") or DEFAULT_MODEL)
        self.max_tokens = max_tokens
        if not self._has_adapter_for(self.provider):
            if self.provider != DEFAULT_PROVIDER:
                raise RuntimeError(f'no adapter registered for provider "{self.provider}"')
            if self.llm_handle is None:
                self.llm_handle = self.ctx.plugin(apply_deepseek, {})
        return {"serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}

    async def prompt(self, params: dict) -> dict:
        """按 sessionId 取/建会话，投递一条 user 消息，返回消息 id。"""
        session_id = str(params.get("sessionId") or "")
        if not session_id:
            raise TypeError("session/prompt sessionId is required")
        record = await self._get_or_create_session(session_id)
        message = create_user_message(
            content=blocks_from_wire(params.get("contentBlocks")),
            source=MessageSource("user"),
        )
        record["agent"].insert(message)
        return {"messageId": message.id}

    def shutdown(self):
        """幂等关闭：退订事件、取消会话 agent、卸载兜底插件。"""
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.ensure_future(self._perform_shutdown())
        return self._shutdown_task

    async def handle_request(self, method: str, params: dict) -> Any:
        """分发一个请求到类型化 handler；未知方法抛错（→ -32603）。"""
        if method == "initialize":
            return await self.initialize(params)
        if method == "session/prompt":
            return await self.prompt(params)
        if method == "shutdown":
            return await self.shutdown()
        raise RuntimeError(f"unknown DeepSeek Harness SDK runtime method: {method}")

    # ------------------------------------------------------------------ #
    # 会话管理
    # ------------------------------------------------------------------ #
    async def _get_or_create_session(self, session_id: str) -> dict:
        if self._shutting_down:
            raise RuntimeError("SDK server is shutting down")
        existing = self._records.get(session_id)
        if existing is not None:
            return existing
        pending = self._creations.get(session_id)
        if pending is not None:
            return await pending
        creation = asyncio.ensure_future(self._create_session(session_id))
        self._creations[session_id] = creation
        creation.add_done_callback(lambda _t: self._creations.pop(session_id, None))
        return await creation

    async def _create_session(self, session_id: str) -> dict:
        """prepare(session_id) + enter + create_agent（对齐 dsh 的 createSession）。"""
        if not self.ctx.has_service("sessions") or not self.ctx.has_service("agents"):
            raise RuntimeError("sessions / agents 服务未就绪")
        session = self.ctx.sessions.prepare(session_id=session_id, cwd=self.cwd)
        self.ctx.sessions.enter(session)
        agent_options = AgentOptions(provider=self.provider, model=self.model,
                                     max_tokens=self.max_tokens)
        agent = self.ctx.agents.create_agent(session, agent_options)
        record = {"agent": agent, "session": session}
        self._records[session_id] = record
        return record

    def _has_adapter_for(self, provider: str) -> bool:
        if not self.ctx.has_service("llm"):
            return False
        return any(entry.id == provider for entry in self.ctx.llm.list_providers())

    # ------------------------------------------------------------------ #
    # 关闭
    # ------------------------------------------------------------------ #
    async def _perform_shutdown(self) -> dict:
        self._shutting_down = True
        # 等 in-flight 会话创建完成（幂等清理）
        creations = list(self._creations.values())
        await asyncio.gather(*creations, return_exceptions=True)
        self._creations.clear()
        records = list(self._records.values())
        self._records.clear()
        # 退订全部事件
        while self._disposers:
            try:
                self._disposers.pop()()
            except Exception:  # noqa: BLE001 - 单个退订失败不阻断清理
                pass
        # 取消会话 agent（取消当前活动；记录本身随 ctx 生命周期回收）
        for record in records:
            try:
                record["agent"].cancel()
            except Exception:  # noqa: BLE001
                pass
        # 卸载兜底挂载的 llm-deepseek 插件
        if self.llm_handle is not None:
            try:
                self.llm_handle.dispose()
            except Exception:  # noqa: BLE001
                pass
            self.llm_handle = None
        return {}


def _encode_payload(value: Any) -> Any:
    """事件载荷 → JSON 安全结构（Message/Block/dataclass 递归编码）。"""
    from dsh_py.services.message import encode_payload
    return encode_payload(value)

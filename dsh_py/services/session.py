"""会话服务（session seam）：消息日志 + 事件总线 + 持久化（对标 dsh 的 ``dsh-session``）。

会话是「一次 Agent 交互」的追加式真相来源。每追加一条事件都会通过上下文的
``session/event`` 事件广播出去（载荷为 ``(session, event)``），插件据此做长记忆
捕获、指令投影等。``derive_messages()`` 从表面事件还原出模型可见的对话历史。

**准备 / 进入 / 恢复（对标 dsh 的 SessionStore）**：
- :meth:`SessionService.prepare` 构建会话但**不进入存储**（供恢复流程编排）；
- :meth:`SessionService.enter` 把已准备的会话登记进存储并安装清理；
- :meth:`SessionService.create` = prepare + enter（常规创建）；
- :meth:`SessionService.resume` 从持久化后端装载历史后恢复会话。

**持久化 seam（对标 dsh 的 SessionPersistence）**：sessions 服务可挂载任意
持久化后端（如 :class:`dsh_py.services.session_persistence.JsonlSessionPersistence`），
``attach_persistence()`` 之后，``create`` 自动登记会话、``append`` 自动落盘。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.message import Message


@dataclass
class SessionHeader:
    """会话的存储元信息（与消息日志分离）。"""
    version: int
    id: str
    created_at: float
    cwd: Optional[str] = None
    # epoch 级调用配置（对齐 dsh header 的 request/config 字段）：
    # provider/model/采样参数，供后续请求从 header 构建（缓存复用）
    request: Optional[dict] = None


# 表面事件类型：这些事件承载模型可见消息
SURFACE_EVENTS = ("user/message", "assistant/message", "tool/result")


@dataclass
class SessionEvent:
    """会话日志里的一条不可变事件。"""
    type: str
    seq: int
    time: float
    data: Any


class Session:
    """一次会话的事件日志与历史投影。"""

    def __init__(
        self,
        ctx: AppContext,
        session_id: str,
        cwd: Optional[str] = None,
        seed_events: Optional[list[SessionEvent]] = None,
        meta: Optional[SessionHeader] = None,
        persistence: Any = None,
    ) -> None:
        self.ctx = ctx
        self.header = meta or SessionHeader(version=0, id=session_id, created_at=time.time(), cwd=cwd)
        self.events: list[SessionEvent] = list(seed_events or [])
        # seq 从已有事件续接（resume 场景）
        self._seq = max((e.seq for e in self.events), default=0)
        # 可选持久化后端：append 时同步落盘（耐久性写）
        self._persistence = persistence

    def append(self, event_type: str, data: Any) -> SessionEvent:
        """追加一条事件到日志，并广播 ``session/event``；已挂持久化时同步落盘。"""
        self._seq += 1
        event = SessionEvent(type=event_type, seq=self._seq, time=time.time(), data=data)
        self.events.append(event)
        if self._persistence is not None:
            self._persistence.append(self.header.id, [event])
        # 广播给插件（fire-and-forget；监听器多为同步落盘/注入）
        self.ctx.emit("session/event", self, event)
        return event

    def derive_messages(self) -> list[Message]:
        """从表面事件还原模型可见的对话历史（按时间顺序）。"""
        messages: list[Message] = []
        for ev in self.events:
            if ev.type == "user/message":
                messages.append(ev.data)
            elif ev.type == "assistant/message":
                messages.append(ev.data["message"])
            elif ev.type == "tool/result":
                messages.append(ev.data["message"])
        return messages


class SessionPreparation:
    """一次「已准备但尚未发布」的会话（对标 dsh 的 SessionPreparation）。

    - ``commit()``：标记已发布（发布方调用，例如进入存储并广播后）。
    - ``dispose()``：释放未发布的预留（本实现无预留资源，仅做标记与幂等）。
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self._committed = False

    @staticmethod
    def create(session: Session) -> "SessionPreparation":
        """包装一个已准备的会话。"""
        return SessionPreparation(session)

    def commit(self) -> None:
        """提交发布（幂等）。"""
        self._committed = True

    def dispose(self) -> None:
        """释放预留（幂等；未发布即丢弃）。"""
        self._committed = False


class SessionService(Service):
    """``sessions`` 服务：会话存储器，``ctx.sessions``。"""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "sessions")
        self._store: dict[str, Session] = {}
        self._persistence: Any = None  # 可选持久化后端（SessionPersistence 子类）

    # ------------------------------------------------------------------ #
    # 持久化 seam
    # ------------------------------------------------------------------ #
    def attach_persistence(self, backend: Any) -> None:
        """挂载持久化后端；此后 ``create`` 自动登记、``append`` 自动落盘。"""
        self._persistence = backend

    def has_persistence(self) -> bool:
        return self._persistence is not None

    # ------------------------------------------------------------------ #
    # 准备 / 进入 / 创建 / 恢复
    # ------------------------------------------------------------------ #
    def prepare(
        self,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        seed_events: Optional[list[SessionEvent]] = None,
        meta: Optional[SessionHeader] = None,
    ) -> Session:
        """构建一个会话但**不进入存储**（对标 dsh 的 ``sessions.prepare``）。"""
        sid = session_id or uuid.uuid4().hex
        return Session(self.ctx, sid, cwd=cwd, seed_events=seed_events, meta=meta,
                       persistence=self._persistence)

    def enter(self, session: Session) -> None:
        """把已准备的会话登记进存储（对标 dsh 的 ``sessions.enter``）。"""
        self._store[session.header.id] = session

    def create(self, cwd: Optional[str] = None, persist: bool = True) -> Session:
        """创建并返回一个全新会话（prepare + enter）。

        :param persist: 已挂持久化后端时是否登记落盘（缺省 True）。
        """
        session = self.prepare(cwd=cwd)
        self.enter(session)
        if persist and self._persistence is not None:
            self._persistence.create(session.header)
        return session

    def resume(self, session_id: str) -> Session:
        """从持久化后端恢复会话历史（对标 dsh 的 resume 语义）。

        :raises RuntimeError: 未挂载持久化后端，或后端中不存在该会话。
        """
        if self._persistence is None:
            raise RuntimeError("未配置持久化后端：请先加载 sessionPersistence 插件")
        inspection = self._persistence.load(session_id)
        if inspection is None:
            raise RuntimeError(f"持久化后端中不存在会话 {session_id!r}")
        session = self.prepare(meta=inspection["meta"], seed_events=inspection["events"])
        self.enter(session)
        return session

    def get(self, session_id: str) -> Optional[Session]:
        return self._store.get(session_id)

    def list(self) -> list[str]:
        """列出当前已登记的全部会话 id。"""
        return list(self._store.keys())


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``sessions`` 服务（会话日志 + 事件广播 + 持久化 seam）。

    本服务本身也是「一切皆插件」的一等公民：想换成别的会话实现（例如持久化到
    SQLite 的版本），提供另一个注册了同名 ``sessions`` 服务的插件即可。
    """
    SessionService(ctx)


apply.provides = ["sessions"]  # 声明：本插件提供 sessions 服务（供 loader 拓扑排序）
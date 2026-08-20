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
    # 谱系：父会话 id（subagent 派生场景；traceSession 沿此链）
    parent_session: Optional[str] = None


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
        # 表面（surface）：模型可见节点的 seq 列表 + 重写代数（compaction 替换后递增）
        self._surface_nodes: list[int] = [e.seq for e in self.events if e.type in SURFACE_EVENTS]
        self._replace_generation = 0
        # 表面替换记录（surface fold 的 replacements：{newSeq, shadowedSeqs}）
        self._replacements: list[dict] = []
        # 最近一次路由请求的 header（system/tools/config；compaction 摘要前缀复用）
        self._request_header: Optional[dict] = None

    @property
    def seq(self) -> int:
        """当前已追加的事件数（最后一个事件的 seq；空日志为 0）。"""
        return self._seq

    @property
    def surface(self) -> dict:
        """当前表面：``{"nodes": [...], "replace_generation": int}``。

        表面节点是模型可见事件（user/assistant/tool-result）的 seq 列表，按表面
        顺序排列。compaction 的 surface replace 会用高 seq 摘要节点替换一段
        节点并递增 ``replace_generation``——替换后表面 seq 不再单调。
        """
        return {"nodes": list(self._surface_nodes), "replace_generation": self._replace_generation}

    @property
    def request_header(self) -> Optional[dict]:
        """最近一次路由请求的 header（含 system/tools/config；compaction 摘要复用）。"""
        return self._request_header

    @request_header.setter
    def request_header(self, value: Optional[dict]) -> None:
        self._request_header = value

    def append(self, event_type: str, data: Any, surface_op: Optional[dict] = None,
               source_event_seqs: Optional[list[int]] = None) -> SessionEvent:
        """追加一条事件到日志，并广播 ``session/event``；已挂持久化时同步落盘。

        :param surface_op: 表面操作（对齐 dsh 的 ``surfaceOp``）。``{"op":
            "replace", "start": s, "end": e}`` 表示本条事件替换表面节点区间
            ``[s, e]``（s/e 为表面节点 seq）；缺省时表面事件追加到表面末尾。
        :param source_event_seqs: 本条事件的来源事件 seq 列表（对齐 dsh 的
            ``sourceEventSeqs``，供溯源）。
        """
        self._seq += 1
        event = SessionEvent(type=event_type, seq=self._seq, time=time.time(), data=data)
        self.events.append(event)
        if surface_op is not None:
            if surface_op.get("op") == "replace":
                self._apply_surface_replace(surface_op["start"], surface_op["end"], event.seq)
        elif event_type in SURFACE_EVENTS:
            self._surface_nodes.append(event.seq)
        if self._persistence is not None:
            self._persistence.append(self.header.id, [event])
        # 广播给插件（fire-and-forget；监听器多为同步落盘/注入）
        self.ctx.emit("session/event", self, event)
        return event

    def _apply_surface_replace(self, start: int, end: int, new_seq: int) -> None:
        """用新节点 seq 替换表面中区间 ``[start, end]`` 的节点（递增重写代数）。"""
        indices = [i for i, s in enumerate(self._surface_nodes) if start <= s <= end]
        if not indices:
            raise RuntimeError(f"surface replace: 表面中未找到区间 [{start}, {end}] 内的节点")
        shadowed = self._surface_nodes[indices[0]:indices[-1] + 1]
        self._surface_nodes[indices[0]:indices[-1] + 1] = [new_seq]
        self._replace_generation += 1
        self._replacements.append({"newSeq": new_seq, "shadowedSeqs": list(shadowed)})

    def derive_event_message(self, event: SessionEvent) -> Any:
        """把一条表面事件还原为模型可见消息；非表面事件返回 None。"""
        if event.type == "user/message":
            return event.data
        if event.type == "assistant/message":
            return event.data["message"]
        if event.type == "tool/result":
            return event.data["message"]
        return None

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

    def flush(self, session: Session) -> None:
        """确保会话已提交事件全部耐久（投影缓存写前的持久性屏障）。

        本实现的 ``append`` 为同步落盘（原子提交），落盘即已耐久；此方法保留
        以对齐 dsh 的 ``sessions.flush`` 语义——缓存行必须**不早于**其覆盖的
        日志事件落地，崩溃可让缓存落后于日志（更长尾部重放），绝不超前（幽灵值）。
        """

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
        """创建并返回一个全新会话（prepare + enter，经 SessionPreparation 屏障）。

        :param persist: 已挂持久化后端时是否登记落盘（缺省 True）。
        """
        session = self.prepare(cwd=cwd)
        preparation = SessionPreparation.create(session)
        try:
            self.enter(session)
            if persist and self._persistence is not None:
                self._persistence.create(session.header)
            preparation.commit()  # 发布成功：标记已提交
            return session
        except Exception:
            preparation.dispose()  # 发布失败：释放未发布的预留
            raise

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
        preparation = SessionPreparation.create(session)
        try:
            self.enter(session)
            preparation.commit()
            return session
        except Exception:
            preparation.dispose()
            raise

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
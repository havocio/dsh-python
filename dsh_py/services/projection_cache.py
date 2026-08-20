"""持久化投影缓存（projection-cache）：每个已注册投影单元状态的耐久检查点
（对标 dsh 的 ``dsh-session-projection-cache``）。

缓存是**折叠捷径，绝非权威**：一行可能陈旧（其 ``seq`` 说明有多旧）但绝不错误，
所以每条写路径 fail-soft（一次丢失写只让下次冷读做更长的尾部重放），``ver``
不匹配则丢弃整行而非迁移。

**写路径（write-behind）**：``writeEveryEvents``（条）与 ``writeIntervalMs``
（毫秒）两个节流触发器，加上两个强制点——``turn/end`` 与会话 detach
（live-to-cold 时刻），强制点总是触发。

**读阶梯（冷读）**：缓存行 → 持久化 ``read_from`` 尾部 → 注册表 ``restore``
（重折 + 写回），使下次冷读起点更近。日志收缩（崩溃修复截断）导致缓存行失效时
触发一次从 seq 0 的全量重读——阶梯的慢档，仍不崩溃。
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.session import Session, SessionHeader
from dsh_py.services.storage_kv import KvTable


class SessionProjectionCache(Service):
    """``sessionProjectionCache`` 服务：持久化的投影检查点缓存（``ctx.sessionProjectionCache``）。

    配置（可选）：
    - ``writeEveryEvents``：两次强制点之间，每 N 条已提交事件强制一次耐久写（默认 10）；
    - ``writeIntervalMs``：脏检查点最长未写时长（默认 5000）；
    - ``path``：KV 表文件路径（缺省内存态，测试友好）。
    """

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx, "sessionProjectionCache")
        cfg = config or {}
        self._write_every_events = int(cfg.get("writeEveryEvents", 10))
        self._write_interval_ms = int(cfg.get("writeIntervalMs", 5000))
        self._table = KvTable(cfg.get("path"))
        self._dirty: dict[int, dict] = {}  # id(session) -> {pending, timer}
        # 节流触发器只在脏时点火（mark_clean 连同计数清掉计时器），两个强制点无条件写。
        ctx.on("session/event", self._on_event)
        ctx.on("session/disposed", self._on_disposed)

    # ------------------------------------------------------------------ #
    # 读侧（冷读阶梯）
    # ------------------------------------------------------------------ #
    def cached_snapshot(self, meta: SessionHeader) -> Optional[dict]:
        """零 IO 列表读：直接从存储行查看整值（版本匹配的键）。

        返回 ``{"as_of_seq": int, "values": {...}}``；无可用行时返回 None。
        值与上次耐久检查点一样陈旧但绝不错误，且绝不来自无关日志
        （调用方持有的 header 是身份见证）。
        """
        record = self._record_for(meta.id, self._identity_of(meta))
        if record is None:
            return None
        values = self.ctx.sessionProjections.view_checkpoint(record["rows"])
        if not values:
            return None
        # 该块携带一个切面：最低可用水印即每条值至少新于的 seq
        # （低报在 higher-seq-wins 下安全；高报会让陈旧值压过新推送）。
        as_of_seq = min(record["rows"][key]["seq"] for key in values)
        return {"as_of_seq": as_of_seq, "values": values}

    def cold_snapshot(self, session_id: str, signal: Any = None) -> dict:
        """冷读一个持久化会话的投影：缓存行 + 持久化尾部重放（零全量日志加载）。

        无投影单元注册时仅做存在性探测（日志缺失抛错）。缓存行被收缩日志失效
        （崩溃修复截断）时触发一次从 seq 0 的全量重读。
        """
        record = self._table.get(session_id)
        cached = record["rows"] if record is not None else {}
        floor = self.ctx.sessionProjections.restore_floor(cached)
        if floor is None:
            probe = self.ctx.sessionPersistence.read_from(session_id, 0, signal) \
                if self._supports_signal() else self.ctx.sessionPersistence.read_from(session_id, 0)
            if probe is None:
                raise RuntimeError(f"持久化日志中不存在会话 {session_id!r}")
            return {
                "as_of_seq": probe["events"][-1].seq if probe["events"] else -1,
                "values": {},
            }
        tail = self.ctx.sessionPersistence.read_from(session_id, floor, signal) \
            if self._supports_signal() else self.ctx.sessionPersistence.read_from(session_id, floor)
        if tail is None:
            raise RuntimeError(f"持久化日志中不存在会话 {session_id!r}")
        # 尾部的存储 header 是身份见证：绑定到不同生命周期（重建的 id、被换的存储）
        # 的记录整条丢弃，先于任何一行播种折叠。
        related = record is None or self._identity_matches(record["identity"], self._identity_of(tail["meta"]))
        try:
            if not related:
                raise RuntimeError("unrelated log identity")
            restored = self.ctx.sessionProjections.restore(cached, tail["events"], floor)
        except Exception:
            # 可恢复的 restore 失败：无关记录、或行越过日志末端/早于底。
            # 二者都意味着 floor > 0，故全量日志是一次全新读取。
            whole = self.ctx.sessionPersistence.read_from(session_id, 0)
            if whole is None:
                raise RuntimeError(f"持久化日志中不存在会话 {session_id!r}")
            restored = self.ctx.sessionProjections.restore({}, whole["events"], 0)
        self._put_soft(session_id, self._identity_of(tail["meta"]), restored["checkpoint"], "cold-read write-back")
        return restored["snapshot"]

    # ------------------------------------------------------------------ #
    # 写路径（write-behind + 强制点）
    # ------------------------------------------------------------------ #
    def write(self, session: Session) -> None:
        """立即对**活会话**做一次耐久检查点（两个强制点调用；测试/载体也可调用）。

        非 fail-soft——fail-soft 路径上的调用方自行包裹。检查点切面先于持久性
        屏障截取：先 flush 日志再落缓存行，保证崩溃让缓存落后于日志（更长尾部
        重放）而绝不超前（从无日志包含的事件折叠出的幽灵值）。
        """
        rows = self.ctx.sessionProjections.checkpoint(session)
        self._mark_clean(session)
        if self.ctx.sessions.get(session.header.id) is session:
            self.ctx.sessions.flush(session)
        self._put(session.header.id, self._identity_of(session.header), rows)

    def _on_event(self, session: Session, event: Any) -> None:
        if event.type == "turn/end":
            self._flush_soft(session, "turn/end")
            return
        state = self._dirty.setdefault(id(session), {"pending": 0, "timer": None})
        state["pending"] += 1
        if state["pending"] >= self._write_every_events:
            self._flush_soft(session, "count threshold")
            return
        if state["timer"] is None:
            timer = threading.Timer(
                self._write_interval_ms / 1000.0,
                lambda: self._flush_soft(session, "interval"),
            )
            timer.daemon = True
            state["timer"] = timer
            timer.start()

    def _on_disposed(self, session: Session) -> None:
        # detach（live-to-cold 时刻）是第二个强制点；此后冷读阶梯从缓存服务该会话。
        self._flush_soft(session, "detach")
        self._mark_clean(session)
        self._dirty.pop(id(session), None)

    def _flush_soft(self, session: Session, trigger: str) -> None:
        """一次 fail-soft 耐久检查点：失败记 warn，缓存保持陈旧，下次自愈。"""
        try:
            self.write(session)
        except Exception as exc:  # noqa: BLE001 - 缓存写永不失败调用方的读/事件路径
            logger = getattr(self.ctx, "logger", None)
            if logger is not None:
                logger.warn(f"session projection cache: {trigger} write for {session.header.id!r} failed (cache stays stale): {exc}")

    def _mark_clean(self, session: Session) -> None:
        state = self._dirty.get(id(session))
        if state is None:
            return
        state["pending"] = 0
        if state["timer"] is not None:
            state["timer"].cancel()
            state["timer"] = None

    # ------------------------------------------------------------------ #
    # 存储
    # ------------------------------------------------------------------ #
    def _put(self, session_id: str, identity: dict, rows: dict) -> None:
        self._table.put(session_id, {"identity": identity, "rows": rows})

    def _put_soft(self, session_id: str, identity: dict, rows: dict, what: str) -> None:
        try:
            self._put(session_id, identity, rows)
        except Exception as exc:  # noqa: BLE001 - 缓存写永不失败读路径
            logger = getattr(self.ctx, "logger", None)
            if logger is not None:
                logger.warn(f"session projection cache: {what} for {session_id!r} failed (cache stays stale): {exc}")

    def _record_for(self, session_id: str, expected: dict) -> Optional[dict]:
        record = self._table.get(session_id)
        if record is None:
            return None
        return record if self._identity_matches(record["identity"], expected) else None

    @staticmethod
    def _identity_of(header: SessionHeader) -> dict:
        identity: dict = {"createdAt": header.created_at}
        if header.cwd is not None:
            identity["cwd"] = header.cwd
        return identity

    @staticmethod
    def _identity_matches(stored: dict, expected: dict) -> bool:
        return stored.get("createdAt") == expected.get("createdAt") and stored.get("cwd") == expected.get("cwd")

    @staticmethod
    def _supports_signal() -> bool:
        # 当前持久化后端的 read_from 未接 signal；保持签名兼容（冷读不中断）。
        return False

    def dispose(self) -> None:
        """清理节流计时器并落盘（插件卸载/测试收尾调用）。"""
        for state in self._dirty.values():
            if state["timer"] is not None:
                state["timer"].cancel()
        self._dirty.clear()
        self._table.close()


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``sessionProjectionCache`` 服务（持久化投影缓存）。"""
    SessionProjectionCache(ctx, config)


apply.provides = ["sessionProjectionCache"]
apply.inject = ["sessionProjections", "sessionPersistence", "sessions"]

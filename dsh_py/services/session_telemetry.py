"""会话遥测能力：捕获协调器 + 后端抽象 seam（对标 dsh 的 ``session-telemetry``）。

本包只拥有**捕获侧**——哪些记录存在（固定分块投影）、它们携带什么（逻辑记录
模型）、何时捕获（live 采纳 / 按需回放）、以及 HMR（热重载）游标。越过本包
:class:`SessionTelemetryBackend.emit` 之后的一切（批处理、重试、队列、丢包策略）
归上报 SDK 所有，本包**刻意不建模**。设计权衡见 dsh 的 subsystem 文档。

边界公理：「harness 的切面止于 ``emit()``」。本协调器同步地把记录交给后端
（``SessionTelemetrySink.emit``），批处理/重试/队列/丢包是 SDK 的事。

本模块零外部依赖（不引入 OpenTelemetry），仅为捕获契约 + 协调器；具体上报后端
由 ``session_telemetry_otel`` 等实现。
"""

from __future__ import annotations

import copy
import time
import weakref
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Protocol

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service


# 逻辑记录通道：ledger = 会话日志镜像（每条事件一对一）；ops = 运维信号（无日志归属）。
SessionTelemetryChannel = Literal["ledger", "ops"]
# 预映射的告警级别（捕获期定级，后端零配置即可告警）。
SessionTelemetrySeverity = Literal["info", "warn", "error"]
# 部署披露给用户的共享策略（后端无关词汇，与 OTel 后端的序列化模式一致）。
SessionTelemetrySharingStatus = Literal["full", "feedback-only", "disabled"]
# 捕获模式：跟随实时事件流，还是仅在显式请求时回放标准日志。
SessionTelemetryCapture = Literal["live", "on-demand"]


@dataclass
class SessionTelemetryRecord:
    """交给后端的一条逻辑记录——捕获契约的全部对外词汇。

    ledger 记录与会话日志事件一一对应；ops 记录（``channel='ops'``，如
    ``agent-error`` / ``shutdown``）携带两个无日志归属的信号，且**故意不带**
    ``event.seq`` 式身份，以免被误认为 ledger 行。
    """

    channel: SessionTelemetryChannel
    # Unix 纪元秒（ledger 取源事件 append 时间；ops 取发射时间）。
    time: float
    # 预映射告警级别。
    severity: SessionTelemetrySeverity
    # 身份属性，刻意精简：ledger 带 session.id / event.type / event.seq，以及
    # header 上可用的 cwd / parent_id / seed_length；ops 带 telemetry.op /
    # session.id，以及（agent-error）agent.id / turn / step / error.name。
    attributes: dict[str, Any]
    # 完整载荷：ledger 是会话事件 ``data`` 的深拷贝（``Session.append`` 的校验保证
    # JSON 可序列化且不随后被改写）；ops 是 op 载荷。交接后绝不改写。
    body: Any


class SessionTelemetrySink(Protocol):
    """协调器所需的后端最小契约。

    :meth:`emit` 必须是**非阻塞入队**——协调器在 ``session/event`` 热路径或显式
    日志回放中同步调用它，慢于队列推送的操作会拖慢 agent 循环或反馈处理。抛错由
    协调器 containment 捕获并记日志，绝不外泄到调用方。
    """

    def emit(self, record: SessionTelemetryRecord) -> None: ...

    # 可选的 turn 结束提示（fire-and-forget）。多数后端不实现，交给 SDK 自身批处理
    # 节奏。OTel 后端刻意不实现，以规避与 shutdown 内部 drain 的并发刷新交互。
    def flush(self) -> None: ...

    # 把 fiber 卸载转交 SDK：冲刷队列并静默（按 SDK 自身 shutdown 契约）。
    def shutdown(self) -> None: ...


@dataclass
class _ProjectedRecord:
    """一条已投影、待交给后端的记录。"""

    record: SessionTelemetryRecord
    # 仅 ledger 记录带 seq：交接游标只在后端接受后才推进。
    seq: Optional[int] = None


# 交接游标：按会话记录已交给后端的最高 seq。刻意是**模块级**环境态（cordis 无 HMR
# 状态交接 API），按 Session 对象键控（会话对象属于会话存储、比任何遥测 fiber 都长
# 寿），是唯一能在重载后「续传而非重交历史」的进程内生命周期。条目随会话消亡；缺失
# 条目安全等价于「重新交接一切」。只在 emit 时推进——标记的是已交接，不是已送达。
_HANDOFF_CURSOR: "weakref.WeakKeyDictionary[Any, int]" = weakref.WeakKeyDictionary()


class SessionTelemetryBackend(Service, ABC):
    """遥测后端抽象（注册为 ``ctx.sessionTelemetry``）。

    每个上下文一个实现——cordis 的 ``Service`` 注册（键 ``sessionTelemetry``）重复
    注册即抛错（标准行为）。后端在构造器中组合一个
    :class:`SessionTelemetryCoordinator` 以安装捕获侧。
    """

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "sessionTelemetry")

    @property
    @abstractmethod
    def sharing(self) -> SessionTelemetrySharingStatus:
        """部署选定的共享策略，供确认类人机界面披露（如 ``/feedback`` 的确认文案）。"""
        ...

    @abstractmethod
    def emit(self, record: SessionTelemetryRecord) -> None:
        """见 :class:`SessionTelemetrySink.emit`——该声明是契约唯一归属。"""
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """见 :class:`SessionTelemetrySink.shutdown`。"""
        ...


def _clone(obj: Any) -> Any:
    """深拷贝事件数据以交予后端；不可拷贝时退化为原引用（canonical 日志永不改写）。"""
    try:
        return copy.deepcopy(obj)
    except Exception:  # noqa: BLE001 - 兜底：保持事件数据不可变的前提下交原对象
        return obj


def _event_turn_step(event: Any) -> tuple[Optional[int], Optional[int]]:
    """从 assistant/chunk 事件取 ``(turn, step)``（兼容 dict 与属性两种 data 形状）。"""
    data = getattr(event, "data", None)
    if isinstance(data, dict):
        return data.get("turn"), data.get("step")
    return getattr(data, "turn", None), getattr(data, "step", None)


def severity_of(event: Any) -> SessionTelemetrySeverity:
    """把事件自身的结局标记映射为预烘焙的告警级别。

    工具结果的 ``isError``、``turn/end`` 的 error 原因，以及 agent-error 运维记录
    → ``error``；其余默认 ``info``。该 coordinator 不依赖的事件类型（含插件合并进来
    的未知类型）一律按 ``info`` 透传，其结局语义归各所有者所有。
    """
    etype = getattr(event, "type", None)
    data = getattr(event, "data", None)
    if etype == "tool/result":
        msg = data.get("message") if isinstance(data, dict) else getattr(data, "message", None)
        content = getattr(msg, "content", None) if msg is not None else None
        if content:
            first = content[0]
            return "error" if getattr(first, "is_error", False) else "info"
        return "info"
    if etype == "turn/end":
        reason = data.get("reason") if isinstance(data, dict) else getattr(data, "reason", None)
        kind = None
        if isinstance(reason, dict):
            kind = reason.get("kind")
        elif reason is not None:
            kind = getattr(reason, "kind", None)
        return "error" if kind == "error" else "info"
    return "info"


def identity_of(session: Any, event: Any) -> dict[str, Any]:
    """构建最小化身份属性：信封 + 自包含 header 事实。"""
    attributes: dict[str, Any] = {
        "session.id": str(session.header.id),
        "event.type": getattr(event, "type", ""),
        "event.seq": getattr(event, "seq", 0),
    }
    header = session.header
    cwd = getattr(header, "cwd", None)
    if cwd is not None:
        attributes["session.cwd"] = cwd
    parent = getattr(header, "parent_session", None)
    if parent is not None:
        attributes["session.parent_id"] = str(parent)
    seed = getattr(header, "seed_length", None)
    if seed:  # 0 表示无 fork 前缀，省略
        attributes["session.seed_length"] = seed
    return attributes


def error_detail(error: Any) -> dict[str, str]:
    """把实时总线任意抛值规范化为稳定的运维记录形状。"""
    if isinstance(error, BaseException):
        return {"name": type(error).__name__, "message": str(error)}
    return {"name": "Error", "message": str(error)}


def shutdown_record(session: Any) -> SessionTelemetryRecord:
    """构造每会话的干净退出标记：在会话自身终止边缘，或协调器卸载（仍存活）时发射。"""
    return SessionTelemetryRecord(
        channel="ops",
        time=time.time(),
        severity="info",
        attributes={"telemetry.op": "shutdown", "session.id": str(session.header.id)},
        body={"op": "shutdown"},
    )


class SessionTelemetryCoordinator:
    """遥测能力的捕获协调器。

    live 捕获订阅会话火流 + 唯一实时总线转发（``agent/error``）；两条捕获路径都套用
    固定分块投影、构造逻辑记录、各跑一遍 ``session-telemetry/record`` 瀑布（部署挂载
    的脱敏规则；无规则时透传），然后交给后端。每个同步处理器自包含，因此故障后端绝不
    会饿死其他订阅者或触碰 agent 循环。由后端在构造器中组合。

    on-demand 捕获不注册任何连续监听器；:meth:`captureSession` 仅在被显式请求时回放
    标准日志，且绝不创建 ops 记录。
    """

    def __init__(
        self,
        ctx: AppContext,
        backend: SessionTelemetrySink,
        capture: SessionTelemetryCapture = "live",
    ) -> None:
        self.ctx = ctx
        self.backend = backend
        # 本 fiber 采纳且仍存活的会话（双重采纳防护 + 卸载清扫）；session/disposed 标记并退役。
        self.adopted: set[Any] = set()
        # 每会话、已发首块的 (turn, step) 键集合（重新采纳时由日志重建）。
        self.chunk_seen: "weakref.WeakKeyDictionary[Any, set]" = weakref.WeakKeyDictionary()

        if capture == "live":
            ctx.on("session/created", lambda session: self.contain(lambda: self.adopt(session)))
            ctx.on("session/disposed", self._on_session_disposed)
            ctx.on("session/event", lambda s, e: self.contain(lambda: self.capture_event(s, e)))
            ctx.on("session/flush", lambda s: self.contain(lambda: self.hint_flush(s)))
            ctx.on("agent/error", self._on_agent_error)
            # 热重载不会重放 session/created：这里清扫已存活会话，让重新采纳的 fiber 续传。
            for sid in ctx.sessions.list():
                session = ctx.sessions.get(sid)
                if session is not None:
                    self.adopt(session)

        # 卸载：仍为存活的采纳会话先捕获 shutdadow 标记，再 await 后端 shutdown；
        # 那里失败仅告警，绝不令应用拆除失败（best-effort 上报）。
        ctx.effect(self._dispose, "telemetry capture")

    # ------------------------------------------------------------------ #
    # 公共：按需回放（on-demand 后端在反馈事件到达时调用）
    # ------------------------------------------------------------------ #
    def capture_session(self, session: Any, through_seq: Optional[int] = None) -> None:
        """投影并交接游标之后的标准会话日志后缀，可选停在含边界 seq。

        脱敏在调用时执行，因此按需调用方在请求前不保留任何拷贝记录，使用的是当时
        挂载的策略。后端与策略失败仍按事件 containment，不饿死同次回放中的后续事件。
        """
        cursor = _HANDOFF_CURSOR.get(session, session.first_live_seq - 1)
        for event in session.events:
            if through_seq is not None and event.seq > through_seq:
                break
            self.contain(
                lambda ev=event: (
                    self.track(session, ev) if ev.seq <= cursor else self.capture_event(session, ev)
                )
            )

    # ------------------------------------------------------------------ #
    # 私有：采纳 / 投影 / 交接
    # ------------------------------------------------------------------ #
    def adopt(self, session: Any) -> None:
        """采纳一个会话：从交接游标处回放日志（≤游标半为只投影不交接），之后靠火流续传。

        无游标存活时，回放从会话构造边界（``first_live_seq``）起、而非 seq 0——构造期
        种子永不_publish 于火流，且其内容已在另一身份下离开进程（前进程的 resume 同 id，
        或 fork 的父流，由接收方按 ``session.seed_length`` 缝合）。游标处及以下的事件
        仍喂给投影状态（首块追踪）而不重交，因此 resume 会像见证了该步开始的 fiber 一样
        精确丢弃中段续传块。
        """
        if session in self.adopted:
            return
        self.adopted.add(session)
        self.capture_session(session)

    def track(self, session: Any, event: Any) -> None:
        """喂分块投影但不交接（重新采纳时游标以下半段）。"""
        if event.type == "assistant/chunk":
            turn, step = _event_turn_step(event)
            self.seen(session).add(f"{turn}:{step}")

    def capture_event(self, session: Any, event: Any) -> None:
        """投影、脱敏、交接单条事件给后端。"""
        if event.type == "assistant/chunk":
            turn, step = _event_turn_step(event)
            key = f"{turn}:{step}"
            seen = self.seen(session)
            # 固定分块投影：每个 (turn, step) 只发首块——流起始信号；内容在步的组装后
            # assistant/message 中字节完整。丢弃的块不推进游标，故重新采纳会确定性重丢。
            if key in seen:
                return
            seen.add(key)
        self.deliver(
            session,
            _ProjectedRecord(
                record=self.redact(
                    SessionTelemetryRecord(
                        channel="ledger",
                        time=event.time,
                        severity=severity_of(event),
                        attributes=identity_of(session, event),
                        body=_clone(event.data),
                    )
                ),
                seq=event.seq,
            ),
        )

    def redact(self, record: SessionTelemetryRecord) -> SessionTelemetryRecord:
        """在捕获期跑 ``session-telemetry/record`` 瀑布。

        最内层 ``next()`` 透传未改记录——本包不自带规则；导出数据有多干净取决于部署
        挂载了什么规则。调用方都跑在 :meth:`contain` 内，因此抛错的规则 fail-closed
        扣留该记录，绝不抵达 agent 循环。按需捕获在本调用中（读标准日志时）跑瀑布，而非
        事件 append 时。
        """
        return self.ctx.waterfall("session-telemetry/record", record, inner=lambda: record)

    def deliver(self, session: Any, pending: _ProjectedRecord) -> None:
        """交接一条已脱敏记录给后端，再推进其 ledger 游标。"""
        self.backend.emit(pending.record)
        if pending.seq is not None:
            _HANDOFF_CURSOR[session] = pending.seq

    def hint_flush(self, session: Any) -> None:
        """把 turn 结束边界转交后端可选的 flush 提示。"""
        if session in self.adopted:
            flush = getattr(self.backend, "flush", None)
            if callable(flush):
                try:
                    flush()
                except Exception as exc:  # noqa: BLE001 - 提示是 fire-and-forget
                    self._warn(f"telemetry: backend flush failed: {exc}")

    def relay_agent_error(self, agent: Any, turn: Any, step: Any, error: Any) -> None:
        """把一个 ``agent/error`` 总线发射转交为 agent-error 运维记录。"""
        detail = error_detail(error)
        self.deliver(
            agent.session,
            _ProjectedRecord(
                record=SessionTelemetryRecord(
                    channel="ops",
                    time=time.time(),
                    severity="error",
                    attributes={
                        "telemetry.op": "agent-error",
                        "session.id": str(agent.session.header.id),
                        "agent.id": getattr(agent, "id", None),
                        "error.name": detail["name"],
                        "turn": turn,
                        "step": step,
                    },
                    body=detail,
                )
            ),
        )

    def seen(self, session: Any) -> set:
        """惰性创建每会话首块追踪集合。"""
        existing = self.chunk_seen.get(session)
        if existing is None:
            existing = set()
            self.chunk_seen[session] = existing
        return existing

    def contain(self, step: Any) -> None:
        """带异常 containment 地跑一步捕获侧：cordis emit 是 stop-on-throw，因此抛错
        监听器会饿死其后注册的所有订阅者——后端不得有任何东西外泄。"""
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - 单点扣留，fail-closed
            self._warn(f"telemetry: capture step failed: {exc}")

    # ------------------------------------------------------------------ #
    # 监听器包装
    # ------------------------------------------------------------------ #
    def _on_session_disposed(self, session: Any) -> None:
        def step() -> None:
            if session not in self.adopted:
                return
            self.adopted.discard(session)
            self.deliver(session, _ProjectedRecord(record=shutdown_record(session)))

        self.contain(step)

    def _on_agent_error(self, agent: Any, turn: Any, step: Any, error: Any) -> None:
        self.contain(lambda: self.relay_agent_error(agent, turn, step, error))

    def _dispose(self) -> None:
        """fiber 卸载：先为仍采纳的存活会话捕获 shutdown 标记，再 shutdown 后端。"""
        for session in list(self.adopted):
            self.contain(
                lambda s=session: self.deliver(s, _ProjectedRecord(record=shutdown_record(s)))
            )
        try:
            self.backend.shutdown()
        except Exception as exc:  # noqa: BLE001 - best-effort：绝不失败应用拆除
            self._warn(f"telemetry: backend shutdown failed: {exc}")

    def _warn(self, message: str) -> None:
        try:
            self.ctx.logger.warn(message)
        except Exception:  # noqa: BLE001 - 日志失败不可影响捕获
            pass

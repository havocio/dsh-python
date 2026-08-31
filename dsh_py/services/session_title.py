"""会话标题服务（session-title）：确定性回退标题 + 可选的模型 provider（对标 dsh 的 ``dsh-session-title``）。

本服务拥有：

- 纯函数标题归一化（去除控制字符 / UTF-8 字节截断 / 首条消息回退标题）；
- 日志真相来源 ``session/title`` 事件（仅追加、永不进入模型表面或派生历史）；
- 每会话并发状态：最近一次回退标题的确定性生成、以及（当注册了 provider 时）
  自动生成调度（first-prompt / all-prompts 两种节奏）；
- 一个 ``title`` 投影单元（仅当 ``sessionProjections`` 服务被装配时注册），
  供会话列表行读取最新标题字符串。

provider 通过 :meth:`SessionTitleService.register` 注册（至多一个），其 ``generate``
生产一次标题修订；服务负责归一化、校验与日志接受。显式 :meth:`rename` 会钉住标题
（停止后续自动生成），:meth:`refresh` 则是解除钉定的显式重试。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, NewType, Optional, Union

from dsh_py.core.context import AppContext
from dsh_py.core.fiber import FiberState
from dsh_py.core.schema import SchemaError
from dsh_py.core.service import Service
from dsh_py.core import schema as z
from dsh_py.core.signal import CancelSignal, SignalCancelledError
from dsh_py.services.message import MessageSource, TextBlock
from dsh_py.services.session import Session, SessionEvent

# 品牌化 provider id（运行期仍是字符串，类型检查可区分）
SessionTitleProviderId = NewType("SessionTitleProviderId", str)


# --------------------------------------------------------------------------- #
# 类型
# --------------------------------------------------------------------------- #
#: 生成标题所用的精确辅助模型路由。
@dataclass(frozen=True)
class SessionTitleModelProvenance:
    provider: str
    model: str


#: 已接受标题的持久所有权记录。
@dataclass(frozen=True)
class SessionTitleSource:
    kind: str  # 'fallback' | 'provider' | 'user'
    provider: Optional[SessionTitleProviderId] = None  # kind=='provider'
    model: Optional[SessionTitleModelProvenance] = None  # kind=='provider'


#: ``session/title`` 事件载荷（仅日志）。
@dataclass(frozen=True)
class SessionTitleEventData:
    title: str
    message_seqs: tuple[int, ...]
    source: SessionTitleSource


#: 最新折叠标题 + 标题事件的持久信封事实。
@dataclass(frozen=True)
class SessionTitleSnapshot(SessionTitleEventData):
    event_seq: int
    updated_at: float


#: 服务配置（确定性回退与接受上限）。
@dataclass(frozen=True)
class Config:
    fallback_max_words: int
    fallback_max_bytes: int
    max_title_bytes: int


#: 显式用户标题归一化后为空时的拒绝（唯一归咎输入的 rename 失败）。
class SessionTitleInvalidError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.name = "SessionTitleInvalidError"


#: 一条供 provider 使用的合格人类文本消息。
@dataclass(frozen=True)
class SessionTitleUserMessage:
    seq: int
    text: str


#: 自动生成节奏（由注册的 provider 拥有）。
SessionTitleAutomaticMode = str  # 'first-prompt' | 'all-prompts'


#: provider 调用所需的不可变输入。
@dataclass(frozen=True)
class SessionTitleProviderRequest:
    session: Session
    messages: tuple[SessionTitleUserMessage, ...]
    route: Optional[SessionTitleModelProvenance] = None
    signal: Optional[CancelSignal] = None


#: provider 产出（经服务归一化与日志接受之前）。
@dataclass(frozen=True)
class SessionTitleProviderResult:
    title: str
    message_seqs: tuple[int, ...]
    model: Optional[SessionTitleModelProvenance] = None


#: 注册到服务的一个可选异步标题实现。
@dataclass
class SessionTitleProvider:
    id: SessionTitleProviderId
    automatic: SessionTitleAutomaticMode
    generate: Callable[[SessionTitleProviderRequest], Any]  # -> Awaitable[SessionTitleProviderResult]


def session_title_provider_id(id: str) -> SessionTitleProviderId:  # noqa: D401
    """品牌化一个原始 provider id。"""
    return SessionTitleProviderId(id)  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# 纯函数：文本归一化与标题折叠
# --------------------------------------------------------------------------- #
import re

# 操作系统命令转义（含未终止尾巴）
_OSC = re.compile(r"(?:\x1b\]|\x9d)(?:(?!\x07|\x1b\\)[\s\S])*(?:\x07|\x1b\\|$)", re.IGNORECASE)
# CSI 转义（如 SGR 颜色）
_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
# 剩余两字节 ESC 控制序列
_ESC = re.compile(r"\x1b[@-_]")
# 非空白 C0/C1 控制字符
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# 方向性与不可见控制（可让标题显示具欺骗性）
_DIR = re.compile(r"[\u200b\u200e\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff]")


def _assert_positive(name: str, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"session-title: {name} 必须是正整数")


def _clean_text(input: str) -> str:
    """去除控制字符并产出单行、空白归一化的文本。"""
    cleaned = _OSC.sub("", input)
    cleaned = _CSI.sub("", cleaned)
    cleaned = _ESC.sub("", cleaned)
    cleaned = _CTRL.sub("", cleaned)
    cleaned = _DIR.sub("", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def truncate_title_utf8(input: str, max_bytes: int) -> str:
    """按 UTF-8 字节预算截断，不切断 Unicode 码点。"""
    _assert_positive("maxBytes", max_bytes)
    data = input.encode("utf-8")
    if len(data) <= max_bytes:
        return input
    out = ""
    used = 0
    for ch in input:
        b = len(ch.encode("utf-8"))
        if used + b > max_bytes:
            break
        out += ch
        used += b
    return out


def normalize_session_title(input: str, max_bytes: int) -> str:
    """归一化一次接受标题并执行 UTF-8 字节预算。"""
    return truncate_title_utf8(_clean_text(input), max_bytes).rstrip()


def fallback_session_title(input: str, max_words: int, max_bytes: int) -> str:
    """推导确定性首条消息回退标题。"""
    _assert_positive("maxWords", max_words)
    words = [w for w in _clean_text(input).split(" ") if w]
    return truncate_title_utf8(" ".join(words[:max_words]), max_bytes).rstrip()


def collect_session_title_messages(
    events: list[SessionEvent], through_seq: Optional[int] = None
) -> list[SessionTitleUserMessage]:
    """收集日志顺序中的人类文本消息（来源为 user）。"""
    messages: list[SessionTitleUserMessage] = []
    for event in events:
        if through_seq is not None and event.seq > through_seq:
            break
        if event.type != "user/message":
            continue
        msg = event.data
        source = msg.source if isinstance(msg, object) and hasattr(msg, "source") else None
        if not isinstance(source, MessageSource) or source.kind != "user":
            continue
        blocks = msg.content if hasattr(msg, "content") else []
        text = "\n".join(b.text for b in blocks if isinstance(b, TextBlock))
        if normalize_session_title(text, 2 ** 31) == "":
            continue
        messages.append(SessionTitleUserMessage(seq=event.seq, text=text))
    return messages


def fold_session_title(events: list[SessionEvent]) -> Optional[SessionTitleSnapshot]:
    """不查可变元数据，折叠最新日志标题。"""
    event: Optional[SessionEvent] = None
    for e in reversed(events):
        if e.type == "session/title":
            event = e
            break
    if event is None:
        return None
    data = event.data
    return SessionTitleSnapshot(
        title=data.title,
        message_seqs=tuple(data.message_seqs),
        source=_copy_source(data.source),
        event_seq=event.seq,
        updated_at=event.time,
    )


def _copy_source(source: SessionTitleSource) -> SessionTitleSource:
    if source.kind == "fallback":
        return SessionTitleSource(kind="fallback")
    if source.kind == "provider":
        model = None if source.model is None else SessionTitleModelProvenance(
            provider=source.model.provider, model=source.model.model
        )
        return SessionTitleSource(kind="provider", provider=source.provider, model=model)
    return SessionTitleSource(kind="user")


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #
@dataclass
class _ProviderRegistration:
    provider: SessionTitleProvider
    active: set[asyncio.Task] = field(default_factory=set)
    closing: bool = False


@dataclass
class _PendingWork:
    registration: _ProviderRegistration
    revision: int
    through_seq: int


@dataclass
class _ActiveWork(_PendingWork):
    controller: CancelSignal = field(default_factory=CancelSignal)
    signal: CancelSignal = field(default_factory=CancelSignal)


@dataclass
class _WorkState:
    revision: int = 0
    fallback_task: Optional[asyncio.Task] = None
    pending: Optional[_PendingWork] = None
    active: Optional[_ActiveWork] = None


class SessionTitleService(Service):
    """``sessionTitle`` 服务：日志支撑的标题折叠 + 异步回退生成 + 可选 provider。"""

    inject = ["sessions"]

    def __init__(self, ctx: AppContext, config: Config) -> None:
        super().__init__(ctx, "sessionTitle")
        self.ctx = ctx
        _assert_positive("fallbackMaxWords", config.fallback_max_words)
        _assert_positive("fallbackMaxBytes", config.fallback_max_bytes)
        _assert_positive("maxTitleBytes", config.max_title_bytes)
        if config.fallback_max_bytes > config.max_title_bytes:
            raise ValueError("session-title: fallbackMaxBytes 不得超过 maxTitleBytes")
        self.config = config
        self.owner_fiber = ctx.fiber
        self._registration: Optional[_ProviderRegistration] = None
        self._work: dict[Session, _WorkState] = {}
        self._lifetime = CancelSignal()
        self._in_flight: set[asyncio.Task] = set()
        self._disposed = False

        ctx.effect(self._dispose, label="sessionTitle lifecycle")

        # 标题投影单元（仅当投影注册表被装配时；无头装配不受影响）
        if ctx.has_service("sessionProjections"):
            from dsh_py.services.projection import ProjectionDefinition

            ctx.sessionProjections.register(ProjectionDefinition(
                key="title",
                schema=None,  # 透传：apply 产出已是规范字符串或 None
                init=lambda: None,
                apply=lambda state, event: event.data.title if event.type == "session/title" else state,
                view=lambda state: state,
                state_version=1,
            ))

        ctx.on("session/event", self._on_session_event)
        ctx.on("session/disposed", self._on_session_disposed)

    # ------------------------------------------------------------------ #
    # 公开 API
    # ------------------------------------------------------------------ #
    def get(self, session: Session) -> Optional[SessionTitleSnapshot]:
        """读取一个 live 或重放会话的最新折叠标题。"""
        return fold_session_title(session.events)

    def rename(self, session: Session, title: str) -> SessionTitleSnapshot:
        """接受显式用户标题（钉住，停止自动生成）。"""
        self._assert_active()
        if self.ctx.sessions.get(session.header.id) is not session:
            raise ValueError(f'会话 "{session.header.id}" 不在本存储中')
        normalized = normalize_session_title(title, self.config.max_title_bytes)
        if normalized == "":
            raise SessionTitleInvalidError("会话标题必须包含可见字符")
        state = self._state_for(session)
        self._supersede(state, "user rename superseded automatic title generation")
        session.append("session/title", SessionTitleEventData(
            title=normalized, message_seqs=(), source=SessionTitleSource(kind="user")
        ))
        snap = self.get(session)
        if snap is None:  # pragma: no cover - 上面 append 已提交
            raise RuntimeError("重命名后的标题未能折叠")
        return snap

    async def refresh(
        self, session: Session, signal: Optional[CancelSignal] = None
    ) -> Optional[SessionTitleSnapshot]:
        """显式重试注册 provider；无 provider 时物化回退标题。"""
        if signal is not None:
            signal.throw_if_aborted()
        self._assert_active()
        if self.ctx.sessions.get(session.header.id) is not session:
            raise ValueError(f'会话 "{session.header.id}" 不在本存储中')
        registration = self._registration
        messages = collect_session_title_messages(session.events)
        latest = messages[-1] if messages else None
        if registration is None or registration.closing or latest is None:
            current = self.get(session)
            first = messages[0] if messages else None
            if current is not None and current.source.kind == "user" and first is not None:
                self._append_fallback(session, first)
                if signal is not None:
                    signal.throw_if_aborted()
                return self.get(session)
            fallback = await self._ensure_fallback(session)
            if signal is not None:
                signal.throw_if_aborted()
            return fallback
        state = self._state_for(session)
        revision = self._supersede(state, "explicit title refresh superseded older generation")
        work = self._activate(
            _PendingWork(registration=registration, revision=revision, through_seq=latest.seq),
            state, signal,
        )
        route = self._current_route(session)
        return await self._start_provider(session, work, route)

    def register(self, provider: SessionTitleProvider) -> Callable[[], None]:
        """注册唯一的标题 provider；返回精确注销函数（结算活跃的 provider 调用）。"""
        self._validate_provider(provider)
        if self._registration is not None:
            raise ValueError(f'session-title provider "{self._registration.provider.id}" 已注册')
        registration = _ProviderRegistration(provider=provider)
        self._registration = registration

        def dispose() -> None:
            registration.closing = True
            for state in list(self._work.values()):
                if state.pending is not None and state.pending.registration is registration:
                    state.pending = None
                if state.active is not None and state.active.registration is registration:
                    state.active.controller.abort(ValueError(f'session-title provider "{provider.id}" 已注销'))
            if self._registration is registration:
                self._registration = None

        return dispose

    # ------------------------------------------------------------------ #
    # 事件处理
    # ------------------------------------------------------------------ #
    def _on_session_event(self, session: Session, event: SessionEvent) -> None:
        if event.type == "user/message":
            self._on_user_message(session, event)

    def _on_session_disposed(self, session: Session) -> None:
        state = self._work.get(session)
        if state is None:
            return
        if state.active is not None:
            state.active.controller.abort(ValueError("会话在标题生成期间被释放"))
        self._work.pop(session, None)

    def _on_user_message(self, session: Session, event: SessionEvent) -> None:
        if not self._service_active():
            return
        msg = event.data
        source = getattr(msg, "source", None)
        if not isinstance(source, MessageSource) or source.kind != "user":
            return
        if collect_session_title_messages([event]) == []:
            return
        # 用户重命名钉住标题：不再自动修订
        current = self.get(session)
        if current is not None and current.source.kind == "user":
            return
        registration = self._registration
        if registration is not None and not registration.closing:
            messages = collect_session_title_messages(session.events, event.seq)
            should_schedule = registration.provider.automatic == "all-prompts" or (
                session.header.parent_session is None
                and len(messages) == 1
                and self.get(session) is None
            )
            if should_schedule:
                state = self._state_for(session)
                revision = self._supersede(
                    state, "newer user message superseded title generation"
                )
                state.pending = _PendingWork(
                    registration=registration, revision=revision, through_seq=event.seq
                )
        self._defer(self._on_user_message_async(session, event.seq))

    async def _on_user_message_async(self, session: Session, event_seq: int) -> None:
        # 让出一次事件循环：agent 主循环在 append 后会同步写入 request_header
        await asyncio.sleep(0)
        try:
            await self._ensure_fallback(session)
        except Exception as exc:  # noqa: BLE001
            if self._service_active():
                self.ctx.logger.warn(
                    f'session "{session.header.id}": 回退标题更新失败: {exc}'
                )
        state = self._work.get(session)
        pending = state.pending if state is not None else None
        if state is None or pending is None:
            return
        if self._registration is not pending.registration or pending.registration.closing:
            return
        if self._work.get(session) is not state or state.revision != pending.revision:
            return
        route = self._current_route(session)
        work = self._activate(pending, state)
        try:
            await self._start_provider(session, work, route)
        except Exception as exc:  # noqa: BLE001
            if work.signal.aborted or not self._service_active():
                return
            self.ctx.logger.warn(
                f'session "{session.header.id}": 自动标题生成失败: {exc}'
            )

    # ------------------------------------------------------------------ #
    # provider 执行
    # ------------------------------------------------------------------ #
    def _start_provider(
        self, session: Session, work: _ActiveWork, route: Optional[SessionTitleModelProvenance]
    ) -> "asyncio.Future":
        run = asyncio.ensure_future(self._run_provider(session, work, route))
        return self._track(run, work.registration)

    async def _run_provider(
        self, session: Session, work: _ActiveWork, route: Optional[SessionTitleModelProvenance]
    ) -> Optional[SessionTitleSnapshot]:
        try:
            self._assert_current(session, work)
            await self._ensure_fallback(session)
            self._assert_current(session, work)
            messages = tuple(collect_session_title_messages(session.events, work.through_seq))
            result = await work.registration.provider.generate(SessionTitleProviderRequest(
                session=session,
                messages=messages,
                **({} if route is None else {"route": route}),
                **({} if work.signal is None else {"signal": work.signal}),
            ))
            self._assert_current(session, work)
            accepted = self._validate_result(result, messages)
            model = None if accepted.model is None else accepted.model
            source = SessionTitleSource(kind="provider", provider=work.registration.provider.id)
            if model is not None:
                source = SessionTitleSource(kind="provider", provider=work.registration.provider.id, model=model)
            session.append("session/title", SessionTitleEventData(
                title=accepted.title, message_seqs=tuple(accepted.message_seqs), source=source
            ))
            return self.get(session)
        finally:
            state = self._work.get(session)
            if state is not None and state.active is work:
                state.active = None

    def _validate_result(
        self, result: Any, messages: tuple[SessionTitleUserMessage, ...]
    ) -> SessionTitleProviderResult:
        if not isinstance(result, SessionTitleProviderResult):
            raise ValueError("session-title provider 返回了无效的结果")
        title = normalize_session_title(result.title, self.config.max_title_bytes)
        if title == "":
            raise ValueError("session-title provider 返回了空标题")
        if not result.message_seqs:
            raise ValueError("session-title provider 必须指明至少一条来源消息 seq")
        order = {m.seq: i for i, m in enumerate(messages)}
        seqs: list[int] = []
        previous = -1
        for seq in result.message_seqs:
            idx = order.get(seq)
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0 or idx is None or idx <= previous:
                raise ValueError("session-title provider messageSeqs 必须是请求中唯一、有序的 seq")
            seqs.append(seq)
            previous = idx
        model = result.model
        if model is not None and (not model.provider or not model.model):
            raise ValueError("session-title provider result model 必须包含非空的 provider 与 model")
        return SessionTitleProviderResult(title=title, message_seqs=tuple(seqs), model=model)

    def _assert_current(self, session: Session, work: _ActiveWork) -> None:
        self._assert_active()
        if work.signal is not None:
            work.signal.throw_if_aborted()
        state = self._work.get(session)
        if (
            self._registration is not work.registration
            or state is None
            or state.active is not work
            or state.revision != work.revision
            or self.ctx.sessions.get(session.header.id) is not session
        ):
            raise ValueError("会话标题生成状态在未被取消的情况下发生了改变")

    # ------------------------------------------------------------------ #
    # 并发状态机
    # ------------------------------------------------------------------ #
    def _activate(
        self, pending: _PendingWork, state: _WorkState, upstream: Optional[CancelSignal] = None
    ) -> _ActiveWork:
        controller = CancelSignal()
        signals = [controller, self._lifetime]
        if upstream is not None:
            signals.append(upstream)
        work = _ActiveWork(
            registration=pending.registration,
            revision=pending.revision,
            through_seq=pending.through_seq,
            controller=controller,
            signal=CancelSignal.any(signals),
        )
        state.active = work
        return work

    def _supersede(self, state: _WorkState, reason: str) -> int:
        if state.active is not None:
            state.active.controller.abort(ValueError(reason))
        state.pending = None
        state.revision += 1
        return state.revision

    def _state_for(self, session: Session) -> _WorkState:
        state = self._work.get(session)
        if state is None:
            state = _WorkState()
            self._work[session] = state
        return state

    def _defer(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._track(task)

    def _track(self, run: "asyncio.Future", registration: Optional[_ProviderRegistration] = None) -> "asyncio.Future":
        self._in_flight.add(run)
        if registration is not None:
            registration.active.add(run)

        def done(_: Any) -> None:
            self._in_flight.discard(run)
            if registration is not None:
                registration.active.discard(run)

        run.add_done_callback(done)
        return run

    async def _drain(self, active: set) -> None:
        while active:
            await asyncio.gather(*list(active), return_exceptions=True)

    # ------------------------------------------------------------------ #
    # 回退标题
    # ------------------------------------------------------------------ #
    def _append_fallback(self, session: Session, first: SessionTitleUserMessage) -> None:
        title = fallback_session_title(
            first.text, self.config.fallback_max_words, self.config.fallback_max_bytes
        )
        if title == "":
            return
        session.append("session/title", SessionTitleEventData(
            title=title, message_seqs=(first.seq,), source=SessionTitleSource(kind="fallback")
        ))

    async def _ensure_fallback(self, session: Session) -> Optional[SessionTitleSnapshot]:
        self._assert_active()
        current = self.get(session)
        if current is not None:
            return current
        messages = collect_session_title_messages(session.events)
        first = messages[0] if messages else None
        if first is None:
            return None
        title = fallback_session_title(
            first.text, self.config.fallback_max_words, self.config.fallback_max_bytes
        )
        if title == "":
            return None
        state = self._state_for(session)
        if state.fallback_task is not None:
            # 已有进行中的回退生成，等待其结果
            try:
                return await state.fallback_task
            except Exception:  # noqa: BLE001
                pass
        task = asyncio.ensure_future(self._commit_fallback(session, first))
        state.fallback_task = task
        try:
            return await task
        finally:
            if state.fallback_task is task:
                state.fallback_task = None

    async def _commit_fallback(
        self, session: Session, first: SessionTitleUserMessage
    ) -> Optional[SessionTitleSnapshot]:
        self._assert_active()
        if self.ctx.sessions.get(session.header.id) is not session:
            raise ValueError(f'session "{session.header.id}" 不在本存储中')
        accepted = self.get(session)
        if accepted is not None:
            return accepted
        self._append_fallback(session, first)
        return self.get(session)

    # ------------------------------------------------------------------ #
    # 路由解析（dsh_py 无 request/header 事件，从 session.request_header 读取）
    # ------------------------------------------------------------------ #
    def _current_route(self, session: Session) -> Optional[SessionTitleModelProvenance]:
        header = session.request_header
        if not isinstance(header, dict):
            return None
        config = header.get("config")
        if not isinstance(config, dict):
            return None
        provider = config.get("provider")
        model = config.get("model")
        if isinstance(provider, str) and provider and isinstance(model, str) and model:
            return SessionTitleModelProvenance(provider=provider, model=model)
        return None

    # ------------------------------------------------------------------ #
    # 生命周期 / 校验
    # ------------------------------------------------------------------ #
    def _service_active(self) -> bool:
        return (
            not self._disposed
            and not self._lifetime.aborted
            and self.owner_fiber.state == FiberState.ACTIVE
        )

    def _assert_active(self) -> None:
        if not self._service_active():
            raise RuntimeError("session-title 服务已释放")

    def _validate_provider(self, provider: Any) -> None:
        if not isinstance(provider, SessionTitleProvider):
            raise ValueError("session-title provider 必须是一个对象")
        if not provider.id:
            raise ValueError("session-title provider id 必须是非空字符串")
        if provider.automatic not in ("first-prompt", "all-prompts"):
            raise ValueError("session-title provider automatic 模式非法")
        if not callable(provider.generate):
            raise ValueError(f'session-title provider "{provider.id}" 需要 generate()')

    def _dispose(self) -> None:
        self._disposed = True
        self._lifetime.abort(ValueError("session-title 服务已释放"))
        if self._registration is not None:
            self._registration.closing = True
            self._registration = None
        for state in self._work.values():
            if state.active is not None:
                state.active.controller.abort(ValueError("session-title 服务已释放"))
        # 注：进行中的任务在 _in_flight 中，由 await 侧感知取消后自然结算


# --------------------------------------------------------------------------- #
# 插件入口
# --------------------------------------------------------------------------- #
ConfigSchema = z.object({
    "fallbackMaxWords": z.integer(minimum=1),
    "fallbackMaxBytes": z.integer(minimum=1),
    "maxTitleBytes": z.integer(minimum=1),
}, extra="strip")


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``sessionTitle`` 服务。"""
    config = config or {}
    try:
        resolved = ConfigSchema.validate(config)
    except SchemaError as exc:
        raise ValueError(f"session-title 配置非法: {exc}")
    cfg = Config(
        fallback_max_words=int(resolved["fallbackMaxWords"]),
        fallback_max_bytes=int(resolved["fallbackMaxBytes"]),
        max_title_bytes=int(resolved["maxTitleBytes"]),
    )
    SessionTitleService(ctx, cfg)


apply.provides = ["sessionTitle"]
apply.inject = ["sessions"]

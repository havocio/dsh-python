"""定时提醒（schedule，治理类）：会话事件日志上的耐用一次性 / 固定间隔提醒。

把重放、时间校验、渲染（:mod:`dsh_py.services.schedule_domain`）与「实时计时器投影」
（本文件）组合：每个根 agent 一个 :class:`ScheduleRuntime`，根据耐用日志折叠出活跃
提醒，到点经 ``agent.run_maintenance`` 注入提醒消息并追加 ``schedule/change`` dispatch，
否则武装一个 bounded 定时器，每次唤醒重新核对墙钟（绝不早于日志记录的未来目标）。

提供三个 agent 级工具（``schedule_create`` / ``schedule_list`` / ``schedule_delete``），
全部经耐用日志持久化；交付模式恒为 ``session-local``——提醒仅在会话存活时准时，否则
直到会话恢复前处于 ``overdue``。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.services.message import MessageSource, TextBlock, create_user_message
from dsh_py.services.schedule_domain import (
    SCHEDULE_CHANGE_VERSION,
    MIN_EVERY_INTERVAL_SECONDS,
    AfterScheduleRecord,
    AtScheduleRecord,
    EveryScheduleRecord,
    EveryOccurrence,
    FoldedSchedules,
    ScheduleInputError,
    ScheduleLogError,
    allocate_schedule_id,
    create_after_schedule_record,
    create_at_schedule_record,
    create_every_schedule_record,
    decode_schedule_change,
    fold_schedule_events,
    render_every_reminder_batch_framing,
    render_reminder_framing,
    resolve_every_occurrence,
    schedule_view,
)
from dsh_py.services.session import Session

# 计时器能表示而不被钳制的最大延迟（Node/Python 同上限）
MAX_TIMER_DELAY_MS = 2_147_483_647


class SchedulePersistenceError(Exception):
    """无法证明当前实时前缀抵达了持久性监听器。"""
    def __init__(self, cause: Any = None) -> None:
        super().__init__("Schedule persistence did not complete.")
        self.name = "SchedulePersistenceError"
        self.__cause__ = cause


async def flush_schedule_persistence(ctx: AppContext, session: Session) -> None:
    """要求一次成功的共享持久性检查点（对齐 dsh 的 ``sessions.flush``）。

    ``sessions.flush`` 在本内核为同步方法（append 即同步落盘），返回 ``None`` 或
    ``True`` 视为成功；显式 ``False`` 视为持久性不确定。兼容未来可能返回协程的
    持久化后端。
    """
    try:
        result = ctx.sessions.flush(session)
        if asyncio.iscoroutine(result):
            result = await result
        if result is False:
            raise SchedulePersistenceError()
    except SchedulePersistenceError:
        raise
    except Exception as error:  # noqa: BLE001
        raise SchedulePersistenceError(error) from error


# --------------------------------------------------------------------------- #
# 事务串行（按 agent 序列化，对齐 dsh 的 tails WeakMap）
# --------------------------------------------------------------------------- #
_transaction_locks: dict[int, asyncio.Lock] = {}


async def run_schedule_transaction(agent, operation) -> Any:
    """在 agent 自身的事务锁后串行运行一个完整调度事务。"""
    key = id(agent)
    lock = _transaction_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _transaction_locks[key] = lock
    async with lock:
        try:
            return await operation()
        finally:
            if key in _transaction_locks and not _transaction_locks[key].locked():
                del _transaction_locks[key]


# --------------------------------------------------------------------------- #
# 决策
# --------------------------------------------------------------------------- #
def _due_decision(folded: FoldedSchedules, now_ms: int) -> dict:
    """选出一个到期一次性、一个完整固定间隔批次，或下一个唤醒目标。"""
    indexed = [{"record": r, "index": i} for i, r in enumerate(folded.active)]

    def by_target(left, right) -> int:
        return (int(__utc(left["record"].scheduled_at)) - int(__utc(right["record"].scheduled_at))
                or left["index"] - right["index"])

    one_shot = sorted(
        (e for e in indexed if e["record"].kind != "every"
         and __utc(e["record"].scheduled_at) <= now_ms),
        key=lambda e: (__utc(e["record"].scheduled_at), e["index"]),
    )
    if one_shot:
        return {"kind": "one-shot", "record": one_shot[0]["record"]}

    every = sorted(
        (e for e in indexed if e["record"].kind == "every"
         and __utc(e["record"].scheduled_at) <= now_ms),
        key=lambda e: (__utc(e["record"].scheduled_at), e["index"]),
    )
    if every:
        reminders = [
            {"record": e["record"], "occurrenceAt": resolve_every_occurrence(e["record"], now_ms).occurrence_at}
            for e in every
        ]
        return {"kind": "every", "reminders": reminders, "acceptedAt": __iso(now_ms)}

    targets = [__utc(r.scheduled_at) for r in folded.active if __utc(r.scheduled_at) > now_ms]
    target = min(targets) if targets else None
    return {"kind": "wait", "target": target}


def __utc(instant: str) -> int:
    return int(__fromiso(instant).timestamp() * 1000)


def __fromiso(instant: str):
    from datetime import datetime, timezone
    return datetime.fromisoformat(instant.replace("Z", "+00:00")).astimezone(timezone.utc)


def __iso(ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


# --------------------------------------------------------------------------- #
# 运行时（每精确根 agent 一个可弃实时计时器投影）
# --------------------------------------------------------------------------- #
class ScheduleRuntime:
    def __init__(self, ctx: AppContext, agent) -> None:
        self.ctx = ctx
        self.agent = agent
        self._timer: Optional[asyncio.Handle] = None
        self._run: Optional[asyncio.Task] = None
        self._requested = False
        self._stopping = False
        self._faulted = False

    def start(self) -> None:
        self.request_drive()

    def request_drive(self) -> None:
        if self._stopping or self._faulted:
            return
        self._clear_timer()
        self._requested = True
        if self._run is not None and not self._run.done():
            return
        self._run = asyncio.ensure_future(self._run_requested())

    async def dispose(self) -> None:
        self._stopping = True
        self._requested = False
        self._clear_timer()
        if self._run is not None:
            self._run.cancel()
            try:
                await self._run
            except (asyncio.CancelledError, Exception):
                pass

    def _clear_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _arm(self, target_ms: int, now_ms: int) -> None:
        delay = min(target_ms - now_ms, MAX_TIMER_DELAY_MS) / 1000.0
        self._timer = asyncio.get_event_loop().call_later(max(0.0, delay), lambda: self.request_drive())

    def _read_folded(self) -> Optional[FoldedSchedules]:
        try:
            return fold_schedule_events(self.agent.session.events, self.agent.session.header.seed_length)
        except Exception as error:  # noqa: BLE001
            self._faulted = True
            detail = error.message if isinstance(error, ScheduleLogError) else str(error)
            self.ctx.logger.warn(f"schedule: corrupt schedule log for agent {self.agent.id}: {detail}")
            return None

    async def _run_requested(self) -> None:
        while self._requested and not self._stopping and not self._faulted:
            self._requested = False
            await run_schedule_transaction(self.agent, self._drive_once)

    async def _drive_once(self) -> None:
        self._clear_timer()
        if self._stopping:
            return
        try:
            await flush_schedule_persistence(self.ctx, self.agent.session)
        except Exception as error:  # noqa: BLE001
            if not self._faulted:
                self.ctx.logger.warn(f"schedule: preflight failed for agent {self.agent.id}: {error}")
            return
        if self._stopping:
            return
        folded = self._read_folded()
        if folded is None:
            return
        now = _ms_now()
        decision = _safe_decide(folded, now)
        if decision is None:
            return
        if decision["kind"] == "wait":
            if decision.get("target") is not None:
                self._arm(decision["target"], now)
            return

        # 到期：经 run_maintenance 注入 + 持久化 dispatch（agent 忙碌时退回，等空闲再驱动）
        try:
            maintenance = self.agent.run_maintenance(lambda sig: self._dispatch(decision, sig))
        except RuntimeError:
            # 另一活动拥有空闲相位 → 等空闲（idle 监听会 request_drive）
            return
        dispatched = await maintenance
        if not dispatched:
            return
        try:
            await flush_schedule_persistence(self.ctx, self.agent.session)
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.warn(f"schedule: dispatch barrier failed for agent {self.agent.id}: {error}")
            return
        if not self._stopping:
            self.request_drive()

    async def _dispatch(self, decision: dict, maintenance_signal) -> bool:
        if self._stopping:
            return False
        claimed = self._read_folded()
        if claimed is None:
            return False
        now = _ms_now()
        redecision = _safe_decide(claimed, now)
        if redecision is None or redecision["kind"] == "wait":
            if redecision is not None and redecision.get("target") is not None:
                self._arm(redecision["target"], now)
            return False
        try:
            if redecision["kind"] == "one-shot":
                text = render_reminder_framing(redecision["record"])
            else:
                text = render_every_reminder_batch_framing(redecision["reminders"])
            message = create_user_message(
                [TextBlock(text)],
                source=MessageSource("plugin", plugin="schedule"),
            )
            self.agent.followup(message)
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.warn(f"schedule: framing or followup failed for agent {self.agent.id}: {error}")
            return False
        try:
            if redecision["kind"] == "one-shot":
                self.agent.session.append("schedule/change", {
                    "version": SCHEDULE_CHANGE_VERSION, "operation": "dispatch",
                    "id": redecision["record"].id,
                })
            else:
                for reminder in redecision["reminders"]:
                    self.agent.session.append("schedule/change", {
                        "version": SCHEDULE_CHANGE_VERSION, "operation": "dispatch",
                        "id": reminder["record"].id, "acceptedAt": redecision["acceptedAt"],
                    })
        except Exception as error:  # noqa: BLE001
            self._faulted = True
            self._clear_timer()
            self.ctx.logger.warn(f"schedule: dispatch append failed for agent {self.agent.id}: {error}")
            return False
        return True


def _safe_decide(folded: FoldedSchedules, now: int) -> Optional[dict]:
    try:
        return _due_decision(folded, now)
    except Exception as error:  # noqa: BLE001
        return None


def _ms_now() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# 工具（agent 级 schedule_create / schedule_list / schedule_delete）
# --------------------------------------------------------------------------- #
def _internal_error() -> dict:
    return {"code": "internal_error", "message": "The schedule operation failed."}


def _corrupt_log_error() -> dict:
    return {"code": "corrupt_schedule_log", "message": "The session schedule log is corrupt."}


def _persistence_error(operation: str, sid: Optional[str] = None) -> dict:
    return {
        "code": "persistence_uncertain",
        "message": "Schedule persistence is uncertain; retry with schedule_list before relying on this result.",
        "operation": operation,
        **({"id": sid} if sid is not None else {}),
    }


def _input_error(error: ScheduleInputError) -> dict:
    return {"code": error.code, "message": error.message}


def _fold_for_tool(agent) -> Any:
    try:
        return fold_schedule_events(agent.session.events, agent.session.header.seed_length)
    except ScheduleLogError:
        return _corrupt_log_error()
    except Exception:
        return _internal_error()


def _is_tool_error(value: Any) -> bool:
    return isinstance(value, dict) and "code" in value


async def _preflight(ctx: AppContext, agent, operation: str, sid: Optional[str] = None) -> Optional[dict]:
    try:
        await flush_schedule_persistence(ctx, agent.session)
        return None
    except Exception:
        return _persistence_error(operation, sid)


def _validate_create_args(args: dict) -> Optional[dict]:
    keys = list(args.keys())
    selector_count = (1 if args.get("after_seconds") is not None else 0) \
        + (1 if args.get("at") is not None else 0) \
        + (1 if args.get("every_seconds") is not None else 0)
    if any(k not in ("prompt", "after_seconds", "at", "every_seconds") for k in keys) or selector_count != 1:
        return {"code": "invalid_selector",
                "message": "schedule_create 恰好接受 after_seconds / at / every_seconds 之一。"}
    if (args.get("prompt") or "").strip() == "":
        return {"code": "invalid_prompt", "message": "prompt 在 trim 后不得为空。"}
    if args.get("after_seconds") is not None and (not isinstance(args["after_seconds"], int)
                                                  or isinstance(args["after_seconds"], bool) or args["after_seconds"] <= 0):
        return {"code": "invalid_rule", "message": "after_seconds 必须是正的安全整数。"}
    if args.get("every_seconds") is not None and (not isinstance(args["every_seconds"], int)
                                                  or isinstance(args["every_seconds"], bool)):
        return {"code": "invalid_rule", "message": "every_seconds 必须是安全整数。"}
    if args.get("every_seconds") is not None and args["every_seconds"] < MIN_EVERY_INTERVAL_SECONDS:
        return {"code": "frequency_too_high", "message": f"every_seconds 必须 >= {MIN_EVERY_INTERVAL_SECONDS}。"}
    return None


def register_schedule_tools(ctx: AppContext) -> None:
    """注册三个全局 schedule 工具（handler 经 exec.agent 操作所属会话）。"""

    async def schedule_create(arguments: dict, exec: dict) -> tuple:
        agent = exec.get("agent")
        if agent is None:
            return json.dumps(_internal_error()), False
        invalid = _validate_create_args(arguments)
        if invalid is not None:
            return json.dumps(invalid), False
        signal = exec.get("signal")
        if signal is not None and getattr(signal, "aborted", False):
            return json.dumps(_internal_error()), False

        async def op():
            uncertain = await _preflight(ctx, agent, "create")
            if uncertain is not None:
                return uncertain
            folded = _fold_for_tool(agent)
            if _is_tool_error(folded):
                return folded
            sid = allocate_schedule_id(folded)
            now = _ms_now()
            try:
                if arguments.get("at") is not None:
                    record = create_at_schedule_record(sid, arguments["prompt"], arguments["at"], now)
                elif arguments.get("after_seconds") is not None:
                    record = create_after_schedule_record(sid, arguments["prompt"], arguments["after_seconds"], now)
                else:
                    record = create_every_schedule_record(sid, arguments["prompt"], arguments["every_seconds"], now)
            except ScheduleInputError as error:
                return _input_error(error)
            except Exception:
                return _internal_error()
            agent.session.append("schedule/change", {
                "version": SCHEDULE_CHANGE_VERSION, "operation": "create", "schedule": _record_as_dict(record),
            })
            barrier = await _preflight(ctx, agent, "create", sid)
            if barrier is not None:
                return barrier
            return schedule_view(record, _ms_now())

        result = await run_schedule_transaction(agent, op)
        if signal is not None and getattr(signal, "aborted", False):
            return json.dumps(_internal_error()), False
        return json.dumps(result), False

    async def schedule_list(arguments: dict, exec: dict) -> tuple:
        agent = exec.get("agent")
        if agent is None:
            return json.dumps(_internal_error()), False
        signal = exec.get("signal")
        if signal is not None and getattr(signal, "aborted", False):
            return json.dumps(_internal_error()), False

        async def op():
            uncertain = await _preflight(ctx, agent, "list")
            if uncertain is not None:
                return uncertain
            folded = _fold_for_tool(agent)
            if _is_tool_error(folded):
                return folded
            now = _ms_now()
            return [schedule_view(r, now) for r in folded.active]

        result = await run_schedule_transaction(agent, op)
        return json.dumps(result), False

    async def schedule_delete(arguments: dict, exec: dict) -> tuple:
        agent = exec.get("agent")
        if agent is None:
            return json.dumps(_internal_error()), False
        sid_raw = arguments.get("id", "")
        if not isinstance(sid_raw, str) or sid_raw.strip() != sid_raw or len(sid_raw) == 0:
            return json.dumps({"code": "invalid_rule",
                               "message": "schedule_delete 的 id 必须是无首尾空白的非空字符串。"}), False
        signal = exec.get("signal")
        if signal is not None and getattr(signal, "aborted", False):
            return json.dumps(_internal_error()), False

        async def op():
            uncertain = await _preflight(ctx, agent, "delete", sid_raw)
            if uncertain is not None:
                return uncertain
            folded = _fold_for_tool(agent)
            if _is_tool_error(folded):
                return folded
            sid = ScheduleId_local(sid_raw)
            if not any(r.id == sid for r in folded.active):
                return {"id": sid, "deleted": False, "code": "schedule_not_found"}
            agent.session.append("schedule/change", {
                "version": SCHEDULE_CHANGE_VERSION, "operation": "delete", "id": sid,
            })
            barrier = await _preflight(ctx, agent, "delete", sid)
            if barrier is not None:
                return barrier
            return {"id": sid, "deleted": True}

        result = await run_schedule_transaction(agent, op)
        return json.dumps(result), False

    ctx.tools.register(
        "schedule_create",
        "在当前会话创建一个提醒：给定非空 prompt 与恰好一个选择器（after_seconds 正延迟 / at 绝对时刻 / every_seconds 固定间隔≥"
        f"{MIN_EVERY_INTERVAL_SECONDS}）。交付为 session-local：会话存活时准时，否则直到恢复前为 overdue。",
        {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "required": True},
                "after_seconds": {"type": "integer"},
                "every_seconds": {"type": "integer"},
                "at": {"type": "string",
                       "description": "严格偏移 RFC 3339 或本地 date/time + 显式 IANA 时区"},
            },
            "required": ["prompt"],
        },
        schedule_create,
    )
    ctx.tools.register(
        "schedule_list",
        "列出当前会话全部活跃提醒（按创建顺序，含 id / UTC 目标 / scheduled|overdue 状态 / session-local 交付模式）。",
        {"type": "object", "properties": {}},
        schedule_list,
    )
    ctx.tools.register(
        "schedule_delete",
        "按创建返回的精确 id 删除一个活跃提醒；未知或已完成的 id 返回 deleted=false。",
        {
            "type": "object",
            "properties": {"id": {"type": "string", "required": True,
                                 "description": "精确的会话内 schedule id"}},
            "required": ["id"],
        },
        schedule_delete,
    )


def ScheduleId_local(value: str) -> str:
    return value


def _record_as_dict(record) -> dict:
    base = {"id": record.id, "kind": record.kind, "prompt": record.prompt, "scheduledAt": record.scheduled_at}
    if isinstance(record, AfterScheduleRecord):
        base["afterSeconds"] = record.after_seconds
    elif isinstance(record, EveryScheduleRecord):
        base["everySeconds"] = record.every_seconds
    return base


# --------------------------------------------------------------------------- #
# 插件入口
# --------------------------------------------------------------------------- #
def apply(ctx: AppContext, config: dict | None = None) -> None:
    """仅为加载后发布的根 agent 安装 schedule（运行时 + 工具）。"""
    runtimes: dict = {}
    stopping = False

    def on_created(event):
        payload = event if isinstance(event, dict) else {}
        agent = payload.get("agent")
        if agent is None or stopping or agent in runtimes:
            return
        try:
            roots = ctx.agents.roots()
        except Exception:
            roots = []
        if agent not in roots:
            return
        runtime = ScheduleRuntime(ctx, agent)

        def install_for_agent():
            register_schedule_tools(ctx)
            runtime.start()
            # 空闲且会话存在 schedule/change 时再驱动（捕捉到期）
            def on_status(status_event):
                sp = status_event if isinstance(status_event, dict) else {}
                if sp.get("status") != "idle":
                    return
                a = sp.get("agent")
                if a is not runtime.agent:
                    return
                if any(e.type == "schedule/change" for e in a.session.events):
                    runtime.request_drive()
            ctx.on("agent/status", on_status)
            runtimes[agent] = runtime

        install_for_agent()

    ctx.on("agent/session-start", on_created)

    def cleanup():
        nonlocal stopping
        stopping = True
        for rt in list(runtimes.values()):
            try:
                asyncio.ensure_future(rt.dispose())
            except Exception:  # noqa: BLE001
                pass

    ctx.effect(cleanup)


apply.name = "schedule"
apply.inject = ["agents", "sessions", "tools"]

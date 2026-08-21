"""
goal 服务层（``ctx.goals``）：同会话持久目标域，唯一依赖所属会话日志。

对齐 dsh ``packages/goal/goal/src/index.ts``：事件溯源状态（``goal/change``
全值快照事件）、compare-and-set 变更（ref 校验）、进程本地续行激活
（armed/disarmed，永不持久化）、可选 ``goal`` 投影单元与 ``goal/changed``
通知。

**与 dsh 的差异（已注明）**：
- ``agentEvents(ctx, agent).emit``（agent 作用域事件）→ dsh_py 用 ``ctx.emit``
  全局发射，载荷携带 ``agent``（dsh_py 无 scope 过滤层）；
- typert：dsh 的 ``@Remote`` 装饰器与 ``TypertRemoteService`` 基类 → dsh_py 的
  ``@remote`` 装饰器 + ``Service`` 基类（typertRegistry.register 时扫描）；
- 投影单元经 ``hasattr(ctx, "sessionProjections")`` 守卫（dsh 用运行时 lazy
  ``ctx.inject``，dsh_py 无此机制）；
- 配置读取一律 ``cfg.get(k) or default``（loader 的 schema 校验会把未提供的
  可选键填成显式 ``None``）。
"""

from __future__ import annotations

import weakref
from typing import Any, Callable, Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.goal_fold import (
    GOAL_CHANGE_VERSION,
    GoalError,
    GoalId,
    apply_goal_event,
    apply_goal_projection,
    empty_goal_fold_state,
    goal_change_ref,
    new_goal_id,
)
from dsh_py.services.typert import remote

DEFAULT_MAX_GOAL_ROUNDS = 256

Config = z.object({
    "defaultMaxGoalRounds": z.integer().default(DEFAULT_MAX_GOAL_ROUNDS),
})


def resolve_max_goal_rounds(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise GoalError("maxGoalRounds must be a positive safe integer", "GOAL_INVALID_MAX_ROUNDS")
    return value


def resolve_objective(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GoalError("goal objective must be a non-empty string", "GOAL_INVALID_OBJECTIVE")
    return value.strip()


def resolve_block_reason(value: Any) -> dict:
    import re as _re
    if not isinstance(value, dict):
        raise GoalError(
            "goal block reason requires a lower-kebab-case code and a non-empty message",
            "GOAL_INVALID_BLOCK_REASON",
        )
    code = value.get("code")
    message = value.get("message")
    if not isinstance(code, str) or not _re.match(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", code) \
            or not isinstance(message, str) or not message.strip():
        raise GoalError(
            "goal block reason requires a lower-kebab-case code and a non-empty message",
            "GOAL_INVALID_BLOCK_REASON",
        )
    return {"code": code, "message": message.strip()}


class GoalService(Service):
    """goal 域服务：读/写都基于所属会话日志；进程本地缓存增量观察。"""

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx, "goals")
        cfg = config or {}
        self._default_max_goal_rounds = resolve_max_goal_rounds(
            int(cfg.get("defaultMaxGoalRounds") or DEFAULT_MAX_GOAL_ROUNDS),
        )
        self._caches: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

        @ctx.on("agent/session-start")
        def on_session_start(event) -> None:
            payload = event if isinstance(event, dict) else {}
            agent = payload.get("agent")
            if agent is None:
                return
            self._cache(agent.session)["activation"] = "disarmed"

        # 可选 ``goal`` 投影单元（缝装配时才注册；全值 last-wins）
        if hasattr(ctx, "sessionProjections"):
            from dsh_py.services.projection import ProjectionDefinition
            ctx.sessionProjections.register(ProjectionDefinition(
                key="goal",
                schema=None,  # 透传：apply 产出已是规范载荷
                init=lambda: None,
                apply=apply_goal_projection,
                view=lambda state: state,
                state_version=4,
            ))

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def get(self, agent) -> Optional[dict]:
        """读取一个精确 live agent 的当前目标；无目标返回 ``None``。"""
        self.assert_live(agent)
        cache = self._cache(agent.session)
        self._sync(agent.session, cache)
        return self._view(cache)

    def disarm(self, agent) -> Optional[dict]:
        """移除进程本地续行权限（不改持久 phase/revision）。"""
        self.assert_live(agent)
        cache = self._cache(agent.session)
        self._sync(agent.session, cache)
        cache["activation"] = "disarmed"
        return self._view(cache)

    # ------------------------------------------------------------------ #
    # 变更
    # ------------------------------------------------------------------ #
    def create(self, agent, request: dict) -> dict:
        """创建并武装目标；已完成目标可替换，其余 phase 须先 clear/resume。"""
        objective = resolve_objective(request.get("objective"))
        max_rounds = resolve_max_goal_rounds(
            request.get("maxGoalRounds", self._default_max_goal_rounds),
        )
        cache = self._prepare_mutation(agent)
        current = cache["state"]["goal"]
        if current is not None and current["phase"] != "complete":
            raise GoalError(
                f'goal "{current["id"]}" already exists with phase "{current["phase"]}"',
                "GOAL_ALREADY_EXISTS",
            )
        now = _now_ms()
        goal = {
            "id": new_goal_id(),
            "revision": 1,
            "objective": objective,
            "phase": "active",
            "maxGoalRounds": max_rounds,
        }
        return self._commit_snapshot(agent, cache, "create", goal, 0, now, now, "armed")

    def edit(self, agent, ref: dict, request: dict) -> dict:
        """不换 phase 编辑目标与/或回合上限。"""
        cache = self._prepare_mutation(agent)
        current = self._expect_current(cache, ref)
        if request.get("objective") is None and request.get("maxGoalRounds") is None:
            raise GoalError("goal edit requires objective and/or maxGoalRounds", "GOAL_INVALID_EDIT")
        goal = dict(current)
        goal["revision"] = current["revision"] + 1
        if request.get("objective") is not None:
            goal["objective"] = resolve_objective(request["objective"])
        if request.get("maxGoalRounds") is not None:
            goal["maxGoalRounds"] = resolve_max_goal_rounds(request["maxGoalRounds"])
        return self._commit_current(agent, cache, "edit", goal, cache["activation"])

    def pause(self, agent, ref: dict) -> dict:
        return self._transition(agent, ref, "pause", ["active"], "paused", "disarmed")

    def resume(self, agent, ref: dict) -> dict:
        """恢复并武装已停目标，或会话开始边缘后重新武装 active 目标（预算有余）。"""
        cache = self._prepare_mutation(agent)
        current = self._expect_current(cache, ref)
        resumable = ("active", "paused", "blocked")
        if current["phase"] not in resumable:
            raise self._transition_error(current, "resume", resumable)
        if current["phase"] == "active" and cache["activation"] == "armed":
            raise GoalError(
                f'goal "{current["id"]}" is already active and armed',
                "GOAL_INVALID_TRANSITION",
            )
        if cache["state"]["roundsStarted"] >= current["maxGoalRounds"]:
            raise GoalError(
                f'goal "{current["id"]}" exhausted {current["maxGoalRounds"]} goal rounds; '
                "increase maxGoalRounds before resuming",
                "GOAL_INVALID_TRANSITION",
            )
        return self._commit_current(agent, cache, "resume", self._with_phase(current, "active"), "armed")

    def complete(self, agent, ref: dict) -> dict:
        return self._transition(agent, ref, "complete", ["active", "paused", "blocked"], "complete", "disarmed")

    def block(self, agent, ref: dict, reason: Any) -> dict:
        """把 active 目标标为 blocked 并卸武。"""
        cache = self._prepare_mutation(agent)
        current = self._expect_current(cache, ref)
        if current["phase"] != "active":
            raise self._transition_error(current, "block", ["active"])
        goal = self._with_phase(current, "blocked")
        goal["blockedReason"] = resolve_block_reason(reason)
        return self._commit_current(agent, cache, "block", goal, "disarmed")

    def clear(self, agent, ref: dict) -> dict:
        """清空当前目标，保留耐用墓碑与历史。"""
        cache = self._prepare_mutation(agent)
        current = self._expect_current(cache, ref)
        tombstone = {"id": current["id"], "revision": current["revision"] + 1}
        change = {
            "kind": "goal/change",
            "version": GOAL_CHANGE_VERSION,
            "operation": "clear",
            "cleared": tombstone,
            "clearedAt": self._next_mutation_time(cache),
        }
        self._commit(agent, cache, change, "disarmed")
        return dict(tombstone)

    # typert 远程导出（dsh 的 remoteExportCreate：返回 wire 安全 ack）
    @remote("create")
    def remote_export_create(self, agent, request: dict) -> dict:
        view = self.create(agent, request)
        return {"ref": {"id": view["id"], "revision": view["revision"]}}

    @remote("edit")
    def remote_export_edit(self, agent, ref: dict, request: dict) -> dict:
        return self._remote_view(self.edit(agent, ref, request))

    @remote("pause")
    def remote_export_pause(self, agent, ref: dict) -> dict:
        return self._remote_view(self.pause(agent, ref))

    @remote("resume")
    def remote_export_resume(self, agent, ref: dict) -> dict:
        return self._remote_view(self.resume(agent, ref))

    @remote("complete")
    def remote_export_complete(self, agent, ref: dict) -> dict:
        return self._remote_view(self.complete(agent, ref))

    @remote("clear")
    def remote_export_clear(self, agent, ref: dict) -> dict:
        return self.clear(agent, ref)

    def _remote_view(self, view: dict) -> dict:
        return view

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _prepare_mutation(self, agent) -> dict:
        self.assert_live(agent)
        cache = self._cache(agent.session)
        self._sync(agent.session, cache)
        return cache

    def _expect_current(self, cache: dict, ref: dict) -> dict:
        current = cache["state"]["goal"]
        if current is None:
            raise GoalError("no current goal", "GOAL_NOT_FOUND")
        if ref.get("id") != current["id"] or ref.get("revision") != current["revision"]:
            raise GoalError(
                f'stale goal ref "{ref.get("id")}" revision {ref.get("revision")}; '
                f'current is "{current["id"]}" revision {current["revision"]}',
                "GOAL_STALE_REVISION",
            )
        return current

    def assert_live(self, agent) -> None:
        loop = getattr(self.ctx, "agentLoop", None)
        if loop is None or loop.get(agent.id) is not agent:
            raise GoalError(f'agent "{agent.id}" is not live in this registry', "GOAL_AGENT_NOT_LIVE")

    def _cache(self, session) -> dict:
        cache = self._caches.get(session)
        if cache is not None:
            return cache
        state = empty_goal_fold_state()
        for event in session.events:
            apply_goal_event(state, event)
        cache = {
            "state": state,
            "activation": "disarmed",
            "observedSeq": len(session.events),
            "pendingActivation": None,
        }
        self._caches[session] = cache
        return cache

    def _sync(self, session, cache: dict) -> None:
        events = session.events
        observed = cache["observedSeq"]
        for event in events[observed:]:
            apply_goal_event(cache["state"], event)
            if event.type == "goal/change":
                pending = cache["pendingActivation"]
                cache["activation"] = (
                    pending["activation"] if pending is not None and pending["seq"] == event.seq
                    else "disarmed"
                )
            cache["observedSeq"] += 1

    def _with_phase(self, current: dict, phase: str) -> dict:
        goal = dict(current)
        goal["revision"] = current["revision"] + 1
        goal["phase"] = phase
        goal.pop("blockedReason", None)
        return goal

    def _transition(
        self,
        agent,
        ref: dict,
        operation: str,
        allowed: list,
        phase: str,
        activation: str,
    ) -> dict:
        cache = self._prepare_mutation(agent)
        current = self._expect_current(cache, ref)
        if current["phase"] not in allowed:
            raise self._transition_error(current, operation, allowed)
        return self._commit_current(agent, cache, operation, self._with_phase(current, phase), activation)

    def _transition_error(self, current: dict, operation: str, allowed: list) -> GoalError:
        return GoalError(
            f'cannot {operation} goal "{current["id"]}" from phase "{current["phase"]}"; '
            f"expected {' or '.join(allowed)}",
            "GOAL_INVALID_TRANSITION",
        )

    def _commit_current(self, agent, cache: dict, operation: str, goal: dict, activation: str) -> dict:
        created_at = cache["state"]["createdAt"]
        if created_at is None:
            raise GoalError("current goal cache lacks createdAt", "GOAL_INVALID_CHANGE")
        return self._commit_snapshot(
            agent,
            cache,
            operation,
            goal,
            cache["state"]["roundsStarted"],
            created_at,
            self._next_mutation_time(cache),
            activation,
        )

    def _next_mutation_time(self, cache: dict) -> int:
        updated_at = cache["state"]["updatedAt"]
        if updated_at is None:
            raise GoalError("current goal cache lacks updatedAt", "GOAL_INVALID_CHANGE")
        return max(_now_ms(), updated_at)

    def _commit_snapshot(
        self,
        agent,
        cache: dict,
        operation: str,
        goal: dict,
        rounds_started: int,
        created_at: int,
        updated_at: int,
        activation: str,
    ) -> dict:
        change = {
            "kind": "goal/change",
            "version": GOAL_CHANGE_VERSION,
            "operation": operation,
            "goal": goal,
            "roundsStarted": rounds_started,
            "createdAt": created_at,
            "updatedAt": updated_at,
        }
        self._commit(agent, cache, change, activation)
        view = self._view(cache)
        if view is None:
            raise GoalError("snapshot commit cleared the goal unexpectedly", "GOAL_INVALID_CHANGE")
        return view

    def _commit(self, agent, cache: dict, change: dict, activation: str) -> None:
        ref = goal_change_ref(change)
        # dsh_py 的 session.seq 是「最后事件 seq」（append 前）；新事件 seq = seq + 1
        cache["pendingActivation"] = {"seq": agent.session.seq + 1, "activation": activation}
        try:
            agent.session.append("goal/change", change)
            self._sync(agent.session, cache)
        finally:
            cache["pendingActivation"] = None
        goal = self._view(cache)
        notification = {"operation": change["operation"], "ref": dict(ref)}
        if goal is not None:
            notification["goal"] = goal
        self.ctx.emit("goal/changed", {"agent": agent, "change": notification})

    def _view(self, cache: dict) -> Optional[dict]:
        goal = cache["state"]["goal"]
        created_at = cache["state"]["createdAt"]
        updated_at = cache["state"]["updatedAt"]
        if goal is None:
            return None
        if created_at is None or updated_at is None:
            raise GoalError(f'goal "{goal["id"]}" cache lacks timestamps', "GOAL_INVALID_CHANGE")
        view = dict(goal)
        view["roundsStarted"] = cache["state"]["roundsStarted"]
        view["createdAt"] = created_at
        view["updatedAt"] = updated_at
        view["activation"] = cache["activation"]
        return view


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：注册 ``ctx.goals``（+ 可选投影与 typert 远程作用域）。"""
    service = GoalService(ctx, config)
    if hasattr(ctx, "typertRegistry"):
        dispose = ctx.typertRegistry.register("goals", service)
        ctx.effect(dispose, label="goal.remote")


apply.Config = Config
apply.name = "goal"
apply.inject = ["agents"]
apply.provides = ["goals"]


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)

"""
面向模型的目标工具（对齐 dsh ``packages/goal/tool-goal``）。

注册 ``get_goal`` / ``create_goal`` / ``update_goal`` 三个工具 + systemPrompt
策略片段（order 114）。变更类工具带执行期权威检查：create/edit/pause/resume
须直接人类回合；complete/blocked 允许直接人类回合或当前目标的确切目标轮。

**与 dsh 的差异（已注明）**：
- 权威检查降级：dsh_py 的 agent 无 ``status``/``currentInitiator()``，
  ``goalToolExecution`` 只校验 live 身份 + 开放回合窗口；
- ``exec.deferContext``（自动回合 wrapup 注入）不存在——complete/blocked 的
  wrapup 上下文注入省略，工具输出保持纯 JSON；
- 输出无结构化 ``output.schema``/``presentCall``（dsh_py 工具返回文本）。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.services.goal_fold import GoalError
from dsh_py.services.system_prompt import PromptSection

PLUGIN_NAME = "tool-goal"

UPDATE_ACTIONS = ("edit", "pause", "resume", "complete", "blocked")

Config = z.object({
    "blockedAfterConsecutiveRounds": z.integer().default(3),
})

_GET_DESCRIPTION = (
    "Read the current same-session goal, including its exact id/revision, objective, phase, "
    "completed continuation rounds, round limit, blocker reason when present, and whether "
    "another continuation is armed. Call this before updating a goal."
)
_CREATE_DESCRIPTION = (
    "Create one persisted same-session completion goal when the current direct human request "
    "is a long-running objective that should continue across autonomous goal rounds. You may "
    "infer that intent without requiring the user to say \"create a goal\". Do not use this "
    "for trivial single-turn work. Execution rejects non-human and subagent authority."
)
_UPDATE_DESCRIPTION = (
    "Update the exact current goal revision. edit, pause, and resume require a direct "
    "top-level human request. During an automatic continuation of the current goal, complete "
    "and blocked are also allowed. blocked is rejected before the configured minimum round "
    "count; the model remains responsible for judging that the same condition persisted "
    "across those rounds and must explain it in blocked_reason."
)


def _guidance(blocked_after: int) -> str:
    return (
        "Use goal tools for one long-running completion objective in the current session. "
        "create_goal may infer goal intent from a direct human request in any language; do not "
        "create a goal for routine single-turn work. Call get_goal before update_goal and copy "
        "its exact goal_id and revision. After session resume or fork, an active goal is "
        "disarmed: when a human asks to continue or resume in any wording or language, use "
        "update_goal action resume to rearm it. Mark complete only when the objective is "
        f"actually achieved. Mark blocked only after the same blocking condition persists for "
        f"at least {blocked_after} consecutive goal rounds, and report that concrete condition "
        "in blocked_reason; difficulty, uncertainty, or useful remaining work is not blocked."
    )


def _resolve_config(config: Optional[dict]) -> dict:
    cfg = config or {}
    blocked_after = int(cfg.get("blockedAfterConsecutiveRounds") or 3)
    if blocked_after < 1:
        raise TypeError("blockedAfterConsecutiveRounds must be a positive safe integer")
    return {"blockedAfterConsecutiveRounds": blocked_after}


# --------------------------------------------------------------------------- #
# 权威检查（对齐 authority.ts，降级版）
# --------------------------------------------------------------------------- #
def _goal_tool_execution(ctx: AppContext, exec: dict) -> dict:
    """解析并认证调用 agent 与其驱动边界（live 身份 + 开放回合窗口）。"""
    agent = exec.get("agent")
    if agent is None:
        raise GoalError("goal tools require a calling agent", "GOAL_TOOL_AGENT_REQUIRED")
    loop = getattr(ctx, "agentLoop", None)
    if loop is None or loop.get(agent.id) is not agent:
        raise GoalError(
            "goal tools require the exact live calling agent inside its active driver",
            "GOAL_TOOL_DRIVER_REQUIRED",
        )
    events = agent.session.events
    start_index = None
    for index in range(len(events) - 1, -1, -1):
        boundary = events[index]
        if boundary.type == "turn/end":
            raise GoalError("goal tools require an open model turn", "GOAL_TOOL_DRIVER_REQUIRED")
        if boundary.type == "turn/start":
            start_index = index
            break
    if start_index is None:
        raise GoalError("goal tools require an open model turn", "GOAL_TOOL_DRIVER_REQUIRED")
    return {"agent": agent, "start": events[start_index], "events": events[start_index + 1:]}


def _has_direct_human_input(ctx: AppContext, execution: dict) -> bool:
    """当前根 agent 回合中是否有宿主证言的人类输入。"""
    loop = getattr(ctx, "agentLoop", None)
    if loop is None or execution["agent"] not in loop.roots():
        return False
    return any(
        e.type == "user/message" and getattr(getattr(e.data, "source", None), "kind", None) == "user"
        for e in execution["events"]
    )


def _is_matching_goal_round(execution: dict, goal: dict) -> bool:
    """本回合是否是当前目标的精确准入回合。"""
    return any(
        e.type == "user/message" and getattr(getattr(e.data, "source", None), "kind", None) == "goal"
        and e.data.source.goalId == goal["id"]
        and e.data.source.revision == goal["revision"]
        and e.data.source.round == goal["roundsStarted"]
        for e in execution["events"]
    )


def _require_direct_human(ctx: AppContext, execution: dict) -> None:
    if not _has_direct_human_input(ctx, execution):
        raise GoalError(
            "this goal operation requires a direct human turn on a top-level agent",
            "GOAL_TOOL_AUTHORITY_REQUIRED",
        )


def _completion_authority(ctx: AppContext, execution: dict) -> dict:
    """complete/blocked 的授权：直接人类回合或确切目标轮。"""
    if _has_direct_human_input(ctx, execution):
        return {"kind": "direct-human"}
    goal = ctx.goals.get(execution["agent"])
    if goal is not None and _is_matching_goal_round(execution, goal):
        return {"kind": "goal-round", "goal": goal}
    raise GoalError(
        "complete and blocked require a direct human turn or the current goal round",
        "GOAL_TOOL_AUTHORITY_REQUIRED",
    )


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _has_text(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _has_round_cap(value: Any) -> bool:
    return isinstance(value, (int, float)) and value != 0 and not isinstance(value, bool)


def _goal_ref(goal_id: Any, revision: Any) -> dict:
    if not isinstance(goal_id, str) or not goal_id or goal_id != goal_id.strip() \
            or not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise GoalError(
            "goal_id must be non-empty and revision must be a positive safe integer",
            "GOAL_TOOL_INVALID_UPDATE",
        )
    return {"id": goal_id, "revision": revision}


def _goal_value(goal: Optional[dict]) -> dict:
    """稳定紧凑模型结果；activation 是观察值而非重放状态。"""
    if goal is None:
        return {"goal": None}
    value = {
        "goal": {
            "id": goal["id"],
            "revision": goal["revision"],
            "objective": goal["objective"],
            "phase": goal["phase"],
            "roundsStarted": goal["roundsStarted"],
            "maxGoalRounds": goal["maxGoalRounds"],
        },
        "activation": goal["activation"],
    }
    if goal.get("blockedReason") is not None:
        value["goal"]["blockedReason"] = dict(goal["blockedReason"])
    return value


def _error_text(error: Exception) -> str:
    return f"Error: {error}" if not isinstance(error, GoalError) else f"Error: {error.message}"


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """注册三个目标工具与共享策略片段。"""
    resolved = _resolve_config(config)
    if hasattr(ctx, "systemPrompt"):
        ctx.systemPrompt.section(PromptSection(
            name="tool:goal",
            order=114,
            text=_guidance(resolved["blockedAfterConsecutiveRounds"]),
        ))

    async def handle_get(arguments: dict, exec: dict):
        try:
            execution = _goal_tool_execution(ctx, exec)
            return json.dumps(_goal_value(ctx.goals.get(execution["agent"])), ensure_ascii=False), False
        except GoalError as error:
            return _error_text(error), True

    async def handle_create(arguments: dict, exec: dict):
        try:
            execution = _goal_tool_execution(ctx, exec)
            _require_direct_human(ctx, execution)
            request: dict = {"objective": arguments["objective"]}
            if _has_round_cap(arguments.get("max_goal_rounds")):
                request["maxGoalRounds"] = int(arguments["max_goal_rounds"])
            goal = ctx.goals.create(execution["agent"], request)
            return json.dumps(_goal_value(goal), ensure_ascii=False), False
        except GoalError as error:
            return _error_text(error), True

    async def handle_update(arguments: dict, exec: dict):
        try:
            execution = _goal_tool_execution(ctx, exec)
            ref = _goal_ref(arguments.get("goal_id"), arguments.get("revision"))
            action = arguments.get("action")
            replacements: dict = {}
            if _has_text(arguments.get("objective")):
                replacements["objective"] = arguments["objective"]
            if _has_round_cap(arguments.get("max_goal_rounds")):
                replacements["maxGoalRounds"] = int(arguments["max_goal_rounds"])
            blocked_reason = arguments.get("blocked_reason")

            if action == "edit":
                _require_direct_human(ctx, execution)
                if _has_text(blocked_reason):
                    raise GoalError("blocked_reason is valid only with action blocked", "GOAL_TOOL_INVALID_UPDATE")
                goal = ctx.goals.edit(execution["agent"], ref, replacements)
                return json.dumps(_goal_value(goal), ensure_ascii=False), False
            if action in ("pause", "resume"):
                _require_direct_human(ctx, execution)
                if _has_text(arguments.get("objective")) or _has_round_cap(arguments.get("max_goal_rounds")) \
                        or _has_text(blocked_reason):
                    raise GoalError(
                        "objective and max_goal_rounds are valid only with action edit; "
                        "blocked_reason is valid only with action blocked",
                        "GOAL_TOOL_INVALID_UPDATE",
                    )
                goal = ctx.goals.pause(execution["agent"], ref) if action == "pause" \
                    else ctx.goals.resume(execution["agent"], ref)
                return json.dumps(_goal_value(goal), ensure_ascii=False), False
            if action not in ("complete", "blocked"):
                raise GoalError(f"unknown action {action!r}", "GOAL_TOOL_INVALID_UPDATE")

            authority = _completion_authority(ctx, execution)
            if _has_text(arguments.get("objective")) or _has_round_cap(arguments.get("max_goal_rounds")):
                raise GoalError("objective and max_goal_rounds are valid only with action edit", "GOAL_TOOL_INVALID_UPDATE")
            if action == "complete" and _has_text(blocked_reason):
                raise GoalError("blocked_reason is valid only with action blocked", "GOAL_TOOL_INVALID_UPDATE")
            if action == "blocked" and (not _has_text(blocked_reason)):
                raise GoalError("blocked_reason is required with action blocked", "GOAL_TOOL_INVALID_UPDATE")
            if action == "blocked" and authority["kind"] == "goal-round" \
                    and authority["goal"]["roundsStarted"] < resolved["blockedAfterConsecutiveRounds"]:
                raise GoalError(
                    f"blocked requires at least {resolved['blockedAfterConsecutiveRounds']} consecutive "
                    f"goal rounds; current round is {authority['goal']['roundsStarted']}",
                    "GOAL_TOOL_BLOCK_THRESHOLD",
                )
            goal = ctx.goals.complete(execution["agent"], ref) if action == "complete" \
                else ctx.goals.block(execution["agent"], ref, {"code": "model-reported", "message": blocked_reason})
            # dsh_py 无 exec.deferContext：wrapup 上下文注入省略（差异已注明），
            # 工具输出保持纯 JSON 结构化。
            return json.dumps(_goal_value(goal), ensure_ascii=False), False
        except GoalError as error:
            return _error_text(error), True

    ctx.tools.register("get_goal", _GET_DESCRIPTION, {"type": "object", "properties": {}}, handle_get)
    ctx.tools.register("create_goal", _CREATE_DESCRIPTION, {
        "type": "object",
        "properties": {
            "objective": {"type": "string", "description": "The concrete completion objective inferred from the direct human request."},
            "max_goal_rounds": {"type": "number", "description": "Optional positive safe-integer limit on automatic continuation rounds."},
        },
        "required": ["objective"],
    }, handle_create)
    ctx.tools.register("update_goal", _UPDATE_DESCRIPTION, {
        "type": "object",
        "properties": {
            "goal_id": {"type": "string", "description": "Exact id returned by get_goal."},
            "revision": {"type": "number", "description": "Exact positive revision returned by get_goal."},
            "action": {"type": "string", "enum": list(UPDATE_ACTIONS), "description": "edit | pause | resume | complete | blocked"},
            "objective": {"type": "string", "description": "Replacement objective; valid only with action edit."},
            "max_goal_rounds": {"type": "number", "description": "Replacement cap; valid only with action edit."},
            "blocked_reason": {"type": "string", "description": "Concrete blocking condition; required only with action blocked."},
        },
        "required": ["goal_id", "revision", "action"],
    }, handle_update)


apply.Config = Config
apply.name = PLUGIN_NAME
apply.inject = ["agents", "goals", "tools", "systemPrompt"]

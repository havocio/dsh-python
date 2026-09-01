"""
面向用户的 ``/goal`` 命令（对齐 dsh ``packages/goal/command-goal``）。

语法：``/goal [<objective>|clear|edit <objective>|pause|resume]``。命令不暴露
compare-and-set 内部：一律读取当前视图、用其精确 ref 发起变更。

**与 dsh 的差异（已注明）**：无（命令语义与输出逐行对齐）。
"""

from __future__ import annotations

from typing import Any

from dsh_py.core.context import AppContext
from dsh_py.services.commands import CommandDefinition, CommandInvocation, CommandResult
from dsh_py.services.goal_fold import GoalError

PLUGIN_NAME = "command-goal"

USAGE = "Usage: /goal [<objective>|clear|edit <objective>|pause|resume]"


def parse_goal_command(raw_input: str) -> dict:
    """只解析 ``/goal`` 拥有的语法；其他任意输入视为目标描述。"""
    input_ = raw_input.strip()
    if not input_:
        return {"kind": "show"}
    control = input_.lower()
    if control == "clear":
        return {"kind": "clear"}
    if control == "pause":
        return {"kind": "pause"}
    if control == "resume":
        return {"kind": "resume"}
    if control == "edit":
        return {"kind": "invalid-edit"}
    if input_[:4].lower() == "edit" and len(input_) > 4 and input_[4].isspace():
        return {"kind": "edit", "objective": input_[4:].strip()}
    return {"kind": "create", "objective": input_}


def _phase_label(phase: str) -> str:
    return phase


def _command_hint(goal: dict) -> str:
    if goal["phase"] == "active":
        return "/goal edit <objective>, /goal pause, /goal clear" if goal["activation"] == "armed" \
            else "/goal edit <objective>, /goal resume, /goal clear"
    if goal["phase"] == "paused" or goal["phase"] == "blocked":
        return "/goal edit <objective>, /goal resume, /goal clear"
    return "/goal <objective>, /goal clear"  # complete


def _render_goal(title: str, goal: dict) -> CommandResult:
    reason = goal.get("blockedReason")
    blocker = [] if reason is None else [f"Blocker: {reason['code']}: {reason['message']}"]
    return CommandResult(
        kind="success",
        text="\n".join([
            title,
            f"Status: {_phase_label(goal['phase'])}",
            *blocker,
            f"Objective: {goal['objective']}",
            f"Rounds: {goal['roundsStarted']}/{goal['maxGoalRounds']}",
            f"Activation: {goal['activation']}",
            "",
            f"Commands: {_command_hint(goal)}",
        ]),
    )


def _goal_ref(goal: dict) -> dict:
    return {"id": goal["id"], "revision": goal["revision"]}


def _missing_goal(action: str) -> CommandResult:
    return CommandResult(
        kind="error",
        text=f"No goal is currently set; /goal {action} requires one. {USAGE}",
    )


def execute_goal_command(ctx: AppContext, invocation: CommandInvocation) -> CommandResult:
    command = parse_goal_command(invocation.rawInput)
    try:
        current = ctx.goals.get(invocation.agent)
        kind = command["kind"]
        if kind == "show":
            if current is None:
                return CommandResult(kind="success", text=f"No goal is currently set.\n{USAGE}")
            return _render_goal("Goal", current)
        if kind == "invalid-edit":
            return CommandResult(kind="error", text=f"Goal editing requires a replacement objective.\n{USAGE}")
        if kind == "create":
            if current is not None and current["phase"] != "complete":
                return CommandResult(
                    kind="error",
                    text=f"A goal is already {_phase_label(current['phase'])}. Use /goal edit <objective> "
                         "to change it or /goal clear before replacing it.",
                )
            return _render_goal("Goal created", ctx.goals.create(invocation.agent, {"objective": command["objective"]}))
        if kind == "edit":
            if current is None:
                return _missing_goal("edit")
            if current["phase"] == "complete":
                return _render_goal("Goal created", ctx.goals.create(invocation.agent, {"objective": command["objective"]}))
            return _render_goal("Goal updated", ctx.goals.edit(invocation.agent, _goal_ref(current), {"objective": command["objective"]}))
        if kind == "pause":
            if current is None:
                return _missing_goal("pause")
            return _render_goal("Goal paused", ctx.goals.pause(invocation.agent, _goal_ref(current)))
        if kind == "resume":
            if current is None:
                return _missing_goal("resume")
            return _render_goal("Goal resumed", ctx.goals.resume(invocation.agent, _goal_ref(current)))
        if kind == "clear":
            if current is None:
                return CommandResult(kind="success", text="No goal to clear.")
            ctx.goals.clear(invocation.agent, _goal_ref(current))
            return CommandResult(kind="success", text="Goal cleared.")
        raise TypeError(f"unknown goal command {kind!r}")
    except GoalError:
        return CommandResult(
            kind="error",
            text="The goal command is not valid for the current state. Run /goal to view available commands.",
        )


def apply(ctx: AppContext) -> None:
    """注册 ``/goal`` 命令（commands 缝装配时）。"""
    if not hasattr(ctx, "commands"):
        return
    ctx.commands.register(CommandDefinition(
        name="goal",
        description="set or view the goal for a long-running task",
        handler=lambda invocation: execute_goal_command(ctx, invocation),
    ))


apply.Config = None
apply.name = PLUGIN_NAME
apply.inject = ["commands", "goals"]

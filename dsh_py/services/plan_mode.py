"""
plan 协作状态（对齐 dsh ``packages/plan/plan-mode``）。

Plan mode 是按 agent 记录的协作状态：active 时在每次模型请求中注入部署方
指引片段（``plan:policy``），``exit_plan_mode`` 工具呈现完整计划供用户审查，
``/plan off`` 让用户直接离开。生效状态从会话日志折叠（``plan/mode``，最后
一个胜出），resume/fork 无需活动镜像即可恢复；用户选择在下一个被接受的
回合内 pre-step 前保持 pending。

**与 dsh 的差异（已注明）**：
- 无 ``request/header`` 会话事件 → ``narration``（模式切换播报）恒省略；
- 无 ``userQuestions`` 缝 → ``exit_plan_mode`` 的审查流程不可用，工具在
  无审查通道时抛明确错误（对齐 dsh 的缺通道行为）；
- dsh 的 ``agent.inject``（立即注入）/``agent.steer``（下回合注入）→ dsh_py
  统一用 ``agent.insert``（收件箱下回合投递）；
- 可选子部件（投影/命令/section）用 ``hasattr`` 守卫（dsh 用运行时
  ``ctx.inject``，dsh_py 无此机制）。
"""

from __future__ import annotations

import weakref
from typing import Any, Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.message import MessageSource, TextBlock, create_user_message
from dsh_py.services.system_prompt import PromptSection

PLUGIN_NAME = "plan-mode"
EXIT_PLAN_MODE = "exit_plan_mode"

Config = z.object({
    "section": z.string().default(""),
})


def resolve_config(config: Optional[dict]) -> dict:
    """校验部署方指引：非字符串/空白/未知键在加载期失败。"""
    cfg = config or {}
    section = cfg.get("section")
    if not isinstance(section, str):
        raise TypeError("PlanModeConfig needs a string `section`")
    if not section.strip():
        raise ValueError("PlanModeConfig needs a non-empty `section`")
    unknown = [k for k in cfg if k != "section"]
    if unknown:
        raise ValueError(f"PlanModeConfig has unknown key(s) {', '.join(map(str, unknown))} — config is {{ section }}")
    return {"section": section}


def fold_plan_mode(events: list, end: Optional[int] = None) -> bool:
    """``plan/mode`` 最后一个胜出；无事件前缀为 inactive。"""
    if end is None:
        end = len(events)
    active = False
    index = 0
    for event in events:
        if index >= end:
            break
        index += 1
        if event.type == "plan/mode":
            active = bool(event.data.get("active") if isinstance(event.data, dict) else event.data)
    return active


def _has_open_turn(events: list) -> bool:
    open_ = False
    for event in events:
        if event.type == "turn/start":
            open_ = True
        elif event.type == "turn/end":
            open_ = False
    return open_


class PlanModeController(Service):
    """``ctx.planMode``：拥有已记录 plan 状态、step 起点应用/播报、``plan:policy``
    片段、``/plan`` 命令与稳定退出工具。UI 经 ``session/event`` 观察已提交翻转，
    无活动镜像。"""

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx, "planMode")
        self.section = resolve_config(config or {})["section"]
        # 每个会话待下一被接受的回合内 pre-step 应用的选择（narrate 恒 False：
        # dsh_py 无 request/header 事件，narration 省略）
        self._pending_intents: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._disposed = False
        ctx.effect(lambda: setattr(self, "_disposed", True), label="plan-mode.close")

        @ctx.on("agent/pre-step")
        async def on_pre_step(event: dict, next):
            decision = await next()
            pending = self._pending_intents.get(event["agent"].session)
            if decision.get("kind") == "reject" or pending is None:
                return decision
            try:
                self._on_boundary(event["agent"].session)
            except Exception:  # noqa: BLE001 追加失败保持 pending，策略不阻断 step
                return decision
            return decision

        if hasattr(ctx, "systemPrompt"):
            def _policy_text(context: dict) -> str:
                agent = context.get("agent")
                if agent is None:
                    return ""
                pending = self._pending_intents.get(agent.session)
                active = pending["active"] if pending is not None else fold_plan_mode(agent.session.events)
                return self.section if active else ""

            ctx.systemPrompt.section(PromptSection(
                name="plan:policy",
                order=50,
                text=_policy_text,
            ))

        if hasattr(ctx, "sessionProjections"):
            from dsh_py.services.projection import ProjectionDefinition
            ctx.sessionProjections.register(ProjectionDefinition(
                key="plan",
                schema=None,  # 透传：view 已产出规范 {active, pending}
                init=lambda: {"active": False, "wanted": None},
                apply=self._apply_projection,
                view=lambda state: {
                    "active": state["active"],
                    "pending": state["wanted"] is not None and state["wanted"] != state["active"],
                },
                state_version=1,
            ))

        if hasattr(ctx, "commands"):
            ctx.commands.register(
                "plan",
                "Enter or leave plan mode",
                lambda invocation: self._run_plan_command(invocation),
            )

        ctx.tools.register(EXIT_PLAN_MODE, _EXIT_DESCRIPTION, {
            "type": "object",
            "properties": {
                "plan": {"type": "string", "description": "The complete plan, as markdown, starting with a # heading that names it."},
            },
            "required": ["plan"],
        }, self._handle_exit)

    # ------------------------------------------------------------------ #
    # 读 / 选
    # ------------------------------------------------------------------ #
    def get(self, agent) -> dict:
        """当前已记录状态 + 待应用选择（如有）。"""
        active = fold_plan_mode(agent.session.events)
        pending = self._pending_intents.get(agent.session)
        return {"active": active, "pending": pending["active"]} if pending is not None else {"active": active}

    def set(self, agent, active: bool) -> str:
        """选择 plan mode 是否生效：committed（立即记录）/queued（待下一步）/
        cancelled（清掉相反待选）/noop（已在该状态）。"""
        session = agent.session
        pending = self._pending_intents.get(session)
        target = pending["active"] if pending is not None else fold_plan_mode(session.events)
        if active == target:
            return "noop"
        if _has_open_turn(session.events):
            self._pending_intents[session] = {"active": active}
            return "cancelled" if fold_plan_mode(session.events) == active else "queued"
        if active == fold_plan_mode(session.events):
            self._pending_intents.pop(session, None)
            return "cancelled"
        session.append("plan/mode", {"active": active})
        self._pending_intents.pop(session, None)
        return "committed"

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _on_boundary(self, session) -> None:
        """在下一次请求装配前追加一条待选。"""
        pending = self._pending_intents.get(session)
        if pending is None:
            return
        if pending["active"] == fold_plan_mode(session.events):
            self._pending_intents.pop(session, None)
            return
        session.append("plan/mode", {"active": pending["active"]})
        self._pending_intents.pop(session, None)

    @staticmethod
    def _apply_projection(state, event):
        """plan 投影单元：command/run 记录选择、plan/mode 记录并清除。"""
        if event.type == "command/run":
            data = event.data if isinstance(event.data, dict) else {}
            if data.get("name") != "plan" or data.get("args") is None:
                return state
            wanted = data["args"].strip() != "off"
            return state if wanted == state["wanted"] else {"active": state["active"], "wanted": wanted}
        if event.type == "plan/mode":
            data = event.data if isinstance(event.data, dict) else {}
            return {"active": bool(data.get("active", False)), "wanted": None}
        return state

    def _run_plan_command(self, invocation) -> Any:
        from dsh_py.services.commands import CommandResult
        agent = invocation.agent
        message = invocation.rawInput.strip()
        if message == "off":
            outcome = self.set(agent, False)
            if outcome == "committed":
                return CommandResult(kind="success", text="Plan mode off.")
            if outcome == "queued":
                return CommandResult(kind="success", text="Leaving plan mode (applies from the next step).")
            if outcome == "cancelled":
                return CommandResult(kind="success", text="Plan mode entry cancelled.")
            return CommandResult(
                kind="success",
                text="Leaving plan mode (applies from the next step)."
                if fold_plan_mode(agent.session.events) else "Plan mode is already inactive.",
            )
        outcome = self.set(agent, True)
        if message:
            agent.insert(create_user_message(
                [TextBlock(message)],
                source=MessageSource("user"),
            ))
        return CommandResult(
            kind="success",
            text="Plan mode on. Use /plan off to leave." if outcome == "committed"
            else "Entering plan mode (applies from the next step). Use /plan off to leave.",
        )

    async def _handle_exit(self, arguments: dict, exec: dict):
        import json as _json
        agent = exec.get("agent")
        if agent is None:
            raise RuntimeError(f"{EXIT_PLAN_MODE} requires a calling agent (no session to switch)")
        if not fold_plan_mode(agent.session.events):
            raise RuntimeError(f"{EXIT_PLAN_MODE} is only available in plan mode")
        plan = arguments.get("plan", "")
        if not (isinstance(plan, str) and plan.strip().startswith("# ") and len(plan.strip()) > 2):
            raise RuntimeError(f"{EXIT_PLAN_MODE} requires a non-empty markdown plan starting with a # heading")
        if not hasattr(self.ctx, "userQuestions"):
            raise RuntimeError(
                "no user-questions channel is available to review the plan; "
                "ask the user to switch the session mode instead",
            )
        # dsh_py 无 userQuestions 缝：此分支不可达（见模块 docstring 差异）。
        # 若未来接入审查通道，在此 ask 并依据答案批准/继续。
        self._pending_intents[agent.session] = {"active": False}
        return _json.dumps({"approved": True}), False


_EXIT_DESCRIPTION = (
    "Use only in plan mode. Present your plan for the user's review and, on approval, leave plan mode. "
    "Send the COMPLETE plan as markdown, starting with a # heading that names it. "
    "The user may approve (carry out the plan from your next step) or keep "
    "planning — their feedback comes back in the tool result; revise and present again."
)


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：注册 ``ctx.planMode``（+ 可选投影/命令/section）。"""
    PlanModeController(ctx, config)


apply.Config = Config
apply.name = PLUGIN_NAME
apply.inject = ["tools"]
apply.provides = ["planMode"]

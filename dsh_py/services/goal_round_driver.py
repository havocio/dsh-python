"""同会话 goal-round 驱动（对标 dsh 的 ``@deepseek-ai/dsh-goal-round-driver``）。

经公开 agent/session/goal 服务安装自动同会话续行及其竞态栅栏：agent 空闲且
目标 active+armed 时，为下一轮渲染续行提示、经 ``agent.followup`` 入收件箱，
并用预约台账 + pre-step 校验保证**恰好一个**续行轮被准入；目标到达回合上限
即 block，取消/搁置/过期轮不会泄漏为伪续行。

**与 dsh 差异**：

- ``ctx.agents.get/list`` → ``ctx.agentLoop.get/roots``（dsh_py 的活 agent 注册
  表在 agentLoop 服务上；goals 的 assert_live 也如此校验）。
- ``agent.status`` 属性不存在——经 ``agent/status`` 事件在驱动状态里跟踪。
- ``agents.withoutInitiator``（dsh 用其压制 initiator 反馈回路）在 dsh_py 无
  initiator 概念，省略；驱动循环以普通任务串行化（状态机本身防重入）。
- ``agent/error`` 事件不存在（dsh_py 只有 ``agent/request-error`` 瀑布流），
  省略「出错即卸武」钩子；其余栅栏覆盖释放路径。
- ``sessions.flush`` 是同步方法，直接调用。
"""

from __future__ import annotations

import json
import weakref
from dataclasses import dataclass, field
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.fiber import FiberState
from dsh_py.services.message import MessageSource, TextBlock, create_user_message

# --------------------------------------------------------------------------- #
# 纯函数：续行提示
# --------------------------------------------------------------------------- #


def render_goal_round_prompt(goal: dict, round_number: int) -> list:
    """渲染保留在会话历史中的完整 goal-round 指令（模型可见续行提示）。

    :param goal: 被准入的精确 active goal 修订视图（dict）。
    :param round_number: 下一正数回合号。
    :returns: 给 ``agent.followup()`` 的单文本块 prompt。
    """
    text = (
        "<goal_round>\n"
        f"Objective: {json.dumps(goal['objective'], ensure_ascii=False)}\n"
        f"Round: {round_number}/{goal['maxGoalRounds']}\n\n"
        "Continue working toward the objective in this same session. Treat the current workspace, "
        "tool results, and durable session state as authoritative; inspect them instead of assuming "
        "earlier narration is still current. Make concrete progress and verify the result. Before "
        "claiming completion, gather evidence that the whole objective is achieved, read the current "
        "goal, and mark it complete. If work remains, leave the goal active for the next round. Follow "
        "the configured goal-tool policy before reporting a blocker.\n"
        "</goal_round>"
    )
    return [TextBlock(text=text)]


# --------------------------------------------------------------------------- #
# 驱动状态
# --------------------------------------------------------------------------- #


@dataclass
class RoundAttempt:
    """一条已预约、已领取或已准入的 goal 消息，保留到整 agent 静默。"""

    goal_id: str
    revision: int
    round: int
    message_id: str
    content: tuple
    phase: str = "queued"  # queued | claimed | admitted
    cancelled: bool = False
    stale: bool = False


@dataclass
class DriverState:
    """一次精确 Agent 生命周期的进程本地串行调度状态。"""

    agent: Any
    attempt: Optional[RoundAttempt] = None
    competing_queued: bool = False
    needs_checkpoint: bool = False
    requested: bool = False
    run: Optional[Any] = None
    stopping: bool = False
    status: str = "idle"


def is_goal_round_source(source: Any) -> bool:
    """一个来源是否识别为自动的正数 goal 轮。"""
    return source.kind == "goal" and source.round is not None and source.round > 0


def same_round(source: Any, round_identity: RoundAttempt) -> bool:
    return (
        source.goalId == round_identity.goal_id
        and source.revision == round_identity.revision
        and source.round == round_identity.round
    )


def same_queued(content: tuple, source: Any, attempt: RoundAttempt) -> bool:
    return is_goal_round_source(source) and same_round(source, attempt) and tuple(content) == attempt.content


def goal_ref(goal: dict) -> dict:
    return {"id": goal["id"], "revision": goal["revision"]}


def _render_thrown(value: Any) -> str:
    return value.message if isinstance(value, BaseException) else str(value)


# --------------------------------------------------------------------------- #
# 插件入口
# --------------------------------------------------------------------------- #


def apply(ctx: AppContext, config: Any = None) -> None:
    states: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def state_for(agent: Any) -> DriverState:
        existing = states.get(agent)
        if existing is not None:
            return existing
        state = DriverState(agent=agent)
        states[agent] = state
        return state

    def current_goal(state: DriverState) -> Optional[dict]:
        """仅当精确 Agent 仍活着时读取。"""
        if ctx.agentLoop.get(state.agent.id) is not state.agent:
            return None
        return ctx.goals.get(state.agent)

    def ready_to_drive(state: DriverState) -> bool:
        return (
            ctx.fiber.state == FiberState.ACTIVE
            and not state.stopping
            and ctx.agentLoop.get(state.agent.id) is state.agent
            and state.status == "idle"
            and not state.competing_queued
        )

    def ready_after_checkpoint(state: DriverState) -> bool:
        return ready_to_drive(state) and not state.needs_checkpoint

    def disarm(state: DriverState) -> None:
        """移除自动续行权限（保留持久 phase）。"""
        try:
            goal = current_goal(state)
            if goal is not None and goal["activation"] == "armed":
                ctx.goals.disarm(state.agent)
        except Exception as error:  # noqa: BLE001 - 释放尽力而为
            ctx.logger.warn(f'goal-round-driver: could not disarm agent "{state.agent.id}": {_render_thrown(error)}')

    def restore_other_claimed(agent: Any, messages: list, message_id: str) -> None:
        """驱动只丢弃自己的轮时，保留已被领取的 step 上下文。"""
        retained = [
            m for m in messages
            if m.id != message_id and not (m.source.kind == "goal" and m.source.round == 0)
        ]
        for message in reversed(retained):
            if any(c.id == message.id for c in agent.inbox.next_step):
                continue
            if any(c.id == message.id for c in agent.inbox.next_turn):
                continue
            agent.inbox.prepend("next-step", message)

    async def drive(state: DriverState) -> None:
        """在静默处处理已准入工作，然后预约至多下一轮。"""
        agent = state.agent
        if not ready_to_drive(state):
            return

        if state.needs_checkpoint:
            state.needs_checkpoint = False
            try:
                ctx.sessions.flush(agent.session)
            except Exception as error:  # noqa: BLE001 - 耐久检查点失败
                ctx.logger.warn(
                    f'goal-round-driver: durability checkpoint failed for agent "{agent.id}": {_render_thrown(error)}'
                )
                disarm(state)
                return
            # 检查点沉降期间可能到达变更或普通提示：给它自己的检查点/轮再预约。
            if not ready_after_checkpoint(state):
                return

        attempt = state.attempt
        if attempt is not None:
            state.attempt = None
            state.needs_checkpoint = True
            state.requested = True
            return

        goal = current_goal(state)
        if goal is None or goal["phase"] != "active" or goal["activation"] != "armed":
            return
        if goal["roundsStarted"] >= goal["maxGoalRounds"]:
            ctx.goals.block(agent, goal_ref(goal), {
                "code": "round-limit",
                "message": f"Goal reached its configured limit of {goal['maxGoalRounds']} rounds.",
            })
            return

        round_number = goal["roundsStarted"] + 1
        content = tuple(render_goal_round_prompt(goal, round_number))
        message = create_user_message(
            list(content),
            MessageSource(kind="goal", goalId=goal["id"], revision=goal["revision"], round=round_number),
        )
        reservation = RoundAttempt(
            goal_id=goal["id"],
            revision=goal["revision"],
            round=round_number,
            message_id=message.id,
            content=content,
            phase="queued",
        )
        state.attempt = reservation
        try:
            agent.followup(message)
        except Exception as error:  # noqa: BLE001 - 无法入箱
            state.attempt = None
            ctx.logger.warn(
                f'goal-round-driver: could not queue round {round_number} for agent "{agent.id}": {_render_thrown(error)}'
            )
            latest = current_goal(state)
            if (
                latest is not None
                and latest["id"] == goal["id"]
                and latest["revision"] == goal["revision"]
                and latest["phase"] == "active"
                and latest["activation"] == "armed"
            ):
                ctx.goals.block(agent, goal_ref(latest), {
                    "code": "queue-failed",
                    "message": f"Could not queue goal round {round_number}: {_render_thrown(error)}",
                })

    def request_drive(state: DriverState) -> None:
        """把触发合并到一个 agent 本地串行驱动上。"""
        if state.stopping:
            return
        state.requested = True
        if state.run is not None:
            return

        async def _loop() -> None:
            while state.requested and not state.stopping:
                state.requested = False
                try:
                    await drive(state)
                except Exception as error:  # noqa: BLE001 - 驱动失败卸武
                    ctx.logger.warn(
                        f'goal-round-driver: driver failed for agent "{state.agent.id}": {_render_thrown(error)}'
                    )
                    disarm(state)

        run = asyncio_create_task(_loop())
        state.run = run

        def retire(_task: Any = None) -> None:
            state.run = None
            if state.requested and not state.stopping:
                request_drive(state)

        run.add_done_callback(retire)

    # -- 监听器 -------------------------------------------------------------- #
    @ctx.on("agent/session-start")
    def on_session_start(payload: Any) -> None:
        event = payload if isinstance(payload, dict) else {}
        agent = event.get("agent")
        if agent is None:
            return
        state = state_for(agent)
        state.attempt = None
        state.competing_queued = False
        state.needs_checkpoint = False
        state.status = "idle"

    @ctx.on("agent/status")
    def on_status(payload: dict) -> None:
        agent = payload.get("agent")
        if agent is None:
            return
        state = state_for(agent)
        state.status = payload.get("status", "idle")
        if state.status == "idle":
            state.competing_queued = False
            attempt = state.attempt
            goal = current_goal(state)
            if (
                attempt is not None
                and (attempt.phase in ("queued", "claimed") or attempt.cancelled)
                and goal is not None
                and goal["phase"] == "active"
                and goal["activation"] == "armed"
            ):
                state.attempt = None
                try:
                    ctx.goals.pause(agent, goal_ref(goal))
                except Exception as error:  # noqa: BLE001
                    ctx.logger.warn(
                        f'goal-round-driver: could not pause cancelled goal for agent "{agent.id}": {_render_thrown(error)}'
                    )
                    disarm(state)
            request_drive(state)

    @ctx.on("goal/changed")
    def on_goal_changed(payload: dict) -> None:
        agent = payload.get("agent")
        if agent is None:
            return
        state = state_for(agent)
        state.needs_checkpoint = True
        request_drive(state)

    @ctx.on("agent/inbox/inserted")
    def on_inbox_inserted(agent: Any, message: Any) -> None:
        if not any(c.id == message.id for c in agent.inbox.next_turn):
            return
        state = state_for(agent)
        attempt = state.attempt
        if attempt is not None and same_queued(message.content, message.source, attempt):
            return
        state.competing_queued = True
        if attempt is not None and attempt.phase == "queued":
            attempt.stale = True

    @ctx.on("agent/inbox/claimed")
    def on_inbox_claimed(agent: Any, message: Any, turn: Any = None) -> None:
        state = state_for(agent)
        attempt = state.attempt
        if attempt is not None and same_queued(message.content, message.source, attempt):
            attempt.phase = "claimed"

    @ctx.on("agent/inbox/discarded")
    def on_inbox_discarded(agent: Any, message: Any) -> None:
        state = state_for(agent)
        attempt = state.attempt
        if attempt is not None and same_queued(message.content, message.source, attempt):
            attempt.cancelled = True

    @ctx.on("session/event")
    def on_session_event(session: Any, event: Any) -> None:
        try:
            session_id = session.header.id
        except Exception:  # noqa: BLE001 - 异常会话对象防御
            return
        agent = ctx.agentLoop.get(session_id)
        if agent is None or agent.session is not session:
            return
        state = state_for(agent)
        if event.type == "user/message":
            if state.attempt is not None and event.data.id == state.attempt.message_id:
                state.attempt.phase = "admitted"
            return
        if event.type == "turn/end":
            reason = event.data.get("reason") if isinstance(event.data, dict) else None
            kind = reason.get("kind") if isinstance(reason, dict) else None
            if kind == "max-tokens":
                disarm(state)
                return
            if kind != "cancelled":
                return
            attempt = state.attempt
            if attempt is not None and attempt.phase in ("claimed", "admitted"):
                attempt.cancelled = True
            else:
                disarm(state)
            return

    def valid_reservation(state: DriverState, content: tuple, source: Any) -> bool:
        """除非队列中的提示仍拥有精确的活修订，否则失败关闭。"""
        attempt = state.attempt
        goal = current_goal(state)
        return (
            ctx.fiber.state == FiberState.ACTIVE
            and not state.stopping
            and attempt is not None
            and attempt.phase == "claimed"
            and not attempt.stale
            and same_queued(content, source, attempt)
            and goal is not None
            and goal["id"] == source.goalId
            and goal["revision"] == source.revision
            and goal["phase"] == "active"
            and goal["activation"] == "armed"
            and source.round == goal["roundsStarted"] + 1
        )

    @ctx.on("agent/pre-step")
    async def on_pre_step(payload: dict, nxt: Any) -> Any:
        agent = payload["agent"]
        messages = payload.get("messages", [])
        signal = payload.get("signal")
        submitted = next((m for m in messages if is_goal_round_source(m.source)), None)
        if submitted is None:
            return await nxt()
        content, source = submitted.content, submitted.source
        state = state_for(agent)
        valid = False
        try:
            valid = valid_reservation(state, content, source)
        except Exception as error:  # noqa: BLE001 - 预约检查失败卸武
            ctx.logger.warn(
                f'goal-round-driver: pre-step check failed for agent "{agent.id}": {_render_thrown(error)}'
            )
            disarm(state)
        if not valid:
            attempt = state.attempt
            if attempt is not None and same_round(source, attempt):
                attempt.stale = True
                state.attempt = None
            restore_other_claimed(agent, messages, submitted.id)
            request_drive(state)
            return {"kind": "reject"}
        try:
            decision = await nxt()
        except Exception as error:  # noqa: BLE001 - 下游 hook 丢掉整个 step 提议
            if signal is not None and getattr(signal, "aborted", False):
                raise
            state.attempt = None
            request_drive(state)
            raise
        if signal is not None and getattr(signal, "aborted", False):
            if decision.get("kind") == "enter":
                restore_other_claimed(agent, decision.get("messages", []), submitted.id)
            return decision
        if decision.get("kind") == "reject":
            state.attempt = None
            goal = current_goal(state)
            if (
                goal is not None
                and goal["id"] == source.goalId
                and goal["revision"] == source.revision
                and goal["phase"] == "active"
                and goal["activation"] == "armed"
            ):
                ctx.goals.block(agent, goal_ref(goal), {
                    "code": "prompt-rejected",
                    "message": "Goal round was rejected before entering its step.",
                })
            return decision
        try:
            valid = valid_reservation(state, content, source)
        except Exception as error:  # noqa: BLE001 - 决策后复查失败卸武
            ctx.logger.warn(
                f'goal-round-driver: post-decision check failed for agent "{agent.id}": {_render_thrown(error)}'
            )
            disarm(state)
            valid = False
        if not valid:
            state.attempt = None
            restore_other_claimed(agent, decision.get("messages", []), submitted.id)
            request_drive(state)
            return {"kind": "reject"}
        return decision

    # 在既有 agent 上加载生命周期驱动不继承早前 producer 实例的隐藏自动权限。
    for agent in ctx.agentLoop.roots():
        disarm(state_for(agent))

    # 释放：停止全部驱动、卸武、取消带待处理预约的运行 agent。
    def _teardown() -> None:
        for state in list(states.values()):
            state.stopping = True
            disarm(state)
            attempt = state.attempt
            if attempt is not None and state.status == "running":
                try:
                    state.agent.cancel("goal-round-driver lifecycle disposed")
                except Exception:  # noqa: BLE001
                    pass
            run = state.run
            if run is not None and not run.done():
                run.cancel()

    ctx.effect(lambda: _teardown, label="goal-round-driver lifecycle")


def asyncio_create_task(coro: Any) -> Any:
    import asyncio

    return asyncio.create_task(coro)

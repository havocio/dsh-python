"""plan/plan-mode 验证（第 3 层）。

运行：python dsh_py/tests/test_plan_mode.py

覆盖：
- fold_plan_mode：最后 plan/mode 胜出、无事件 inactive、前缀截断；
- resolve_config：非字符串/空白/未知键拒绝；
- set：committed（无开放回合立即记录）/queued（开放回合待下步）/noop/cancelled；
- pre-step 边界：queued 选择在下次被接受 pre-step 追加 plan/mode；
- /plan 命令：on/off + 消息注入；
- exit_plan_mode：非 plan mode 拒绝、plan 非 # 开头拒绝、无 userQuestions 拒绝；
- 投影单元：command/run 记录选择、plan/mode 清除。
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.agent import AgentOptions

import dsh_py.services.plan_mode as pm
from dsh_py.services.plan_mode import EXIT_PLAN_MODE, fold_plan_mode, resolve_config


def _ctx(section="你处于计划模式，先给出完整计划再执行。", extra=None):
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    from dsh_py.services.commands import apply as commands_apply
    from dsh_py.services.llm import ChunkType, LlmAdapter, StreamChunk
    commands_apply(ctx)

    class _Echo(LlmAdapter):
        async def stream(self, options):
            yield StreamChunk(ChunkType.TEXT_DELTA, text="（mock）")
            yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})

    ctx.llm.register_adapter(["mock"], _Echo())
    pm.apply(ctx, {"section": section})
    if extra:
        extra(ctx)
    return ctx


def _agent(ctx):
    session = ctx.sessions.create(cwd=None)
    agent = ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))
    return session, agent


async def _invoke(ctx, name, agent, raw_input="", signal=None):
    """按现行命令 API 执行（``execute(agent, line, signal)``），解包 ``.result``。"""
    from dsh_py.core.signal import CancelSignal

    line = f"/{name}" + (f" {raw_input}" if raw_input else "")
    execution = await ctx.commands.execute(agent, line, signal or CancelSignal())
    return execution.result if execution is not None else None


def _mk_event(type_, data, seq=1):
    from types import SimpleNamespace
    return SimpleNamespace(type=type_, data=data, seq=seq)


# --------------------------------------------------------------------------- #
# 纯函数与配置
# --------------------------------------------------------------------------- #
def test_fold_plan_mode():
    assert fold_plan_mode([]) is False
    events = [
        _mk_event("user/message", None),
        _mk_event("plan/mode", {"active": True}),
        _mk_event("user/message", None),
    ]
    assert fold_plan_mode(events) is True
    events.append(_mk_event("plan/mode", {"active": False}))
    assert fold_plan_mode(events) is False
    # 前缀截断
    assert fold_plan_mode(events, end=3) is True


def test_resolve_config_validation():
    assert resolve_config({"section": "ok"}) == {"section": "ok"}
    for bad in ({"section": ""}, {"section": "  "}, {"section": 42}, {"section": "x", "extra": 1}):
        try:
            resolve_config(bad)
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError(f"应拒绝：{bad}")


# --------------------------------------------------------------------------- #
# set / get 语义
# --------------------------------------------------------------------------- #
async def test_set_commit_queued_noop_cancelled():
    ctx = _ctx()
    session, agent = _agent(ctx)

    # 无开放回合：立即 committed
    assert ctx.planMode.get(agent) == {"active": False}
    assert ctx.planMode.set(agent, True) == "committed"
    assert fold_plan_mode(session.events) is True
    assert ctx.planMode.set(agent, True) == "noop"

    # 开放回合：queued（待下个 pre-step）
    session.append("turn/start", {"reason": "test"})
    assert ctx.planMode.set(agent, False) == "queued"
    assert ctx.planMode.get(agent) == {"active": True, "pending": False}
    assert fold_plan_mode(session.events) is True  # 尚未落盘

    # 相反待选覆盖：再选 True → cancelled（新选择保留 pending，日志已 True）
    assert ctx.planMode.set(agent, True) == "cancelled"
    assert ctx.planMode.get(agent) == {"active": True, "pending": True}


async def test_pre_step_applies_queued_selection():
    ctx = _ctx()
    session, agent = _agent(ctx)
    session.append("turn/start", {"reason": "test"})
    ctx.planMode.set(agent, True)  # queued

    async def default_decision():
        return {"kind": "enter", "messages": []}
    decision = await ctx.waterfall(
        "agent/pre-step",
        {"agent": agent, "messages": [], "turn": 1, "step": 1, "signal": None},
        inner=default_decision,
    )
    assert decision["kind"] == "enter"
    assert fold_plan_mode(session.events) is True  # 边界已追加
    assert ctx.planMode.get(agent) == {"active": True}

    # reject 决策：不应用
    ctx.planMode.set(agent, False)  # queued（open turn 仍开）
    async def reject_inner():
        return {"kind": "reject"}
    await ctx.waterfall(
        "agent/pre-step",
        {"agent": agent, "messages": [], "turn": 2, "step": 1, "signal": None},
        inner=reject_inner,
    )
    assert fold_plan_mode(session.events) is True  # 仍 active
    assert ctx.planMode.get(agent) == {"active": True, "pending": False}


# --------------------------------------------------------------------------- #
# /plan 命令
# --------------------------------------------------------------------------- #
async def test_plan_command_on_off_message():
    ctx = _ctx()
    session, agent = _agent(ctx)

    r = await _invoke(ctx, "plan", agent, "")
    assert r.kind == "success" and "Plan mode on" in r.text
    assert fold_plan_mode(session.events) is True

    r = await _invoke(ctx, "plan", agent, "off")
    assert r.kind == "success" and "Plan mode off" in r.text
    assert fold_plan_mode(session.events) is False

    # 带消息：进入 plan mode 并注入 steer 消息（下回合收件箱）
    r = await _invoke(ctx, "plan", agent, "我们先用计划模式工作")
    assert r.kind == "success" and "Plan mode on" in r.text
    assert agent.inbox.has_pending is True


# --------------------------------------------------------------------------- #
# exit_plan_mode 工具
# --------------------------------------------------------------------------- #
async def test_exit_plan_mode_rejections_and_no_channel():
    ctx = _ctx()
    session, agent = _agent(ctx)

    # 非 plan mode：拒绝
    text, is_error, _ = await ctx.tools.execute_with_agent(
        EXIT_PLAN_MODE, '{"plan": "# 计划\\n1. 做 A\\n2. 做 B"}', agent=agent)
    assert is_error is True and "only available in plan mode" in text

    # plan mode 中：无 userQuestions 通道 → 拒绝
    ctx.planMode.set(agent, True)
    text, is_error, _ = await ctx.tools.execute_with_agent(
        EXIT_PLAN_MODE, '{"plan": "# 计划\\n1. 做 A\\n2. 做 B"}', agent=agent)
    assert is_error is True and "no user-questions channel" in text

    # plan 非 # 开头：拒绝
    text, is_error, _ = await ctx.tools.execute_with_agent(
        EXIT_PLAN_MODE, '{"plan": "没有标题的计划"}', agent=agent)
    assert is_error is True and "# heading" in text

    # 缺参数：拒绝
    text, is_error, _ = await ctx.tools.execute_with_agent(EXIT_PLAN_MODE, "{}", agent=agent)
    assert is_error is True


# --------------------------------------------------------------------------- #
# 投影单元
# --------------------------------------------------------------------------- #
def test_plan_projection_fold():
    controller = pm.PlanModeController.__new__(pm.PlanModeController)  # 只测静态方法
    state = {"active": False, "wanted": None}
    # command/run name=plan args="写代码" → wanted True
    state = controller._apply_projection(state, _mk_event("command/run", {"name": "plan", "args": "写代码"}))
    assert state == {"active": False, "wanted": True}
    # 再次相同选择 → 同一引用
    state2 = controller._apply_projection(state, _mk_event("command/run", {"name": "plan", "args": "写代码"}))
    assert state2 is state
    # plan/mode active → 清除 wanted
    state = controller._apply_projection(state, _mk_event("plan/mode", {"active": True}))
    assert state == {"active": True, "wanted": None}
    # command/run args="off" → wanted False
    state = controller._apply_projection(state, _mk_event("command/run", {"name": "plan", "args": "off"}))
    assert state == {"active": True, "wanted": False}
    # 无关事件 → 同一引用
    state3 = controller._apply_projection(state, _mk_event("user/message", None))
    assert state3 is state


def _run_all():
    fails = 0
    sync_tests = [
        test_fold_plan_mode,
        test_resolve_config_validation,
        test_plan_projection_fold,
    ]
    async_tests = [
        test_set_commit_queued_noop_cancelled,
        test_pre_step_applies_queued_selection,
        test_plan_command_on_off_message,
        test_exit_plan_mode_rejections_and_no_channel,
    ]
    for fn in sync_tests:
        try:
            fn()
            print(f"OK   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"FAIL {fn.__name__}: {e}")
    for fn in async_tests:
        try:
            asyncio.run(fn())
            print(f"OK   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(sync_tests) + len(async_tests) - fails} 项通过，{fails} 项失败")
    if fails:
        raise SystemExit(f"\n{fails} 项失败")


if __name__ == "__main__":
    _run_all()

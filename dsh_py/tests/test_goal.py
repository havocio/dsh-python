"""goal 家族验证（第 3 层）。

运行：python dsh_py/tests/test_goal.py

覆盖：
- 纯函数域（goal_fold）：decode_goal_change 严格解码（快照/墓碑/畸形拒绝）、
  apply_goal_change 变更校验（create 规则/transition/计数保留）、apply_goal_event
  （goal round 推进/非法拒绝）、fold_goal、apply_goal_projection last-wins；
- 服务（ctx.goals）：create→edit→pause→resume→complete→clear 生命周期、CAS
  陈旧修订拒绝、activation 语义、goal/changed 事件、typert 远程调用；
- 工具（tool-goal）：get_goal null、create_goal 直接人类权威、update_goal
  edit/pause/resume 权威、complete 经 goal-round、blocked 阈值；
- 命令（command-goal）：/goal 全语法（show/create/edit/pause/resume/clear）。
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.agent import AgentOptions
from dsh_py.services.message import MessageSource, TextBlock, create_user_message

import dsh_py.plugins.command_goal as command_goal
import dsh_py.plugins.tool_goal as tool_goal
import dsh_py.services.goal as goal_service
import dsh_py.services.goal_fold as gf
from dsh_py.services.goal_fold import GoalError, apply_goal_change, decode_goal_change, empty_goal_fold_state


def _ctx(extra=None):
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    from dsh_py.services.commands import apply as commands_apply
    commands_apply(ctx)
    goal_service.apply(ctx)
    tool_goal.apply(ctx)
    command_goal.apply(ctx)
    if extra:
        extra(ctx)
    return ctx


def _agent(ctx):
    session = ctx.sessions.create(cwd=None)
    agent = ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))
    return session, agent


def _goal_msg(text, goal_id, revision, round_):
    return create_user_message(
        [TextBlock(text)],
        source=MessageSource("goal", goalId=goal_id, revision=revision, round=round_),
    )


def _user_msg(text):
    return create_user_message([TextBlock(text)], source=MessageSource("user"))


def _open_turn(session):
    session.append("turn/start", {"reason": "test"})


# --------------------------------------------------------------------------- #
# 纯函数域
# --------------------------------------------------------------------------- #
def test_decode_goal_change_strict():
    good = {
        "kind": "goal/change", "version": 1, "operation": "create",
        "goal": {"id": "g1", "revision": 1, "objective": "build", "phase": "active", "maxGoalRounds": 5},
        "roundsStarted": 0, "createdAt": 100, "updatedAt": 100,
    }
    change = decode_goal_change(good)
    assert change is not None and change["operation"] == "create"
    assert change["goal"]["id"] == "g1" and isinstance(change["goal"]["id"], gf.GoalId)
    # 无关值 → None
    assert decode_goal_change({"kind": "other"}) is None
    assert decode_goal_change(None) is None
    # 畸形拒绝
    bad = dict(good)
    bad["version"] = 2
    try:
        decode_goal_change(bad)
    except GoalError:
        pass
    else:
        raise AssertionError("版本不符应拒绝")
    bad2 = dict(good)
    bad2["goal"] = dict(good["goal"])
    bad2["goal"]["phase"] = "flying"
    try:
        decode_goal_change(bad2)
    except GoalError:
        pass
    else:
        raise AssertionError("非法 phase 应拒绝")
    # blocked phase 必须有 blockedReason
    bad3 = dict(good)
    bad3["operation"] = "block"
    bad3["goal"] = dict(good["goal"])
    bad3["goal"]["phase"] = "blocked"
    try:
        decode_goal_change(bad3)
    except GoalError:
        pass
    else:
        raise AssertionError("blocked 缺 blockedReason 应拒绝")


def test_fold_create_edit_complete_clear():
    state = empty_goal_fold_state()
    mk = lambda op, goal, **kw: {  # noqa: E731
        "kind": "goal/change", "version": 1, "operation": op, "goal": goal,
        "roundsStarted": kw.get("roundsStarted", 0),
        "createdAt": kw.get("createdAt", 100), "updatedAt": kw.get("updatedAt", 100),
    }
    g1 = {"id": "g1", "revision": 1, "objective": "build", "phase": "active", "maxGoalRounds": 3}
    state2 = empty_goal_fold_state()
    apply_goal_change(state2, mk("create", g1))
    assert state2["goal"]["id"] == "g1" and state2["roundsStarted"] == 0

    # edit：同一 id/revision+1，phase 不变
    g2 = dict(g1)
    g2["revision"] = 2
    g2["objective"] = "build v2"
    apply_goal_change(state2, mk("edit", g2, roundsStarted=0, updatedAt=150))
    assert state2["goal"]["objective"] == "build v2"

    # pause 只能从 active → paused
    g3 = dict(g2)
    g3["revision"] = 3
    g3["phase"] = "paused"
    apply_goal_change(state2, mk("pause", g3, roundsStarted=0, updatedAt=200))

    # resume paused → active
    g4 = dict(g3)
    g4["revision"] = 4
    g4["phase"] = "active"
    apply_goal_change(state2, mk("resume", g4, roundsStarted=0, updatedAt=250))

    # complete active → complete
    g5 = dict(g4)
    g5["revision"] = 5
    g5["phase"] = "complete"
    apply_goal_change(state2, mk("complete", g5, roundsStarted=0, updatedAt=300))

    # clear：墓碑 revision+1
    apply_goal_change(state2, {
        "kind": "goal/change", "version": 1, "operation": "clear",
        "cleared": {"id": "g1", "revision": 6}, "clearedAt": 350,
    })
    assert state2["goal"] is None and state2["lastRef"]["revision"] == 6

    # 非法：create 只允许在无目标或 complete 之后
    try:
        apply_goal_change(empty_goal_fold_state() and state, mk("create", g1))
    except GoalError:
        pass
    # 非法：pause 不能从 paused
    s3 = empty_goal_fold_state()
    apply_goal_change(s3, mk("create", g1))
    p = dict(g1)
    p["revision"] = 2
    p["phase"] = "paused"
    apply_goal_change(s3, mk("pause", p, updatedAt=150))
    p2 = dict(p)
    p2["revision"] = 3
    p2["phase"] = "paused"
    try:
        apply_goal_change(s3, mk("pause", p2, updatedAt=200))
    except GoalError:
        pass
    else:
        raise AssertionError("paused→paused 应拒绝")


def test_goal_round_advances_counter():
    state = empty_goal_fold_state()
    apply_goal_change(state, {
        "kind": "goal/change", "version": 1, "operation": "create",
        "goal": {"id": "g1", "revision": 1, "objective": "x", "phase": "active", "maxGoalRounds": 3},
        "roundsStarted": 0, "createdAt": 1, "updatedAt": 1,
    })
    from types import SimpleNamespace

    def ev(round_):
        return SimpleNamespace(
            type="user/message", seq=2, data=SimpleNamespace(
                source=MessageSource("goal", goalId="g1", revision=1, round=round_),
            ),
        )
    gf.apply_goal_event(state, ev(1))
    assert state["roundsStarted"] == 1
    gf.apply_goal_event(state, ev(2))
    assert state["roundsStarted"] == 2
    # 跳号拒绝
    try:
        gf.apply_goal_event(state, ev(4))
    except GoalError:
        pass
    else:
        raise AssertionError("跳号 goal round 应拒绝")
    # 非法 goalId/revision 拒绝
    bad = SimpleNamespace(
        type="user/message", seq=9, data=SimpleNamespace(
            source=MessageSource("goal", goalId="other", revision=1, round=1),
        ),
    )
    try:
        gf.apply_goal_event(state, bad)
    except GoalError:
        pass
    else:
        raise AssertionError("非当前目标 round 应拒绝")


def test_goal_projection_last_wins():
    from types import SimpleNamespace
    state = None
    create = {
        "kind": "goal/change", "version": 1, "operation": "create",
        "goal": {"id": "g1", "revision": 1, "objective": "x", "phase": "active", "maxGoalRounds": 3},
        "roundsStarted": 0, "createdAt": 1, "updatedAt": 1,
    }
    state = gf.apply_goal_projection(state, SimpleNamespace(type="goal/change", data=create))
    assert state is not None and state["goal"]["id"] == "g1"
    # 无关事件返回同一引用
    same = gf.apply_goal_projection(state, SimpleNamespace(type="user/message", data=None))
    assert same is state
    # clear → null
    cleared = gf.apply_goal_projection(state, SimpleNamespace(type="goal/change", data={
        "kind": "goal/change", "version": 1, "operation": "clear",
        "cleared": {"id": "g1", "revision": 2}, "clearedAt": 9,
    }))
    assert cleared is None


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #
async def test_service_lifecycle_and_cas():
    ctx = _ctx()
    changes = []
    ctx.on("goal/changed", lambda payload: changes.append(payload["change"]["operation"]))
    session, agent = _agent(ctx)

    assert ctx.goals.get(agent) is None  # 初始无目标
    view = ctx.goals.create(agent, {"objective": "写报告"})
    assert view["phase"] == "active" and view["activation"] == "armed"
    assert view["revision"] == 1 and view["roundsStarted"] == 0
    gid, rev = view["id"], view["revision"]

    # 重复 create 拒绝（非 complete）
    try:
        ctx.goals.create(agent, {"objective": "again"})
    except GoalError as e:
        assert e.code == "GOAL_ALREADY_EXISTS"
    else:
        raise AssertionError("重复 create 应拒绝")

    # 陈旧修订拒绝
    try:
        ctx.goals.edit(agent, {"id": gid, "revision": 99}, {"objective": "x"})
    except GoalError as e:
        assert e.code == "GOAL_STALE_REVISION"
    else:
        raise AssertionError("陈旧 ref 应拒绝")

    v2 = ctx.goals.edit(agent, {"id": gid, "revision": rev}, {"objective": "写季度报告"})
    assert v2["revision"] == 2 and v2["objective"] == "写季度报告"
    v3 = ctx.goals.pause(agent, {"id": gid, "revision": 2})
    assert v3["phase"] == "paused" and v3["activation"] == "disarmed"
    v4 = ctx.goals.resume(agent, {"id": gid, "revision": 3})
    assert v4["phase"] == "active" and v4["activation"] == "armed"
    v5 = ctx.goals.complete(agent, {"id": gid, "revision": 4})
    assert v5["phase"] == "complete"
    tombstone = ctx.goals.clear(agent, {"id": gid, "revision": 5})
    assert tombstone["revision"] == 6
    assert ctx.goals.get(agent) is None

    # goal/changed 通知序列
    assert changes == ["create", "edit", "pause", "resume", "complete", "clear"]

    # 日志事件与折叠一致（重放；goal 为可选键，clear 后缺席）
    folded = gf.fold_goal(session.events)
    assert folded.get("goal") is None and folded["lastRef"]["revision"] == 6


async def test_service_activation_and_budget():
    ctx = _ctx()
    session, agent = _agent(ctx)
    view = ctx.goals.create(agent, {"objective": "x", "maxGoalRounds": 2})
    gid = view["id"]
    # 模拟 goal round 推进到预算上限
    session.append("user/message", _goal_msg("r1", gid, 1, 1))
    session.append("user/message", _goal_msg("r2", gid, 1, 2))
    # resume 因预算耗尽拒绝
    ctx.goals.pause(agent, {"id": gid, "revision": 1})
    try:
        ctx.goals.resume(agent, {"id": gid, "revision": 2})
    except GoalError as e:
        assert e.code == "GOAL_INVALID_TRANSITION" and "exhausted" in e.message
    else:
        raise AssertionError("预算耗尽 resume 应拒绝")
    # 非法目标 id 编辑拒绝
    try:
        ctx.goals.edit(agent, {"id": "nope", "revision": 1}, {"objective": "y"})
    except GoalError as e:
        assert e.code == "GOAL_STALE_REVISION"
    else:
        raise AssertionError("目标不存在应报 STALE")


async def test_service_remote_invoke():
    ctx = _ctx()
    from dsh_py.services.typert import apply as typert_apply
    typert_apply(ctx)
    goal_service.apply(ctx)  # 再 apply 会重复注册？typert 注册表同 scope 覆盖——先测一次性
    ctx2 = AppContext()
    load_profile(ctx2, CORE_PROFILE)
    typert_apply(ctx2)
    goal_service.apply(ctx2)
    _, agent = _agent(ctx2)
    from dsh_py.services import typert as T
    result = await ctx2.typertRegistry.invoke(T.InvocationDescriptor(
        id="r1", service="goals", method="create",
        args={"agent": agent, "request": {"objective": "远程目标"}},
    ))
    assert result.ok is True
    ref = result.value["ref"]
    assert ref["id"].startswith("goal-") and ref["revision"] == 1
    # 远程 edit
    result2 = await ctx2.typertRegistry.invoke(T.InvocationDescriptor(
        id="r2", service="goals", method="edit",
        args={"agent": agent, "ref": ref, "request": {"objective": "改过的"}},
    ))
    assert result2.ok is True and result2.value["objective"] == "改过的"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
async def test_tool_get_and_create_authority():
    ctx = _ctx()
    session, agent = _agent(ctx)
    # 开放回合内：无目标 get_goal → goal:null
    _open_turn(session)
    text, is_error, _ = await ctx.tools.execute_with_agent("get_goal", "{}", agent=agent)
    assert is_error is False and json.loads(text)["goal"] is None

    # 无直接人类输入：create_goal 拒绝
    text, is_error, _ = await ctx.tools.execute_with_agent(
        "create_goal", '{"objective": "写代码"}', agent=agent)
    assert is_error is True and "direct human" in text

    # 直接人类输入：create_goal 通过
    session.append("user/message", _user_msg("请完成这个长期任务"))
    text, is_error, _ = await ctx.tools.execute_with_agent(
        "create_goal", '{"objective": "写代码"}', agent=agent)
    assert is_error is False
    value = json.loads(text)
    assert value["goal"]["phase"] == "active" and value["activation"] == "armed"


async def test_tool_update_authority_and_blocked_threshold():
    ctx = _ctx()  # 默认阈值 3
    session, agent = _agent(ctx)

    # 回合 1：直接人类 → create_goal 通过
    _open_turn(session)
    session.append("user/message", _user_msg("长期目标"))
    text, is_error, _ = await ctx.tools.execute_with_agent(
        "create_goal", '{"objective": "目标", "max_goal_rounds": 5}', agent=agent)
    assert is_error is False
    goal = json.loads(text)["goal"]
    gid, rev = goal["id"], goal["revision"]

    # 回合 2（无人类输入）：edit 权威拒绝
    session.append("turn/end", {"reason": "step-done"})
    _open_turn(session)
    text, is_error, _ = await ctx.tools.execute_with_agent(
        "update_goal", json.dumps({"goal_id": gid, "revision": rev, "action": "edit", "objective": "改"}),
        agent=agent)
    assert is_error is True and "direct human" in text

    # 回合 3：直接人类 → pause 通过
    session.append("turn/end", {"reason": "step-done"})
    _open_turn(session)
    session.append("user/message", _user_msg("先暂停"))
    text, is_error, _ = await ctx.tools.execute_with_agent(
        "update_goal", json.dumps({"goal_id": gid, "revision": rev, "action": "pause"}), agent=agent)
    assert is_error is False and json.loads(text)["goal"]["phase"] == "paused"
    rev += 1

    # 回合 4：直接人类 → resume 回 active
    session.append("turn/end", {"reason": "step-done"})
    _open_turn(session)
    session.append("user/message", _user_msg("继续"))
    text, is_error, _ = await ctx.tools.execute_with_agent(
        "update_goal", json.dumps({"goal_id": gid, "revision": rev, "action": "resume"}), agent=agent)
    assert is_error is False
    rev += 1

    # 回合 5：goal round（无人类）授权下 complete 通过
    session.append("turn/end", {"reason": "step-done"})
    _open_turn(session)
    session.append("user/message", _goal_msg("r1", gid, rev, 1))
    text, is_error, _ = await ctx.tools.execute_with_agent(
        "update_goal", json.dumps({"goal_id": gid, "revision": rev, "action": "complete"}), agent=agent)
    assert is_error is False and json.loads(text)["goal"]["phase"] == "complete"

    # 回合 6：直接人类 → 创建目标 B
    session.append("turn/end", {"reason": "step-done"})
    _open_turn(session)
    session.append("user/message", _user_msg("换一个"))
    text, is_error, _ = await ctx.tools.execute_with_agent(
        "create_goal", '{"objective": "目标B"}', agent=agent)
    assert is_error is False
    goal2 = json.loads(text)["goal"]
    gid2, rev2 = goal2["id"], goal2["revision"]

    # 回合 7：goal round（round=1 < 阈值 3）→ blocked 阈值拒绝
    session.append("turn/end", {"reason": "step-done"})
    _open_turn(session)
    session.append("user/message", _goal_msg("b1", gid2, rev2, 1))
    text, is_error, _ = await ctx.tools.execute_with_agent(
        "update_goal", json.dumps({"goal_id": gid2, "revision": rev2, "action": "blocked", "blocked_reason": "卡住了"}),
        agent=agent)
    assert is_error is True and "consecutive goal rounds" in text


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
async def test_command_goal_grammar():
    ctx = _ctx()
    session, agent = _agent(ctx)

    async def run(raw):
        return await ctx.commands.invoke("goal", agent, raw_input=raw)

    # show：无目标
    r = await run("")
    assert r.kind == "success" and "No goal is currently set" in r.text
    # create
    r = await run("写周报")
    assert r.kind == "success" and "Goal created" in r.text
    assert "Objective: 写周报" in r.text
    # 重复 create 拒绝
    r = await run("再写一个")
    assert r.kind == "error"
    # edit
    r = await run("edit 写月度周报")
    assert r.kind == "success" and "Goal updated" in r.text and "月度周报" in r.text
    # invalid-edit（缺目标文本）
    r = await run("edit")
    assert r.kind == "error" and "replacement objective" in r.text
    # pause / resume
    r = await run("pause")
    assert r.kind == "success" and "paused" in r.text
    r = await run("resume")
    assert r.kind == "success" and "resumed" in r.text
    # show 显示状态
    r = await run("")
    assert "Rounds: 0/256" in r.text
    # clear
    r = await run("clear")
    assert r.kind == "success" and "Goal cleared" in r.text
    r = await run("")
    assert "No goal is currently set" in r.text
    # pause 无目标 → missing
    r = await run("pause")
    assert r.kind == "error" and "requires one" in r.text


def _run_all():
    fails = 0
    sync_tests = [
        test_decode_goal_change_strict,
        test_fold_create_edit_complete_clear,
        test_goal_round_advances_counter,
        test_goal_projection_last_wins,
    ]
    async_tests = [
        test_service_lifecycle_and_cas,
        test_service_activation_and_budget,
        test_service_remote_invoke,
        test_tool_get_and_create_authority,
        test_tool_update_authority_and_blocked_threshold,
        test_command_goal_grammar,
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

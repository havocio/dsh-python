"""治理三包集成验证（guard / hooks / schedule，第 3 层）。

运行：python dsh_py/tests/test_guard_hooks_schedule.py

覆盖：
- guard/timeout-policy：声明 timeout_ms 的工具到点被强制返回 TOOL_TIMEOUT 文本；
- guard/repeat-tool-reminder：连续相同参数调用命中阈值注入提醒；用户消息重置计数；
- hooks（通用桥）PreToolUse 否决 / PostToolUse 注入上下文（用伪 shell 隔离外部命令）；
- schedule 工具：create/list/delete 经耐用日志持久化（事务 + 折叠 + 持久性屏障）。
"""

import asyncio
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.message import MessageSource, TextBlock, create_user_message

import dsh_py.plugins.guard_timeout as guard_timeout
import dsh_py.plugins.guard_repeat_tool as guard_repeat_tool
import dsh_py.plugins.hooks as hooks
from dsh_py.services import schedule as schedule_mod


def _ctx():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    return ctx


async def test_timeout_policy_forces_deadline():
    ctx = _ctx()
    guard_timeout.apply(ctx)

    ran_to_completion = []
    async def slow(args):
        await asyncio.sleep(0.5)
        ran_to_completion.append(1)
        return "done", False

    ctx.tools.register("slow", "慢工具", {"type": "object", "properties": {}}, slow, timeout_ms=80)

    text, is_error, additional = await ctx.tools.execute_with_agent("slow", "{}")
    assert is_error is True
    assert "timed out" in text
    # 内层应被取消，未跑到完成
    await asyncio.sleep(0.6)
    assert ran_to_completion == []


async def test_timeout_policy_passthrough_without_budget():
    ctx = _ctx()
    guard_timeout.apply(ctx)
    seen = []
    async def fast(args):
        seen.append(1)
        return "ok", False
    ctx.tools.register("fast", "快工具", {"type": "object", "properties": {}}, fast)  # 无 timeout_ms
    text, is_error = await ctx.tools.execute("fast", "{}")
    assert is_error is False and text == "ok"
    assert seen == [1]


async def test_repeat_reminder_threshold_and_reset():
    ctx = _ctx()
    # 加载器会先用 apply.Config 校验并填充默认值；此处模拟同样的约定
    guard_repeat_tool.apply(ctx, guard_repeat_tool.Config.validate({}))

    agent = object()  # 伪 agent，仅用于 id 键控连续重复链
    async def handler(args):
        return "ok", False
    ctx.tools.register(
        "probe", "探测",
        {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        handler,
    )

    results = []
    for _ in range(3):
        _, _, additional = await ctx.tools.execute_with_agent("probe", '{"x":"same"}', agent=agent)
        results.append(additional)

    # 前两次无提醒；第 3 次命中首个阈值（默认 thresholds[0]=3）
    assert results[0] == [] and results[1] == []
    assert len(results[2]) == 1
    msg = results[2][0]
    assert msg.source.kind == "plugin" and msg.source.plugin == "repeat-tool-reminder"
    assert "repeating" in msg.content[0].text.lower() or "repeated" in msg.content[0].text.lower()

    # 用户消息重置计数（agent/pre-step 是瀑布流事件，需用 waterfall 触发）
    async def _noop_decision():
        return {"kind": "enter", "messages": []}
    await ctx.waterfall(
        "agent/pre-step",
        {"agent": agent, "messages": [create_user_message([TextBlock("hi")], MessageSource("user"))]},
        inner=_noop_decision,
    )
    _, _, additional_after_reset = await ctx.tools.execute_with_agent("probe", '{"x":"same"}', agent=agent)
    assert additional_after_reset == []


async def test_hooks_pre_block_and_post_context():
    ctx = _ctx()

    class FakeShell:
        def __init__(self):
            self.requests = []
        async def run(self, request):
            self.requests.append(request)
            cmd = request.get("command", "")
            if "BLOCK" in cmd:
                return {"stdout": '{"hookEventName":"PreToolUse","decision":"block","reason":"nope"}',
                        "stderr": "", "exit_code": 2}
            if "CTX" in cmd:
                return {"stdout": '{"hookEventName":"PostToolUse","additionalContext":"remember X"}',
                        "stderr": "", "exit_code": 0}
            return {"stdout": "", "stderr": "", "exit_code": 0}

    ctx.shell = FakeShell()

    hooks.apply(ctx, {
        "hooks": [
            {"point": "PreToolUse", "matcher": "probe", "hooks": [{"command": "BLOCK"}]},
            {"point": "PostToolUse", "matcher": "probe", "hooks": [{"command": "CTX"}]},
        ],
        "points": ["PreToolUse", "PostToolUse"],
    })

    async def handler(args):
        return "ok", False
    ctx.tools.register("probe", "探测", {"type": "object", "properties": {}}, handler)

    # 钩子事件需写入会话日志，提供伪 agent + 真实 session
    session = ctx.sessions.prepare(cwd=None)
    agent = SimpleNamespace(session=session, id=session.header.id)

    # PreToolUse 命中 block → 工具被拒绝，返回错误文本
    text, is_error, _ = await ctx.tools.execute_with_agent("probe", "{}", agent=agent)
    assert is_error is True
    assert "blocked" in text

    # PostToolUse 命中 → 注入 additionalContext
    _, _, additional = await ctx.tools.execute_with_agent("probe", "{}", agent=agent)
    texts = [(m.content[0].text if m.content else "") for m in additional]
    assert any("remember X" in t for t in texts)

    # 应留下 hook/invoked 与 hook/result 耐久事件
    invoked = [e for e in session.events if e.type == "hook/invoked"]
    result = [e for e in session.events if e.type == "hook/result"]
    assert len(invoked) >= 2 and len(result) >= 2


async def test_schedule_tools_create_list_delete():
    ctx = _ctx()
    schedule_mod.register_schedule_tools(ctx)

    session = ctx.sessions.prepare(cwd=None)
    agent = SimpleNamespace(session=session, id=session.header.id)

    # create（after）
    out, _, _ = await ctx.tools.execute_with_agent(
        "schedule_create", json.dumps({"prompt": "喝水", "after_seconds": 3600}), agent=agent)
    created = json.loads(out)
    assert "id" in created and "scheduledAt" in created
    assert created["kind"] == "after"
    sid = created["id"]

    # 持久化：会话事件含一条 schedule/change create
    creates = [e for e in session.events if e.type == "schedule/change"
               and e.data.get("operation") == "create"]
    assert len(creates) == 1

    # list 应能列出
    out, _, _ = await ctx.tools.execute_with_agent("schedule_list", "{}", agent=agent)
    listed = json.loads(out)
    assert any(r["id"] == sid for r in listed)
    assert listed[0]["state"] in ("scheduled", "overdue")

    # delete 应成功并再落一条 dispatch/delete
    out, _, _ = await ctx.tools.execute_with_agent(
        "schedule_delete", json.dumps({"id": sid}), agent=agent)
    deleted = json.loads(out)
    assert deleted.get("deleted") is True

    # 删除后再 list 为空
    out, _, _ = await ctx.tools.execute_with_agent("schedule_list", "{}", agent=agent)
    assert json.loads(out) == []


async def test_schedule_create_validation_rejects_ambiguous_selector():
    ctx = _ctx()
    schedule_mod.register_schedule_tools(ctx)
    session = ctx.sessions.prepare(cwd=None)
    agent = SimpleNamespace(session=session, id=session.header.id)

    out, _, _ = await ctx.tools.execute_with_agent(
        "schedule_create", json.dumps({"prompt": "x", "after_seconds": 10, "every_seconds": 600}),
        agent=agent)
    err = json.loads(out)
    assert err.get("code") == "invalid_selector"

    # 间隔过短
    out, _, _ = await ctx.tools.execute_with_agent(
        "schedule_create", json.dumps({"prompt": "x", "every_seconds": 100}), agent=agent)
    assert json.loads(out).get("code") == "frequency_too_high"


async def test_schedule_runtime_dispatches_due_reminder():
    ctx = _ctx()
    session = ctx.sessions.prepare(cwd=None)
    # 直接写入一条已过期的「一次性」schedule/change create
    session.append("schedule/change", {
        "version": 1, "operation": "create",
        "schedule": {"id": "s1", "kind": "at", "prompt": "提醒喝水",
                      "scheduledAt": "2000-01-01T00:00:00.000Z"},
    })

    dispatched = []

    class FakeAgent:
        def __init__(self, s):
            self.session = s
            self.id = s.header.id
        def followup(self, message):
            dispatched.append(message)  # 收纳注入的提醒，避免触发真实 agent 循环
        def run_maintenance(self, job):
            return job(SimpleNamespace(aborted=False))

    agent = FakeAgent(session)
    runtime = schedule_mod.ScheduleRuntime(ctx, agent)
    await runtime._drive_once()

    # 应追加一条 dispatch 事件（宣布已投递），且提醒消息被注入
    dispatches = [e for e in session.events
                  if e.type == "schedule/change" and e.data.get("operation") == "dispatch"]
    assert len(dispatches) == 1
    assert dispatches[0].data["id"] == "s1"
    assert len(dispatched) == 1
    assert "提醒喝水" in dispatched[0].content[0].text
    # dispatch 后该一次性提醒从活跃集合中移除（折叠验证）
    folded = schedule_mod.fold_schedule_events(session.events, session.header.seed_length)
    assert [r.id for r in folded.active] == []


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for fn in fns:
        try:
            asyncio.run(fn())
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    if fails:
        raise SystemExit(f"\n{fails} 项失败")
    print(f"\nOK: guard/hooks/schedule 集成测试通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()

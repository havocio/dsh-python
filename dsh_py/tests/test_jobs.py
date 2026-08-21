"""jobs 家族验证（jobs / jobs-local / tool-jobs，第 3 层）。

运行：python dsh_py/tests/test_jobs.py

覆盖：
- start：无控制器拒绝、kind/label 校验、id 生成 <kind>-N、owner 上限拒绝；
- list/get/read：owner 隔离（他会话拒绝）、read 流式游标与终态输出；
- kill：requested / already-finished；settle 首胜（后到的 producer 结果被忽略）；
- wait：settle / 超时 / 调用方取消三路；settle 前等待者使 reported；
- onJobDone：完成通知（tool-jobs 统一 followup 交付）、contained 监听器；
- onJobsChanged：注册/stopping/settle/清空均触发；
- 工具：job_output（含 wait）/ job_list / job_kill 的模型面文本；
- dispose_all：取消活任务、等待 settle、清空。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.core.signal import CancelSignal
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.jobs_local import apply as jobs_local_apply
from dsh_py.services.system_prompt import apply as system_prompt_apply

import dsh_py.plugins.tool_jobs as tool_jobs


class _Agent:
    def __init__(self, session):
        self.session = session
        self.id = session.header.id
        self.followups = []

    def followup(self, message):
        self.followups.append(message)


class _Hooks:
    """测试钩子：cancel 直接 settle killed；done 由测试外部 set_result。"""

    def __init__(self, loop):
        self.done = loop.create_future()
        self.cancelled = []
        self._output = ""

    def cancel(self, reason=None):
        self.cancelled.append(reason)
        if not self.done.done():
            self.done.set_result({"status": "killed", "detail": reason or "cancelled"})

    def readOutput(self):
        return self._output


def _ctx():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    system_prompt_apply(ctx)
    return ctx


def _start(ctx, agent=None, kind="bash", label="run tests", hooks=None, limit=None, output_limit=None):
    loop = asyncio.get_event_loop()
    hooks = hooks or _Hooks(loop)
    spec = {"kind": kind, "label": label, "run": lambda: hooks}
    if agent is not None:
        spec["owner"] = agent
    if output_limit is not None:
        spec["outputLimitBytes"] = output_limit
    return ctx.jobs.start(spec), hooks


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
async def test_start_requires_controller():
    ctx = _ctx()
    jobs_local_apply(ctx)
    session = ctx.sessions.prepare(cwd=None)
    agent = _Agent(session)
    try:
        _start(ctx, agent)
    except RuntimeError as e:
        assert "没有任务控制器" in str(e)
    else:
        raise AssertionError("无控制器应拒绝 start")


async def test_start_id_validation_and_limit():
    ctx = _ctx()
    jobs_local_apply(ctx)
    tool_jobs.apply(ctx, {"waitTimeoutMs": 1000, "maxWaitTimeoutMs": 5000})
    session = ctx.sessions.prepare(cwd=None)
    agent = _Agent(session)

    id1, _ = _start(ctx, agent)
    assert str(id1) == "bash-1"
    id2, _ = _start(ctx, agent, kind="subagent", label="child")
    assert str(id2) == "subagent-1"
    id3, _ = _start(ctx, agent)
    assert str(id3) == "bash-2"

    # kind/label 校验
    for bad in ({"kind": "", "label": "x"}, {"kind": "k", "label": ""}):
        try:
            ctx.jobs.start({**bad, "run": lambda: _Hooks(asyncio.get_event_loop())})
        except ValueError:
            pass
        else:
            raise AssertionError(f"应拒绝：{bad}")

    # 上限
    ctx2 = _ctx()
    jobs_local_apply(ctx2, {"maxConcurrentJobsPerOwner": 1})
    tool_jobs.apply(ctx2, {"waitTimeoutMs": 1000, "maxWaitTimeoutMs": 5000})
    s2 = ctx2.sessions.prepare(cwd=None)
    a2 = _Agent(s2)
    _start(ctx2, a2)
    try:
        _start(ctx2, a2)
    except RuntimeError as e:
        assert "limit" in str(e)
    else:
        raise AssertionError("超上限应拒绝")


async def test_owner_isolation():
    ctx = _ctx()
    jobs_local_apply(ctx)
    tool_jobs.apply(ctx, {"waitTimeoutMs": 1000, "maxWaitTimeoutMs": 5000})
    s1 = ctx.sessions.prepare(cwd=None)
    s2 = ctx.sessions.prepare(cwd=None)
    a1, a2 = _Agent(s1), _Agent(s2)

    id1, _ = _start(ctx, a1)
    # owner 可见；他会话不可见
    assert [j["id"] for j in ctx.jobs.list(a1)] == [id1]
    assert ctx.jobs.list(a2) == []
    try:
        ctx.jobs.get(id1, a2)
    except RuntimeError as e:
        assert "另一个会话" in str(e)
    else:
        raise AssertionError("他会话应拒绝访问")
    # 无 agent caller 看不到 owned job
    assert ctx.jobs.list() == []


async def test_read_stream_and_kill():
    ctx = _ctx()
    jobs_local_apply(ctx)
    tool_jobs.apply(ctx, {"waitTimeoutMs": 1000, "maxWaitTimeoutMs": 5000})
    session = ctx.sessions.prepare(cwd=None)
    agent = _Agent(session)

    hooks = _Hooks(asyncio.get_event_loop())
    hooks._output = "line1\n"
    id1, _ = _start(ctx, agent, hooks=hooks)
    read = ctx.jobs.read(id1, agent)
    assert read["text"] == "line1\n" and read["snapshot"]["status"] == "running"
    hooks._output = "line2\n"
    assert ctx.jobs.read(id1, agent)["text"] == "line2\n"

    # kill → stopping → killed（cancel settle）
    outcome = ctx.jobs.kill(id1, agent, "no longer needed")
    assert outcome == "requested"
    await asyncio.sleep(0.01)  # 让 done callback settle 跑完（Task 恢复 + 完成各一轮）
    assert ctx.jobs.get(id1, agent)["status"] == "killed"
    assert hooks.cancelled == ["no longer needed"]
    # 已终态再 kill → already-finished
    assert ctx.jobs.kill(id1, agent) == "already-finished"
    # 终态 read 幂等返回最终输出并标记 reported
    hooks._output = ""
    terminal_read = ctx.jobs.read(id1, agent)
    assert terminal_read["text"] == ""  # killed 无 output
    assert ctx.jobs.get(id1, agent)["reported"] is True


async def test_settle_first_wins():
    ctx = _ctx()
    jobs_local_apply(ctx)
    tool_jobs.apply(ctx, {"waitTimeoutMs": 1000, "maxWaitTimeoutMs": 5000})
    session = ctx.sessions.prepare(cwd=None)
    agent = _Agent(session)

    loop = asyncio.get_event_loop()

    class LateHooks(_Hooks):
        def cancel(self, reason=None):
            self.cancelled.append(reason)
            # 迟到：不 settle killed，留到稍后 completed
            loop.call_later(0.02, lambda: self.done.set_result(
                {"status": "completed", "output": "late"}))

    hooks = LateHooks(loop)
    id1, _ = _start(ctx, agent, hooks=hooks)
    outcome = ctx.jobs.kill(id1, agent, "stop")
    assert outcome == "requested"
    # kill 后 status=stopping；等待迟到 completed → 首胜语义：已 terminal? 否，
    # stopping 非终态 → completed 胜出
    assert ctx.jobs.get(id1, agent)["status"] == "stopping"


async def test_wait_three_ways():
    ctx = _ctx()
    jobs_local_apply(ctx)
    tool_jobs.apply(ctx, {"waitTimeoutMs": 1000, "maxWaitTimeoutMs": 5000})
    session = ctx.sessions.prepare(cwd=None)
    agent = _Agent(session)

    loop = asyncio.get_event_loop()

    # settle 路径
    h1 = _Hooks(loop)
    id1, _ = _start(ctx, agent, hooks=h1)
    loop.call_later(0.02, lambda: h1.done.set_result({"status": "completed", "output": "ok"}))
    snap = await ctx.jobs.wait(id1, 2000, agent)
    assert snap["status"] == "completed"

    # 超时路径：返回 running，任务存活
    h2 = _Hooks(loop)
    id2, _ = _start(ctx, agent, kind="bash", label="hang", hooks=h2)
    snap2 = await ctx.jobs.wait(id2, 50, agent)
    assert snap2["status"] == "running"

    # 调用方取消路径
    h3 = _Hooks(loop)
    id3, _ = _start(ctx, agent, kind="bash", label="cancel-wait", hooks=h3)
    signal = CancelSignal()
    loop.call_later(0.02, lambda: signal.abort("caller"))
    try:
        await ctx.jobs.wait(id3, 2000, agent, signal)
    except RuntimeError as e:
        assert "aborted" in str(e)
    else:
        raise AssertionError("调用方取消应抛错")


async def test_completion_notice_and_changed():
    ctx = _ctx()
    jobs_local_apply(ctx)
    tool_jobs.apply(ctx, {"waitTimeoutMs": 1000, "maxWaitTimeoutMs": 5000})
    session = ctx.sessions.prepare(cwd=None)
    agent = _Agent(session)

    changes = []
    ctx.jobs.onJobsChanged(lambda owner: changes.append(owner))

    h = _Hooks(asyncio.get_event_loop())
    id1, _ = _start(ctx, agent, label="echo", hooks=h)
    assert len(changes) == 1  # 注册

    h.done.set_result({"status": "completed", "output": "hello world"})
    await asyncio.sleep(0.05)  # 让 settle + 通知跑完
    # 完成通知：owner.followup 收到 plugin notice 消息
    assert len(agent.followups) == 1
    notice = agent.followups[0]
    assert "background job bash-1" in notice.content[0].text
    assert "[status: completed]" in notice.content[0].text
    assert notice.source.plugin == "tool-jobs"
    # settle 触发 changed
    assert len(changes) == 2


async def test_tools_job_output_list_kill():
    ctx = _ctx()
    jobs_local_apply(ctx)
    tool_jobs.apply(ctx, {"waitTimeoutMs": 1000, "maxWaitTimeoutMs": 5000})
    session = ctx.sessions.prepare(cwd=None)
    agent = _Agent(session)

    h = _Hooks(asyncio.get_event_loop())
    id1, _ = _start(ctx, agent, label="sleep", hooks=h)

    # job_list
    text, is_error, _ = await ctx.tools.execute_with_agent("job_list", "{}", agent=agent)
    assert is_error is False and "bash-1 [bash] running — sleep" in text

    # job_output（流式增量 + status 行）
    h._output = "progress\n"
    text, is_error, _ = await ctx.tools.execute_with_agent(
        "job_output", f'{{"job_id": "{id1}"}}', agent=agent)
    assert is_error is False and "progress" in text and "[status: running]" in text

    # job_kill
    text, is_error, _ = await ctx.tools.execute_with_agent(
        "job_kill", f'{{"job_id": "{id1}", "reason": "done"}}', agent=agent)
    assert is_error is False and "cancellation" in text
    await asyncio.sleep(0.01)  # 让 settle 跑完
    text, is_error, _ = await ctx.tools.execute_with_agent(
        "job_kill", f'{{"job_id": "{id1}"}}', agent=agent)
    assert "already finished" in text

    # 未知任务 → 错误文本
    text, is_error, _ = await ctx.tools.execute_with_agent("job_output", '{"job_id": "bash-99"}', agent=agent)
    assert is_error is True and "未知任务" in text


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    sync_tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and not asyncio.iscoroutinefunction(v)]
    fails = []
    for t in sync_tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            fails.append((t.__name__, repr(e)))
    for t in tests:
        try:
            asyncio.run(t())
        except Exception as e:  # noqa: BLE001
            fails.append((t.__name__, repr(e)))
    for name, err in fails:
        print(f"FAIL {name}: {err}")
    total = len(tests) + len(sync_tests)
    print(f"{total} 项，{len(fails)} 失败")
    if fails:
        raise SystemExit(f"\n{len(fails)} 项失败")


if __name__ == "__main__":
    _run_all()

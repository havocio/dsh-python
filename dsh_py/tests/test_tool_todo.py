"""todo/tool-todo 集成验证（第 3 层）。

运行：python dsh_py/tests/test_tool_todo.py

覆盖：
- 合法整表写入 → 追加 todo/write 事件 + 返回 pending/in_progress/completed 计数文本；
- 约束兜底（dsh_py schema 不强制 enum / additionalProperties）：content 空 / 重复 / 非法 status / 单活跃纪律；
- 非 agent 调用者被拒绝；
- 投影（缝齐备时）：todos 整值折叠 last-write-wins，turn/start 重置为 None。
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.projection import apply as projection_apply

import dsh_py.plugins.tool_todo as tool_todo


def _ctx():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    return ctx


class _Agent:
    def __init__(self, session):
        self.session = session
        self.id = session.header.id


async def _write(ctx, todos, allow_parallel=False, agent=None):
    tool_todo.apply(ctx, {"allowParallelInProgress": allow_parallel})
    return await ctx.tools.execute_with_agent(
        "todo_write", json.dumps({"todos": todos}), agent=agent,
    )


async def test_write_appends_event_and_reports_counts():
    ctx = _ctx()
    session = ctx.sessions.prepare(cwd=None)
    agent = _Agent(session)

    text, is_error, additional = await _write(ctx, [
        {"content": "读源码", "status": "completed"},
        {"content": "写测试", "status": "in_progress"},
        {"content": "跑通", "status": "pending"},
    ], agent=agent)

    assert is_error is False
    assert "1 pending" in text and "1 in progress" in text and "1 completed" in text
    assert additional == []
    writes = [e for e in session.events if e.type == "todo/write"]
    assert len(writes) == 1
    assert writes[0].data["todos"] == [
        {"content": "读源码", "status": "completed"},
        {"content": "写测试", "status": "in_progress"},
        {"content": "跑通", "status": "pending"},
    ]


async def test_whole_list_replaces_previous():
    ctx = _ctx()
    session = ctx.sessions.prepare(cwd=None)
    agent = _Agent(session)

    await _write(ctx, [{"content": "a", "status": "pending"}], agent=agent)
    await _write(ctx, [{"content": "b", "status": "completed"}], agent=agent)
    writes = [e for e in session.events if e.type == "todo/write"]
    assert len(writes) == 2
    # 视图应仅见最后一次整表（last-write-wins）
    assert writes[-1].data["todos"] == [{"content": "b", "status": "completed"}]


async def test_empty_or_duplicate_content_rejected():
    ctx = _ctx()
    session = ctx.sessions.prepare(cwd=None)
    agent = _Agent(session)

    _, err1, _ = await _write(ctx, [{"content": "  ", "status": "pending"}], agent=agent)
    assert err1 is True

    _, err2, _ = await _write(ctx, [
        {"content": "x", "status": "pending"},
        {"content": "x", "status": "completed"},
    ], agent=agent)
    assert err2 is True


async def test_invalid_status_rejected():
    ctx = _ctx()
    session = ctx.sessions.prepare(cwd=None)
    agent = _Agent(session)
    _, err, _ = await _write(ctx, [{"content": "x", "status": "blocked"}], agent=agent)
    assert err is True


async def test_single_active_discipline():
    ctx = _ctx()
    session = ctx.sessions.prepare(cwd=None)
    agent = _Agent(session)

    # 默认（allowParallel=False）：两个 in_progress 被拒
    _, err, _ = await _write(ctx, [
        {"content": "a", "status": "in_progress"},
        {"content": "b", "status": "in_progress"},
    ], allow_parallel=False, agent=agent)
    assert err is True

    # 允许并行：通过
    text, err2, _ = await _write(ctx, [
        {"content": "a", "status": "in_progress"},
        {"content": "b", "status": "in_progress"},
    ], allow_parallel=True, agent=agent)
    assert err2 is False and "2 in progress" in text


async def test_non_agent_caller_rejected():
    ctx = _ctx()
    text, is_error, _ = await _write(ctx, [{"content": "x", "status": "pending"}], agent=None)
    assert is_error is True
    assert "owning agent session" in text


async def test_projection_folds_and_resets():
    ctx = _ctx()
    projection_apply(ctx)  # 装配 sessionProjections 缝
    session = ctx.sessions.prepare(cwd=None)
    agent = _Agent(session)
    tool_todo.apply(ctx, {"allowParallelInProgress": False})

    # 首写前：null
    snap0 = ctx.sessionProjections.snapshot(session)
    assert snap0["values"]["todos"] is None

    await ctx.tools.execute_with_agent(
        "todo_write", json.dumps({"todos": [{"content": "a", "status": "in_progress"}]}),
        agent=agent,
    )
    assert ctx.sessionProjections.snapshot(session)["values"]["todos"] == [
        {"content": "a", "status": "in_progress"}
    ]

    # 整表替换（last-write-wins）
    await ctx.tools.execute_with_agent(
        "todo_write", json.dumps({"todos": [{"content": "b", "status": "completed"}]}),
        agent=agent,
    )
    assert ctx.sessionProjections.snapshot(session)["values"]["todos"] == [
        {"content": "b", "status": "completed"}
    ]

    # turn/start 重置为 null
    session.append("turn/start", {})
    assert ctx.sessionProjections.snapshot(session)["values"]["todos"] is None


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    fails = []
    for t in tests:
        try:
            asyncio.run(t())
        except Exception as e:  # noqa: BLE001
            fails.append((t.__name__, repr(e)))
    for name, err in fails:
        print(f"FAIL {name}: {err}")
    print(f"{len(tests)} 项，{len(fails)} 失败")
    if fails:
        raise SystemExit(f"\n{len(fails)} 项失败")


if __name__ == "__main__":
    _run_all()

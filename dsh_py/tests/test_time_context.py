"""context/time-context 验证（第 3 层）。

运行：python dsh_py/tests/test_time_context.py

覆盖：
- format_timestamp：ISO 形状（年-月-日T时:分:秒 + 偏移 + [Zone]）；
- format_duration：d/h/m/s 紧凑整秒；
- pre-step 注入：合格步骤注入 plugin snapshot 消息（含时间戳/耗时/浏览器时区策略）；
- 节流：refreshIntervalMs 内跳过；reject 决策不注入；
- 配置校验：无效 IANA 时区加载期抛错。
"""

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile

import dsh_py.plugins.time_context as time_context
from dsh_py.plugins.time_context import format_duration, format_timestamp


def _ctx(config=None):
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    time_context.apply(ctx, config or {})
    return ctx


class _Agent:
    def __init__(self, session):
        self.session = session


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
def test_format_timestamp_shape():
    now = 1_700_000_000_000  # 2023-11-14T22:13:20Z
    text = format_timestamp(now, "UTC")
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00\[UTC\]$", text)
    assert text.startswith("2023-11-14T22:13:20")
    # 非 UTC 时区偏移
    shanghai = format_timestamp(now, "Asia/Shanghai")
    assert re.match(r"^2023-11-15T\d{2}:\d{2}:\d{2}\+08:00\[Asia/Shanghai\]$", shanghai)


def test_format_duration():
    assert format_duration(0) == "0s"
    assert format_duration(59_000) == "59s"
    assert format_duration(61_000) == "1m 1s"
    assert format_duration(3_661_000) == "1h 1m 1s"
    assert format_duration(90_000_000) == "1d 1h 0s"  # dsh：零分钟不输出


def test_invalid_time_zone_fails_loud():
    try:
        _ctx({"timeZone": "Not/AZone"})
    except ValueError:
        pass
    else:
        raise AssertionError("无效 IANA 时区应加载期抛错")


# --------------------------------------------------------------------------- #
# pre-step 注入
# --------------------------------------------------------------------------- #
async def test_pre_step_injects_timestamp_message():
    ctx = _ctx({"timeZone": "UTC"})
    session = ctx.sessions.prepare(cwd=None)
    agent = _Agent(session)

    async def default_decision():
        return {"kind": "enter", "messages": []}

    decision = await ctx.waterfall(
        "agent/pre-step",
        {"agent": agent, "messages": [], "turn": 1, "step": 1, "signal": None},
        inner=default_decision,
    )
    assert decision["kind"] == "enter"
    assert len(decision["messages"]) == 1
    message = decision["messages"][0]
    text = message.content[0].text
    assert "Time sampled while preparing turn 1, step 1:" in text
    assert "+00:00[UTC]" in text
    assert "Elapsed since the preceding model-visible message: unavailable." in text
    assert message.source.kind == "plugin"
    assert message.source.plugin == "time-context"
    assert message.source.form == "snapshot"

    # 第二次 step：Elapsed 基于上一条 time-context 事件（模拟主循环把注入落盘）
    session.append("user/message", decision["messages"][0])
    await asyncio.sleep(0.01)
    decision2 = await ctx.waterfall(
        "agent/pre-step",
        {"agent": agent, "messages": [], "turn": 1, "step": 2, "signal": None},
        inner=default_decision,
    )
    text2 = decision2["messages"][0].content[0].text
    assert "turn 1, step 2" in text2
    assert "Elapsed since the preceding step context:" in text2
    assert "Elapsed since the preceding step context: unavailable." not in text2


async def test_refresh_interval_throttles():
    ctx = _ctx({"timeZone": "UTC", "refreshIntervalMs": 60_000})
    session = ctx.sessions.prepare(cwd=None)
    agent = _Agent(session)

    async def default_decision():
        return {"kind": "enter", "messages": []}

    # 第一次注入
    d1 = await ctx.waterfall(
        "agent/pre-step", {"agent": agent, "messages": [], "turn": 1, "step": 1, "signal": None},
        inner=default_decision,
    )
    assert len(d1["messages"]) == 1
    session.append("user/message", d1["messages"][0])  # 模拟主循环落盘
    # 间隔内再次 → 跳过（messages 保持原样）
    d2 = await ctx.waterfall(
        "agent/pre-step", {"agent": agent, "messages": [], "turn": 1, "step": 2, "signal": None},
        inner=default_decision,
    )
    assert len(d2["messages"]) == 0


async def test_reject_decision_passthrough():
    ctx = _ctx({"timeZone": "UTC"})
    session = ctx.sessions.prepare(cwd=None)
    agent = _Agent(session)

    async def rejecting():
        return {"kind": "reject"}

    decision = await ctx.waterfall(
        "agent/pre-step", {"agent": agent, "messages": [], "turn": 1, "step": 1, "signal": None},
        inner=rejecting,
    )
    assert decision == {"kind": "reject"}  # 原样透传，不注入


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

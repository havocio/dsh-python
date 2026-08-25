"""goal-round-driver 的验证（第 3 层，对标 dsh 的 goal-round-driver.spec.ts）。

运行：python dsh_py/tests/test_goal_round_driver.py

覆盖：续行提示渲染、armed 目标自动入队下一轮、回合上限 block（端到端）。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.agent import AgentOptions
from dsh_py.services.goal import apply as apply_goals
from dsh_py.services.goal_round_driver import apply as apply_driver
from dsh_py.services.goal_round_driver import render_goal_round_prompt
from dsh_py.services.llm import ChunkType, LlmAdapter, StreamChunk
from dsh_py.services.message import MessageSource


class MockDoneAdapter(LlmAdapter):
    """模型直接给出最终答复（无工具调用）→ turn 以 completed 结束。"""

    async def stream(self, options):
        yield StreamChunk(ChunkType.TEXT_DELTA, text="work done.")
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


def _setup():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    apply_goals(ctx, {})
    apply_driver(ctx, {})
    ctx.llm.register_adapter(["mock"], MockDoneAdapter())
    return ctx


def _agent(ctx):
    session = ctx.sessions.create()
    return ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))


async def test_render_goal_round_prompt():
    goal = {
        "id": "g1", "revision": 1, "objective": "audit the repo",
        "phase": "active", "maxGoalRounds": 3, "roundsStarted": 0,
        "activation": "armed",
    }
    blocks = render_goal_round_prompt(goal, 1)
    assert len(blocks) == 1
    text = blocks[0].text
    assert "Objective:" in text and "audit the repo" in text
    assert "Round: 1/3" in text
    assert "<goal_round>" in text


async def test_driver_blocks_at_round_limit():
    ctx = _setup()
    agent = _agent(ctx)
    ctx.goals.create(agent, {"objective": "finish the audit", "maxGoalRounds": 1})
    await agent.run("please start")
    await asyncio.sleep(0.1)  # 驱动串行任务 + followup 处理落地

    goal = ctx.goals.get(agent)
    assert goal is not None
    assert goal["roundsStarted"] == 1
    assert goal["phase"] == "blocked"
    assert goal["blockedReason"]["code"] == "round-limit"
    # 会话历史里应恰好有一条 goal 续行提示（round 1）
    goal_msgs = [
        ev for ev in agent.session.events
        if ev.type == "user/message" and getattr(ev.data, "source", None)
        and isinstance(ev.data.source, MessageSource) and ev.data.source.kind == "goal"
    ]
    assert len(goal_msgs) == 1
    assert goal_msgs[0].data.source.round == 1


async def test_driver_disarmed_no_queue():
    # 确定性：create 同步返回后、驱动任务运行前 disarm → 不得排入续行。
    ctx = _setup()
    agent = _agent(ctx)
    ctx.goals.create(agent, {"objective": "deep investigation", "maxGoalRounds": 50})
    ctx.goals.disarm(agent)
    await asyncio.sleep(0.1)  # 驱动串行任务落地
    queued = [
        m for m in agent.inbox.next_turn
        if getattr(m.source, "kind", None) == "goal" and (m.source.round or 0) > 0
    ]
    assert len(queued) == 0
    goal = ctx.goals.get(agent)
    assert goal["activation"] == "disarmed"


async def test_driver_no_goal_no_rounds():
    ctx = _setup()
    agent = _agent(ctx)
    await agent.run("ordinary request")
    await asyncio.sleep(0.05)
    goal_msgs = [
        ev for ev in agent.session.events
        if ev.type == "user/message" and getattr(ev.data, "source", None)
        and isinstance(ev.data.source, MessageSource) and ev.data.source.kind == "goal"
    ]
    assert len(goal_msgs) == 0


async def _main():
    tests = [
        test_render_goal_round_prompt,
        test_driver_blocks_at_round_limit,
        test_driver_disarmed_no_queue,
        test_driver_no_goal_no_rounds,
    ]
    failures = 0
    for test in tests:
        try:
            await test()
            print(f"  ✓ {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback

            traceback.print_exc()
            print(f"  ✗ {test.__name__}: {exc}")
    print(f"goal-round-driver: {'全部通过' if failures == 0 else f'{failures} 个失败'}")
    return failures


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(_main()) else 0)

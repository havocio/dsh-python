"""Agent 完整版（Inbox / cancel / 声明式 agents）的验证（第 2 层 Agent）。

运行：python dsh_py/tests/test_agent_extended.py
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.agent import AgentOptions, apply_loop
from dsh_py.services.llm import ChunkType, LlmAdapter, StreamChunk
from dsh_py.services.message import TextBlock, create_user_message
from dsh_py.services.session_persistence import apply as apply_persistence


class SlowCancelAdapter(LlmAdapter):
    """慢速流：每帧检查取消信号（对标 dsh 适配器尊重 AbortSignal）。"""

    async def stream(self, options):
        for i in range(10):
            await asyncio.sleep(0.02)
            if options.signal is not None:
                options.signal.throw_if_aborted()
            yield StreamChunk(ChunkType.TEXT_DELTA, text=f"t{i}")
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


class EchoAdapter(LlmAdapter):
    async def stream(self, options):
        yield StreamChunk(ChunkType.TEXT_DELTA, text="ok")
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


# --------------------------------------------------------------------------- #
# Inbox
# --------------------------------------------------------------------------- #
def test_inbox_queue_and_spliced_events():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    session = ctx.sessions.create()
    agent = ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))

    m1 = create_user_message([TextBlock("a")])
    m2 = create_user_message([TextBlock("b")])
    agent.inbox.append("next-turn", m1)
    agent.inbox.append("next-turn", m2)
    assert agent.inbox.has_pending
    assert len(agent.inbox.next_turn) == 2

    # 每次变更落一条 agent/inbox/spliced 事件（持久化可重放）
    spliced = [e for e in session.events if e.type == "agent/inbox/spliced"]
    assert len(spliced) == 2
    assert spliced[0].data["target"] == "next-turn" and len(spliced[0].data["inserted"]) == 1

    # claim：next-turn 模式取走 next-step 全部 + 队首
    claimed = agent.inbox.claim("next-turn", 1)
    assert [m.content[0].text for m in claimed] == ["a"]
    assert len(agent.inbox.next_turn) == 1

    # remove：持久化取消
    assert agent.inbox.remove(m2.id) is True
    assert not agent.inbox.has_pending


def test_inbox_persists_splices():
    """inbox 变更事件可经 session 持久化落盘（resume 后可重建）。"""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = AppContext()
        load_profile(ctx, [*CORE_PROFILE, apply_persistence])
        session = ctx.sessions.create()
        agent = ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))
        agent.inbox.append("next-turn", create_user_message([TextBlock("x")]))
        sid = session.header.id

        ctx2 = AppContext()
        load_profile(ctx2, [*CORE_PROFILE, apply_persistence])
        restored = ctx2.sessions.resume(sid)
        spliced = [e for e in restored.events if e.type == "agent/inbox/spliced"]
        assert len(spliced) == 1
        assert "x" in spliced[0].data["inserted"][0].content[0].text


# --------------------------------------------------------------------------- #
# cancel
# --------------------------------------------------------------------------- #
async def test_cancel_interrupts_turn():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    ctx.llm.register_adapter(["mock"], SlowCancelAdapter())
    session = ctx.sessions.create()
    agent = ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))

    task = asyncio.create_task(agent.run("hi"))
    await asyncio.sleep(0.05)          # 让流开始
    agent.cancel({"kind": "user"})     # 取消（对标 dsh 的 AgentCancelCause）
    await task

    turn_end = [e for e in session.events if e.type == "turn/end"][0]
    assert turn_end.data["reason"]["kind"] == "cancelled"
    assert turn_end.data["reason"]["reason"] == {"kind": "user"}
    # turn 边界仍然闭合（turn/start 在前）
    types = [e.type for e in session.events]
    assert types.index("turn/start") < types.index("turn/end")


async def test_cancel_before_start_ends_immediately():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    ctx.llm.register_adapter(["mock"], EchoAdapter())
    session = ctx.sessions.create()
    agent = ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))
    agent.cancel({"kind": "disposed"})  # 运行前已取消
    await agent.run("hi")
    # 无 turn 产生（取消在先，drain 直接退出）
    assert not any(e.type == "turn/start" for e in session.events)


# --------------------------------------------------------------------------- #
# 声明式 agents
# --------------------------------------------------------------------------- #
async def test_declarative_agents_with_fixed_session():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    ctx.llm.register_adapter(["mock"], EchoAdapter())
    apply_loop(ctx, {
        "agents": [{"id": "a1", "provider": "mock", "model": "m", "sessionId": "s-fixed"}],
        "max_steps": 8,
    })
    # 固定会话 id 已登记
    session = ctx.sessions.get("s-fixed")
    assert session is not None
    # agents 注册表工厂仍在（apply_loop 二次加载不破坏）
    assert ctx.agents.has_factory()


async def test_declarative_agents_resume():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = AppContext()
        load_profile(ctx, [*CORE_PROFILE, apply_persistence])
        ctx.llm.register_adapter(["mock"], EchoAdapter())
        session = ctx.sessions.create(cwd="/proj")
        session.append("turn/start", {"turn": 1})
        session.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
        sid = session.header.id

        # 第二进程：声明式 agents 从持久化恢复
        ctx2 = AppContext()
        load_profile(ctx2, [*CORE_PROFILE, apply_persistence])
        apply_loop(ctx2, {"agents": [{"id": "r1", "provider": "mock", "model": "m",
                                      "resumeSessionId": sid}]})
        restored = ctx2.sessions.get(sid)
        assert restored is not None
        assert restored.header.cwd == "/proj"


async def main():
    test_inbox_queue_and_spliced_events()
    test_inbox_persists_splices()
    await test_cancel_interrupts_turn()
    await test_cancel_before_start_ends_immediately()
    await test_declarative_agents_with_fixed_session()
    await test_declarative_agents_resume()
    print("OK: Agent 完整版测试通过（Inbox 队列/持久化、cancel 中断、声明式 agents + resume）")


if __name__ == "__main__":
    asyncio.run(main())

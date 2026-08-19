"""Agent 主循环与 Session 的验证（Step 4）。

运行：python dsh_py/tests/test_agent.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import bootstrap
from dsh_py.services.agent import AgentOptions
from dsh_py.services.llm import ChunkType, LlmAdapter, StreamChunk
from dsh_py.services.message import TextBlock, ToolCallBlock, ToolResultBlock, as_text


class ToolLoopAdapter(LlmAdapter):
    """智能 mock：首步发起工具调用，看到工具结果后收尾。"""

    async def stream(self, options):
        has_result = any(
            isinstance(b, ToolResultBlock) for m in options.messages for b in m.content
        )
        if has_result:
            yield StreamChunk(ChunkType.TEXT_DELTA, text="已为你查到天气：晴。")
            yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})
            return
        yield StreamChunk(ChunkType.BLOCK_START, index=0, block_type="tool-call")
        yield StreamChunk(ChunkType.TOOL_CALL_DELTA, index=0, tool_call_id="c1", tool_call_name="get_weather", arguments_delta='{"city":"北京"}')
        yield StreamChunk(ChunkType.BLOCK_END, index=0, block=ToolCallBlock(id="c1", name="get_weather", arguments='{"city":"北京"}'))
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "tool-calls"})


async def test_agent_tool_loop():
    ctx = AppContext()
    bootstrap(ctx)
    ctx.llm.register_adapter(["mock"], ToolLoopAdapter())

    async def weather(args):
        return f"天气查询结果：{args.get('city')} 晴", False
    ctx.tools.register("get_weather", "查询天气", {"type": "object", "properties": {"city": {"type": "string"}}}, weather)

    session = ctx.sessions.create()
    agent = ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))
    await agent.run("北京天气如何？")

    # 应执行了工具并收尾
    types = [ev.type for ev in session.events]
    assert "turn/start" in types and "turn/end" in types
    assert "tool/call" in types
    assert "tool/result" in types
    # 最终 assistant 文本
    final = None
    for ev in session.events:
        if ev.type == "assistant/message":
            final = as_text(ev.data["message"].content)
    assert final == "已为你查到天气：晴。"
    # turn 结束原因应为 completed
    turn_end = [ev for ev in session.events if ev.type == "turn/end"][0]
    assert turn_end.data["reason"] == {"kind": "completed"}


async def test_session_derive_messages_order():
    ctx = AppContext()
    bootstrap(ctx)
    ctx.llm.register_adapter(["mock"], ToolLoopAdapter())
    session = ctx.sessions.create()
    agent = ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))
    await agent.run("北京天气如何？")

    msgs = session.derive_messages()
    roles = [m.role for m in msgs]
    # user(提问) → assistant(工具调用) → user(工具结果) → assistant(收尾)
    assert roles[0] == "user"
    assert roles.count("assistant") == 2
    assert any(isinstance(b, ToolResultBlock) for m in msgs if m.role == "user" for b in m.content)


async def test_agent_factory_swappable():
    """智能体循环本身是可替换的插件：set_factory 换实现，装配无需改动。"""
    ctx = AppContext()
    bootstrap(ctx)  # 核心服务照常加载（含默认循环）
    ctx.llm.register_adapter(["mock"], ToolLoopAdapter())

    class CustomLoop:
        """替换实现：不依赖默认 Agent，run 时向会话追加自定义事件。"""

        def __init__(self, ctx):
            self.ctx = ctx

        def create_agent(self, session, options=None):
            self.created = session.header.id
            return self

        async def run(self, user_text):
            self.created_session.append("custom/ran", {"text": user_text})

    session = ctx.sessions.create()
    custom = CustomLoop(ctx)
    custom.created_session = session
    ctx.agents.set_factory(custom)  # 换循环：覆盖默认工厂

    agent = ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))
    assert agent is custom  # 创建的是自定义实现，而非默认 Agent
    await agent.run("hi")
    types = [ev.type for ev in session.events]
    assert "custom/ran" in types
    assert "turn/start" not in types  # 默认循环没有被使用

    # 可随时换回默认循环
    ctx.agents.set_factory(ctx.agentLoop)
    agent2 = ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))
    assert agent2 is not custom


async def test_agent_without_factory_raises():
    """只加载注册表、不加载任何循环时，create_agent 应给出明确指引。"""
    from dsh_py.loader import load_profile

    ctx = AppContext()
    load_profile(ctx, [
        "dsh_py.services.session:apply",
        "dsh_py.services.agent:apply_registry",
    ])
    session = ctx.sessions.create()
    try:
        ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))
    except RuntimeError as e:
        assert "工厂" in str(e) and "apply_loop" in str(e)
    else:  # pragma: no cover
        raise AssertionError("未注册工厂时本应抛出 RuntimeError")


def main():
    asyncio.run(test_agent_tool_loop())
    asyncio.run(test_session_derive_messages_order())
    asyncio.run(test_agent_factory_swappable())
    asyncio.run(test_agent_without_factory_raises())
    print("OK: Agent 主循环与 Session 测试通过（工具闭环、turn 边界、历史重建、循环可替换）")


if __name__ == "__main__":
    main()

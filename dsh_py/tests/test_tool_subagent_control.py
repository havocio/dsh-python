"""子代理控制工具（tool-subagent-control，A 类第 5 项）的验证。

运行：python dsh_py/tests/test_tool_subagent_control.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.plugins.tool_subagent_control import apply as apply_subagent_control
from dsh_py.services.agent import AgentOptions
from dsh_py.services.llm import ChunkType, LlmAdapter, StreamChunk
from dsh_py.services.message import TextBlock
from dsh_py.services.subagents import apply as apply_subagents_service


class ChildEchoAdapter(LlmAdapter):
    """子代理模型：任何输入都回一份固定文本（让可续跑子代理能跑完一轮）。"""

    async def stream(self, options):
        yield StreamChunk(ChunkType.TEXT_DELTA, text="（子代理）已收到并回复。")
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


def _ctx():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    apply_subagents_service(ctx, {})  # 注册 ctx.subagents 服务（含可续跑运行时）
    apply_subagent_control(ctx, {})
    ctx.llm.register_adapter(["child"], ChildEchoAdapter())
    return ctx


def _fake_agent(ctx, session_id="parent-1", cwd="/tmp"):
    session = ctx.sessions.create(cwd=cwd)
    # 用真实 Agent 工厂，保证其 id 与会话 id 绑定（工具授权/列举依赖该关系）
    return ctx.agents.create_agent(session, AgentOptions(provider="child", model="m"))


async def _start_child(ctx, parent):
    child_id = await ctx.subagents.start_continuable(parent, {
        "prompt": [TextBlock("初始任务")],
        "agentOptions": {"provider": "child", "model": "m"},
        "label": "查天气",
    })
    return child_id


async def _wait_idle(ctx, child_id, limit=5.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + limit
    while loop.time() < deadline:
        if ctx.subagents._child_info.get(child_id, {}).get("status") == "idle":
            return
        await asyncio.sleep(0.02)
    # 超时未 idle 也继续（初始轮可能因并发未结束，不影响子集测试）


async def test_list_agents_direct_and_recursive():
    """list_agents 返回调用方的直接子；recursive 返回完整子树。"""
    ctx = _ctx()
    parent = _fake_agent(ctx, "parent-L")
    child_id = await _start_child(ctx, parent)
    out, err, _ = await ctx.tools.execute_with_agent(
        "list_agents", json.dumps({"recursive": False}), agent=parent)
    assert err is False, out
    assert child_id in out, f"直接子未在列表：{out!r}"
    assert "查天气" in out
    # recursive 同样包含该子（单节点子树）
    out2, err2, _ = await ctx.tools.execute_with_agent(
        "list_agents", json.dumps({"recursive": True}), agent=parent)
    assert err2 is False and child_id in out2


async def test_send_message_delivers():
    """send_message 向可续跑子代理投递后续消息，工具返回确认且不含错误。"""
    ctx = _ctx()
    parent = _fake_agent(ctx, "parent-S")
    child_id = await _start_child(ctx, parent)
    out, err, _ = await ctx.tools.execute_with_agent(
        "send_message", json.dumps({"agentId": child_id, "message": "继续深入调查"}), agent=parent)
    assert err is False, out
    assert child_id in out
    await _wait_idle(ctx, child_id)


async def test_send_message_unauthorized():
    """非父 Agent 的 send_message 被拒（运行时抛 UNAUTHORIZED → 工具错误）。"""
    ctx = _ctx()
    parent_a = _fake_agent(ctx, "parent-A")
    parent_b = _fake_agent(ctx, "parent-B")
    child_id = await _start_child(ctx, parent_a)
    out, err, _ = await ctx.tools.execute_with_agent(
        "send_message", json.dumps({"agentId": child_id, "message": "越权消息"}), agent=parent_b)
    assert err is True, f"应拒绝越权 followup，但得到：{out!r}"
    assert "UNAUTHORIZED" in out or "非直接父" in out


async def test_interrupt_agent():
    """interrupt_agent 对可续跑子代理发出中断，工具返回确认、不抛错。"""
    ctx = _ctx()
    parent = _fake_agent(ctx, "parent-I")
    child_id = await _start_child(ctx, parent)
    await _wait_idle(ctx, child_id)
    out, err, _ = await ctx.tools.execute_with_agent(
        "interrupt_agent", json.dumps({"agentId": child_id}), agent=parent)
    assert err is False, out
    assert child_id in out
    assert ctx.subagents._child_info[child_id]["status"] == "interrupted"


async def test_missing_args_rejected():
    ctx = _ctx()
    parent = _fake_agent(ctx, "parent-M")
    out, err, _ = await ctx.tools.execute_with_agent(
        "send_message", json.dumps({"agentId": "x"}), agent=parent)
    assert err is True and ("必填" in out or "参数校验" in out or "message" in out)


async def test_tools_registered():
    ctx = _ctx()
    for name in ("send_message", "interrupt_agent", "list_agents"):
        assert ctx.tools.has(name), f"工具未注册：{name}"


async def main():
    await test_list_agents_direct_and_recursive()
    await test_send_message_delivers()
    await test_send_message_unauthorized()
    await test_interrupt_agent()
    await test_missing_args_rejected()
    await test_tools_registered()
    print("OK: tool-subagent-control 测试通过（列举/投递/越权拒绝/中断/参数校验）")


if __name__ == "__main__":
    asyncio.run(main())

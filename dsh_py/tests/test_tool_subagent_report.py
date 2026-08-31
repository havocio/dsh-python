"""子代理汇报工具（tool-subagent-report，A 类第 5 项续）的验证。

运行：python dsh_py/tests/test_tool_subagent_report.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.plugins.tool_subagent_control import apply as apply_subagent_control
from dsh_py.plugins.tool_subagent_report import apply as apply_subagent_report
from dsh_py.services.agent import AgentOptions
from dsh_py.services.llm import ChunkType, LlmAdapter, StreamChunk
from dsh_py.services.message import (
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    as_text,
)
from dsh_py.services.subagents import apply as apply_subagents_service


class ChildReportAdapter(LlmAdapter):
    """子代理模型：首步发起 ``report`` 工具调用；看到工具结果后收尾。"""

    async def stream(self, options):
        for m in options.messages:
            for b in m.content:
                if isinstance(b, ToolResultBlock):
                    yield StreamChunk(ChunkType.TEXT_DELTA, text="（子代理）已汇报，收尾。")
                    yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})
                    return
        yield StreamChunk(ChunkType.BLOCK_START, index=0, block_type="tool-call")
        yield StreamChunk(ChunkType.TOOL_CALL_DELTA, index=0, tool_call_id="r1",
                          tool_call_name="report",
                          arguments_delta='{"output":"子代理结论：天气晴 25°C，已完成检索"}')
        yield StreamChunk(ChunkType.BLOCK_END, index=0,
                          block=ToolCallBlock(id="r1", name="report",
                                              arguments='{"output":"子代理结论：天气晴 25°C，已完成检索"}'))
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "tool-calls"})


class ParentEchoAdapter(LlmAdapter):
    """父代理模型：收到报告后回一段文本。"""

    async def stream(self, options):
        yield StreamChunk(ChunkType.TEXT_DELTA, text="（父代理）收到子代理报告。")
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


def _ctx():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    apply_subagents_service(ctx, {})  # ctx.subagents（含可续跑运行时 + report_from）
    apply_subagent_control(ctx, {})
    apply_subagent_report(ctx, {})
    ctx.llm.register_adapter(["child"], ChildReportAdapter())
    ctx.llm.register_adapter(["parent"], ParentEchoAdapter())
    return ctx


def _parent_agent(ctx, session_id="parent-R"):
    session = ctx.sessions.create()
    return ctx.agents.create_agent(session, AgentOptions(provider="parent", model="m")), session


async def _wait_for(predicate, limit=5.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + limit
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_report_delivers_to_parent():
    """子代理 report 工具把内容投递给直接父 Agent，父会话出现该 user/message。"""
    ctx = _ctx()
    parent, parent_session = _parent_agent(ctx)
    child_id = await ctx.subagents.start_continuable(parent, {
        "prompt": [TextBlock("去查天气")],
        "agentOptions": {"provider": "child", "model": "m"},
    })
    # 等待子代理跑完其轮（期间它会调用 report → 父被唤醒处理）
    ok = await _wait_for(
        lambda: any(
            ev.type == "user/message" and "天气晴" in as_text(ev.data.content)
            for ev in parent_session.events
        )
    )
    assert ok, "父会话未收到子代理报告内容"
    # 工具本身也注册可用
    assert ctx.tools.has("report")


async def test_report_empty_output_rejected():
    ctx = _ctx()
    parent, _ = _parent_agent(ctx)
    aid = await ctx.subagents.start_continuable(parent, {
        "prompt": [TextBlock("x")], "agentOptions": {"provider": "child", "model": "m"}})
    child_agent = ctx.subagents._continuable_agents[aid]
    out, err, _ = await ctx.tools.execute_with_agent(
        "report", json.dumps({"output": "   "}), agent=child_agent)
    assert err is True and "必填" in out


async def test_report_by_non_child_rejected():
    """非可续跑子代理（如父自身）调用 report 应被拒（NO_CHILD）。"""
    ctx = _ctx()
    parent, _ = _parent_agent(ctx)  # 父不是任何子的子
    out, err, _ = await ctx.tools.execute_with_agent(
        "report", json.dumps({"output": "我不该能汇报"}), agent=parent)
    assert err is True and ("NO_CHILD" in out or "不是可续跑子代理" in out)


async def main():
    await test_report_delivers_to_parent()
    await test_report_empty_output_rejected()
    await test_report_by_non_child_rejected()
    print("OK: tool-subagent-report 测试通过（投递父/空内容拒绝/非子拒绝）")


def _user_text(msg):
    for block in msg.content:
        if isinstance(block, TextBlock):
            return block.text
    return as_text(msg.content)


if __name__ == "__main__":
    asyncio.run(main())

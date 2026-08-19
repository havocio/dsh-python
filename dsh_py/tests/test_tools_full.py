"""tools 完整版（参数校验 / 并行执行 / tool-order）的验证（第 3 层第二批）。

运行：python dsh_py/tests/test_tools_full.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.agent import AgentOptions
from dsh_py.services.llm import ChunkType, LlmAdapter, StreamChunk
from dsh_py.services.message import TextBlock, ToolCallBlock, ToolResultBlock, as_text
from dsh_py.services.system_prompt import TOOL_ORDER_REST, SystemPrompt, order_tools
from dsh_py.services.tools import json_schema_to_schema, validate_args


# --------------------------------------------------------------------------- #
# 参数校验
# --------------------------------------------------------------------------- #
def test_json_schema_to_schema_and_validate():
    parameters = {
        "type": "object",
        "properties": {"city": {"type": "string"}, "days": {"type": "integer"}},
        "required": ["city"],
    }
    assert validate_args({"city": "北京", "days": 3}, parameters) is None
    # 缺 required 字段 → 报错
    err = validate_args({"days": 3}, parameters)
    assert err is not None and "city" in err
    # 类型错误 → 报错
    err = validate_args({"city": "北京", "days": "三"}, parameters)
    assert err is not None and "days" in err
    # 未知字段被剥离（extra=strip），不报错
    assert validate_args({"city": "北京", "extra": 1}, parameters) is None
    # 无 schema → 直接通过
    assert validate_args({"whatever": 1}, {}) is None


# --------------------------------------------------------------------------- #
# execute 集成校验
# --------------------------------------------------------------------------- #
async def test_execute_validates_before_handler():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    called = []

    async def handler(args):
        called.append(args)
        return "ok", False

    ctx.tools.register("calc", "计算", {
        "type": "object",
        "properties": {"a": {"type": "number"}},
        "required": ["a"],
    }, handler)

    # 合法参数 → handler 被调用
    text, is_error = await ctx.tools.execute("calc", '{"a": 2}')
    assert is_error is False and text == "ok"
    assert len(called) == 1
    # 非法参数 → 不调 handler，返回错误文本
    text, is_error = await ctx.tools.execute("calc", '{"a": "not-a-number"}')
    assert is_error is True
    assert "参数校验失败" in text
    assert len(called) == 1  # handler 未被调用


# --------------------------------------------------------------------------- #
# 并行执行
# --------------------------------------------------------------------------- #
class ParallelToolAdapter(LlmAdapter):
    """首步发起两个工具调用，看到工具结果后收尾。"""

    async def stream(self, options):
        if any(isinstance(b, ToolResultBlock) for m in options.messages for b in m.content):
            yield StreamChunk(ChunkType.TEXT_DELTA, text="完成")
            yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})
            return
        yield StreamChunk(ChunkType.BLOCK_START, index=0, block_type="tool-call")
        yield StreamChunk(ChunkType.TOOL_CALL_DELTA, index=0, tool_call_id="c1",
                          tool_call_name="slow_a", arguments_delta='{}')
        yield StreamChunk(ChunkType.BLOCK_END, index=0,
                          block=ToolCallBlock(id="c1", name="slow_a", arguments="{}"))
        yield StreamChunk(ChunkType.BLOCK_START, index=1, block_type="tool-call")
        yield StreamChunk(ChunkType.TOOL_CALL_DELTA, index=1, tool_call_id="c2",
                          tool_call_name="slow_b", arguments_delta='{}')
        yield StreamChunk(ChunkType.BLOCK_END, index=1,
                          block=ToolCallBlock(id="c2", name="slow_b", arguments="{}"))
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "tool-calls"})


class _FinishAdapter(LlmAdapter):
    async def stream(self, options):
        yield StreamChunk(ChunkType.TEXT_DELTA, text="完成")
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


async def test_parallel_tool_execution():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    ctx.llm.register_adapter(["mock"], ParallelToolAdapter())
    events = {"start": [], "order": []}

    async def slow_a(args):
        await asyncio.sleep(0.05)
        events["order"].append("a")
        return "A 完成", False

    async def slow_b(args):
        await asyncio.sleep(0.01)
        events["order"].append("b")
        return "B 完成", False

    ctx.tools.register("slow_a", "慢工具A", {}, slow_a)
    ctx.tools.register("slow_b", "慢工具B", {}, slow_b)

    session = ctx.sessions.create()
    agent = ctx.agents.create_agent(
        session, AgentOptions(provider="mock", model="m", max_parallel_tool_calls=2))
    await agent.run("并行跑")

    # 两个工具都已执行
    results = [e for e in session.events if e.type == "tool/result"]
    assert len(results) == 2
    texts = [_tool_result_text(r.data["message"]) for r in results]
    assert "A 完成" in texts and "B 完成" in texts
    # 完成顺序：b（0.01s）先于 a（0.05s）→ 证明真的并行
    assert events["order"] == ["b", "a"]
    # 回填顺序仍按原调用顺序（tool/result 事件顺序稳定）
    names = [e.data["name"] for e in session.events if e.type == "tool/call"]
    assert names == ["slow_a", "slow_b"]


async def test_serial_execution_when_parallel_cap_one():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    ctx.llm.register_adapter(["mock"], ParallelToolAdapter())
    running = []
    max_inflight = 0

    async def slow(args):
        nonlocal max_inflight
        running.append(1)
        max_inflight = max(max_inflight, len(running))
        await asyncio.sleep(0.02)
        running.pop()
        return "ok", False

    ctx.tools.register("slow_a", "A", {}, slow)
    ctx.tools.register("slow_b", "B", {}, slow)

    session = ctx.sessions.create()
    # max_parallel_tool_calls=1 → 串行
    agent = ctx.agents.create_agent(
        session, AgentOptions(provider="mock", model="m", max_parallel_tool_calls=1))
    await agent.run("串行跑")
    assert max_inflight == 1


# --------------------------------------------------------------------------- #
# tool-order
# --------------------------------------------------------------------------- #
def test_order_tools():
    tools = [{"name": "z_tool"}, {"name": "a_tool"}, {"name": "m_tool"}]
    # 无配置 → 字典序
    assert [t["name"] for t in order_tools(tools, None)] == ["a_tool", "m_tool", "z_tool"]
    # 配置顺序 + rest 标记
    ordered = order_tools(tools, ["z_tool", TOOL_ORDER_REST, "a_tool"])
    assert [t["name"] for t in ordered] == ["z_tool", "m_tool", "a_tool"]
    # 引用未注册工具 → 报错
    try:
        order_tools(tools, ["ghost", TOOL_ORDER_REST])
    except ValueError as e:
        assert "ghost" in str(e)
    else:  # pragma: no cover
        raise AssertionError("toolOrder 引用未注册工具应报错")


async def test_system_prompt_tool_order_config():
    ctx = AppContext()
    sp = SystemPrompt(ctx, {"tool_order": ["zz_tool", TOOL_ORDER_REST]})
    sp.tools(lambda c: {"schemas": [{"name": "aa_tool", "description": "d",
                                     "parameters": {"type": "object"}},
                                    {"name": "zz_tool", "description": "d",
                                     "parameters": {"type": "object"}}]})
    assembly = await sp.assemble()
    assert [t["name"] for t in assembly.tools] == ["zz_tool", "aa_tool"]
    # 缺 rest 标记 → 加载即报错
    try:
        SystemPrompt(AppContext(), {"tool_order": ["x"]})
    except ValueError as e:
        assert TOOL_ORDER_REST in str(e)
    else:  # pragma: no cover
        raise AssertionError("toolOrder 缺 rest 标记应报错")


def _tool_result_text(msg) -> str:
    """提取工具结果消息的文本（下钻 ToolResultBlock.content）。"""
    for block in msg.content:
        if isinstance(block, ToolResultBlock):
            return as_text(block.content)
    return as_text(msg.content)


async def main():
    test_json_schema_to_schema_and_validate()
    await test_execute_validates_before_handler()
    await test_parallel_tool_execution()
    await test_serial_execution_when_parallel_cap_one()
    test_order_tools()
    await test_system_prompt_tool_order_config()
    print("OK: tools 完整版测试通过（参数校验、并行/串行执行、tool-order）")


if __name__ == "__main__":
    asyncio.run(main())

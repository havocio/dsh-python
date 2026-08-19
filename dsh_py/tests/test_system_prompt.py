"""系统提示词组装（system-prompt）的验证（第 3 层支撑服务）。

运行：python dsh_py/tests/test_system_prompt.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.agent import AgentOptions
from dsh_py.services.llm import ChunkType, LlmAdapter, StreamChunk
from dsh_py.services.system_prompt import (
    PERSONA_ORDER,
    PERSONA_SECTION,
    PromptAssembly,
    PromptContext,
    PromptSection,
    SystemPrompt,
    join_context_sections,
    render_context_sections,
    render_prompt,
    apply as apply_system_prompt,
)


async def test_register_and_render_ordered_sections():
    ctx = AppContext()
    sp = SystemPrompt(ctx, {"persona": "你是严谨的助手。"})
    # 默认注册：harness:identity(-100) + deployment:persona(0)
    sp.section(PromptSection(name="tool-guidance", order=100, text="用工具时遵守规则。"))
    assembly = await sp.assemble()
    names = [s["name"] for s in assembly.sections]
    assert names == ["harness:identity", "deployment:persona", "tool-guidance"]
    rendered = render_prompt(assembly)
    assert "DeepSeek Harness" in rendered
    assert "你是严谨的助手" in rendered
    assert "用工具时遵守规则" in rendered
    # 片段按空行连接
    assert "\n\n" in rendered


async def test_variable_interpolation_strict():
    ctx = AppContext()
    sp = SystemPrompt(ctx, {})
    sp.variable("provider", lambda c: "deepseek")
    sp.variable("model", lambda c: "deepseek-chat")
    sp.section(PromptSection(name="route", order=200, text="当前路由：{{provider}}/{{model}}"))
    assembly = await sp.assemble()
    rendered = render_prompt(assembly)
    assert "当前路由：deepseek/deepseek-chat" in rendered

    # 未知变量 → 渲染时报错
    sp.section(PromptSection(name="bad", order=300, text="引用 {{nope}}"))
    assembly2 = await sp.assemble()
    try:
        render_prompt(assembly2)
    except ValueError as e:
        assert "未知 prompt 变量" in str(e) and "nope" in str(e)
    else:  # pragma: no cover
        raise AssertionError("未知变量应报错")

    # 无值变量 → 报错
    ctx2 = AppContext()
    sp2 = SystemPrompt(ctx2, {})
    sp2.variable("x", lambda c: None)
    sp2.section(PromptSection(name="v", order=1, text="{{x}}"))
    try:
        render_prompt(await sp2.assemble())
    except ValueError as e:
        assert "无值" in str(e)
    else:  # pragma: no cover
        raise AssertionError("无值变量应报错")


async def test_complete_section_exclusive():
    ctx = AppContext()
    sp = SystemPrompt(ctx, {})
    sp.section(PromptSection(name="solo", order=50, text="整条提示由我独占", complete=True))
    sp.section(PromptSection(name="other", order=1, text="不会出现"))
    assembly = await sp.assemble()
    # 仅 complete 片段保留为唯一提示
    assert [s["name"] for s in assembly.sections] == ["solo"]
    assert render_prompt(assembly) == "整条提示由我独占"

    # 多个 complete → 报错
    sp.section(PromptSection(name="solo2", order=60, text="另一个独占", complete=True))
    try:
        await sp.assemble()
    except ValueError as e:
        assert "多个 complete" in str(e)
    else:  # pragma: no cover
        raise AssertionError("多个 complete 片段应报错")


async def test_dynamic_context_snapshot():
    ctx = AppContext()
    sp = SystemPrompt(ctx, {})
    sp.context(PromptContext(name="cwd", order=1, text="工作目录：{{cwd}}"))
    sp.variable("cwd", lambda c: "/workspace")
    sp.context(PromptContext(name="catalog", order=0, text="目录：项目文档"))

    assembly = await sp.assemble()
    sections = render_context_sections(assembly)
    assert [s["name"] for s in sections] == ["catalog", "cwd"]  # 按 order 升序
    snapshot = join_context_sections(sections)
    assert "Current runtime context" in snapshot
    assert "工作目录：/workspace" in snapshot


async def test_suppress_runtime_context():
    ctx = AppContext()
    sp = SystemPrompt(ctx, {})
    sp.context(PromptContext(name="c", order=0, text="一些上下文"))
    sp.suppress_runtime_context()
    assembly = await sp.assemble()
    assert assembly.contexts == []
    assert render_context_sections(assembly) == []


async def test_tool_provider_collection():
    ctx = AppContext()
    sp = SystemPrompt(ctx, {})
    sp.tools(lambda c: {"schemas": [{"name": "weather", "description": "查天气",
                                     "parameters": {"type": "object"}}]})
    sp.tools(lambda c: {"schemas": [{"name": "time", "description": "查时间",
                                     "parameters": {"type": "object"}}]})
    assembly = await sp.assemble()
    assert [t["name"] for t in assembly.tools] == ["time", "weather"]  # 字典序


def test_duplicate_registration_fails():
    ctx = AppContext()
    sp = SystemPrompt(ctx, {})
    sp.section(PromptSection(name="a", order=1, text="x"))
    try:
        sp.section(PromptSection(name="a", order=2, text="y"))
    except ValueError as e:
        assert "已注册" in str(e)
    else:  # pragma: no cover
        raise AssertionError("重复注册应报错")
    # 变量名非法
    try:
        sp.variable("Bad Name", lambda c: "x")
    except ValueError as e:
        assert "变量名非法" in str(e)
    else:  # pragma: no cover
        raise AssertionError("非法变量名应报错")


async def test_agent_uses_system_prompt_when_mounted():
    """挂载 systemPrompt 后，Agent 的 system 来自组装渲染而非 options.system。"""
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    apply_system_prompt(ctx, {"persona": "你是测试人格。"})
    ctx.llm.register_adapter(["mock"], _EchoAdapter())
    session = ctx.sessions.create()
    agent = ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m", system="会被覆盖"))
    await agent.run("hi")
    # 适配器收到渲染后的 system（含 persona）
    assert _EchoAdapter.last_system is not None
    assert "你是测试人格" in _EchoAdapter.last_system


class _EchoAdapter(LlmAdapter):
    last_system = None

    async def stream(self, options):
        _EchoAdapter.last_system = options.system
        yield StreamChunk(ChunkType.TEXT_DELTA, text="ok")
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


async def main():
    await test_register_and_render_ordered_sections()
    await test_variable_interpolation_strict()
    await test_complete_section_exclusive()
    await test_dynamic_context_snapshot()
    await test_suppress_runtime_context()
    await test_tool_provider_collection()
    test_duplicate_registration_fails()
    await test_agent_uses_system_prompt_when_mounted()
    print("OK: 系统提示词组装测试通过（片段排序、严格插值、complete 独占、上下文快照、Agent 集成）")


if __name__ == "__main__":
    asyncio.run(main())

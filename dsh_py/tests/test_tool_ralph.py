"""模型侧 ``ralph`` 工具的验证（第 3 层，对标 dsh 的 tool-ralph.spec.ts）。

运行：python dsh_py/tests/test_tool_ralph.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.plugins.tool_ralph import apply as apply_tool_ralph
from dsh_py.services.agent import AgentOptions
from dsh_py.services.llm import ChunkType, LlmAdapter, StreamChunk
from dsh_py.services.subagents import apply as apply_subagents
from dsh_py.services.workflow.engine import apply as apply_engine

COMPLETE_REPORT = (
    '{"status": "complete", "summary": "done", "evidence": ["e1"], "nextSteps": [], "blocker": ""}'
)
CONTINUE_REPORT = (
    '{"status": "continue", "summary": "working", "evidence": ["e1"], "nextSteps": ["more"], "blocker": ""}'
)


class CompleteAdapter(LlmAdapter):
    async def stream(self, options):
        yield StreamChunk(ChunkType.TEXT_DELTA, text=COMPLETE_REPORT)
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


class ContinueAdapter(LlmAdapter):
    async def stream(self, options):
        yield StreamChunk(ChunkType.TEXT_DELTA, text=CONTINUE_REPORT)
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


class BadAdapter(LlmAdapter):
    async def stream(self, options):
        yield StreamChunk(ChunkType.TEXT_DELTA, text="this is not json at all")
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


def _setup(tool_config=None, subagents_config=None):
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    apply_subagents(ctx, subagents_config or {})
    apply_engine(ctx, {"maxConcurrentAgents": 4, "maxTotalAgents": 512, "disposeGraceMs": 300})
    ctx.llm.register_adapter(["mock-complete"], CompleteAdapter())
    ctx.llm.register_adapter(["mock-continue"], ContinueAdapter())
    ctx.llm.register_adapter(["mock-bad"], BadAdapter())
    apply_tool_ralph(ctx, tool_config or {})
    return ctx


def _parent(ctx, provider="mock-complete"):
    session = ctx.sessions.create()
    return ctx.agents.create_agent(session, AgentOptions(provider=provider, model="m"))


def _call(ctx, args: dict, provider="mock-complete"):
    return ctx.tools.execute_with_agent(
        "ralph", json.dumps(args), agent=_parent(ctx, provider)
    )


async def test_ralph_completes():
    ctx = _setup()
    text, is_error, _ = await _call(ctx, {"objective": "finish the audit"})
    assert not is_error
    assert "Ralph worker reported completion after 1 round." in text
    assert '"summary": "done"' in text


async def test_ralph_budget_limited():
    ctx = _setup()
    text, is_error, _ = await _call(ctx, {"objective": "keep going", "maxRounds": 2}, provider="mock-continue")
    assert not is_error
    assert "Ralph reached its 2 rounds limit" in text


async def test_ralph_round_failed():
    ctx = _setup()
    text, is_error, _ = await _call(ctx, {"objective": "do it"}, provider="mock-bad")
    assert is_error
    assert "child failed before producing a structured report" in text


async def test_ralph_requires_agent():
    ctx = _setup()
    text, is_error = await ctx.tools.execute("ralph", json.dumps({"objective": "x"}))
    assert is_error
    assert "calling agent" in text


async def test_ralph_provider_must_be_fresh():
    # 继承父上下文的 provider 被拒（Ralph 要求全新子）
    ctx = _setup(
        tool_config={"subagentProvider": "legacy"},
        subagents_config={"providers": {"legacy": {"inheritsParentContext": True, "outputSchema": True}}},
    )
    text, is_error, _ = await _call(ctx, {"objective": "x"})
    assert is_error
    assert "inherits parent context" in text


async def test_ralph_provider_requires_structured():
    # 无结构化输出能力的 provider 被拒
    ctx = _setup(
        tool_config={"subagentProvider": "plain"},
        subagents_config={"providers": {"plain": {"outputSchema": False}}},
    )
    text, is_error, _ = await _call(ctx, {"objective": "x"})
    assert is_error
    assert "does not support structured output" in text


async def test_ralph_empty_objective():
    ctx = _setup()
    text, is_error, _ = await _call(ctx, {"objective": "   "})
    assert is_error
    assert "non-empty" in text


async def _main():
    tests = [
        test_ralph_completes,
        test_ralph_budget_limited,
        test_ralph_round_failed,
        test_ralph_requires_agent,
        test_ralph_provider_must_be_fresh,
        test_ralph_provider_requires_structured,
        test_ralph_empty_objective,
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
    print(f"tool-ralph: {'全部通过' if failures == 0 else f'{failures} 个失败'}")
    return failures


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(_main()) else 0)

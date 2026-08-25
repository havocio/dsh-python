"""模型侧 ``workflow`` 工具的验证（第 3 层，对标 dsh 的 tool-workflow.spec.ts）。

运行：python dsh_py/tests/test_tool_workflow.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.core.signal import CancelSignal
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.plugins.tool_workflow import apply as apply_tool_workflow
from dsh_py.services.agent import AgentOptions
from dsh_py.services.llm import ChunkType, LlmAdapter, StreamChunk
from dsh_py.services.subagents import apply as apply_subagents
from dsh_py.services.workflow.engine import apply as apply_engine


class ChildTextAdapter(LlmAdapter):
    """子代理模型：prompt 含 ``JSON:`` 返回 JSON，否则返回固定文本。"""

    async def stream(self, options):
        prompt = ""
        for m in options.messages:
            for b in m.content:
                if isinstance(b, dict) and b.get("type") == "text":
                    prompt += str(b.get("text", ""))
                elif hasattr(b, "text"):
                    prompt += str(b.text)
        if "JSON:" in prompt:
            yield StreamChunk(ChunkType.TEXT_DELTA, text='{"ok": true}')
        else:
            yield StreamChunk(ChunkType.TEXT_DELTA, text="child-result")
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


def _setup(tool_config=None):
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    apply_subagents(ctx, {})
    apply_engine(ctx, {"maxConcurrentAgents": 4, "maxTotalAgents": 10, "disposeGraceMs": 300})
    ctx.llm.register_adapter(["mock"], ChildTextAdapter())
    apply_tool_workflow(ctx, tool_config or {})
    return ctx


def _parent(ctx):
    session = ctx.sessions.create()
    return ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))


def _call(ctx, args: dict, parent=None, signal=None, tool_name="workflow"):
    return ctx.tools.execute_with_agent(
        tool_name, json.dumps(args), agent=parent or _parent(ctx), signal=signal
    )


async def test_tool_runs_script():
    ctx = _setup()
    text, is_error, _ = await _call(ctx, {
        "script": "return {'a': 1}",
        "meta": {"name": "my-wf", "description": "test"},
    })
    assert not is_error
    assert 'workflow "my-wf" completed (0 agents).' in text
    assert '"a": 1' in text


async def test_tool_agent_count_and_value():
    ctx = _setup()
    text, is_error, _ = await _call(ctx, {
        "script": "x = await agent('child')\nreturn {'x': x}",
        "meta": {"name": "fan", "description": "test"},
    })
    assert not is_error
    assert "completed (1 agent)." in text
    assert '"x": "child-result"' in text


async def test_tool_bad_meta_is_error():
    ctx = _setup()
    text, is_error, _ = await _call(ctx, {
        "script": "return 1",
        "meta": {"name": ""},
    })
    assert is_error
    assert "invalid meta" in text


async def test_tool_bad_script_is_error():
    ctx = _setup()
    text, is_error, _ = await _call(ctx, {
        "script": "return {",
        "meta": {"name": "wf", "description": "d"},
    })
    assert is_error
    assert "does not parse" in text


async def test_tool_durable_records():
    ctx = _setup()
    parent = _parent(ctx)
    _, is_error, _ = await _call(ctx, {
        "script": "x = await agent('child', {'label': 'c1'})\nreturn x",
        "meta": {"name": "rec", "description": "d"},
    }, parent=parent)
    assert not is_error
    types = [ev.type for ev in parent.session.events]
    assert "tool-workflow/run-start" in types
    assert "tool-workflow/run-end" in types
    assert "tool-workflow/agent-start" in types
    assert "tool-workflow/agent-end" in types
    start = next(ev.data for ev in parent.session.events if ev.type == "tool-workflow/run-start")
    assert start["name"] == "rec"
    end = next(ev.data for ev in parent.session.events if ev.type == "tool-workflow/run-end")
    assert end["stopReason"] == "completed"


async def test_tool_cancelled_run_is_error():
    ctx = _setup()
    signal = CancelSignal()
    signal.abort("parent step aborted")  # 预中止：确定性取消（避免与 mock 子竞态）
    parent = _parent(ctx)
    text, is_error, _ = await _call(ctx, {
        "script": "x = await agent('long')\nreturn x",
        "meta": {"name": "wf", "description": "d"},
    }, parent=parent, signal=signal)
    assert is_error
    assert "cancelled" in text.lower()


async def test_tool_truncation():
    ctx = _setup(tool_config={"maxResultChars": 40})
    text, is_error, _ = await _call(ctx, {
        "script": "return {'long': 'x' * 200}",
        "meta": {"name": "wf", "description": "d"},
    })
    assert not is_error
    assert "[truncated" in text


async def test_tool_requires_agent():
    ctx = _setup()
    text, is_error = await ctx.tools.execute(
        "workflow",
        json.dumps({"script": "return 1", "meta": {"name": "wf", "description": "d"}}),
    )
    assert is_error
    assert "calling agent" in text


async def test_tool_toolname_override():
    ctx = _setup(tool_config={"toolName": "orchestrate"})
    schemas = ctx.tools.list_schemas()
    assert any(s["name"] == "orchestrate" for s in schemas)
    text, is_error, _ = await _call(ctx, {
        "script": "return 42",
        "meta": {"name": "wf", "description": "d"},
    }, tool_name="orchestrate")
    assert not is_error
    assert "42" in text


async def _main():
    tests = [
        test_tool_runs_script,
        test_tool_agent_count_and_value,
        test_tool_bad_meta_is_error,
        test_tool_bad_script_is_error,
        test_tool_durable_records,
        test_tool_cancelled_run_is_error,
        test_tool_truncation,
        test_tool_requires_agent,
        test_tool_toolname_override,
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
    print(f"tool-workflow: {'全部通过' if failures == 0 else f'{failures} 个失败'}")
    return failures


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(_main()) else 0)

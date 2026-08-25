"""workflow seam + 内联引擎的验证（第 3 层，对标 dsh 的 workflow/* 系列测试）。

运行：python dsh_py/tests/test_workflow.py

覆盖：realm（物化/渲染）、meta（校验）、seam（WorkflowError/is_fatal/品牌）、
引擎端到端（纯返回/语法错/meta 错/agent 文本/组合子/上限/取消/事件序列/
不可序列化返回值）、invariant（配对违规响亮失败）。
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.agent import AgentOptions
from dsh_py.services.llm import ChunkType, LlmAdapter, StreamChunk
from dsh_py.services.subagents import apply as apply_subagents
from dsh_py.services.workflow import WorkflowError, WorkflowRunId, is_fatal_workflow_error
from dsh_py.services.workflow.engine import apply as apply_engine
from dsh_py.services.workflow.meta import validate_meta
from dsh_py.services.workflow.realm import MaterializeError, materialize_from_realm, render_thrown
from dsh_py.services.workflow.types import WorkflowMeta, WorkflowPhase, WorkflowResult, WorkflowRunInfo


class ChildTextAdapter(LlmAdapter):
    """子代理模型：prompt 含 ``JSON:`` 时返回 JSON 文本，否则返回固定文本。"""

    async def stream(self, options):
        prompt = ""
        for m in options.messages:
            for b in m.content:
                if isinstance(b, dict) and b.get("type") == "text":
                    prompt += str(b.get("text", ""))
                elif hasattr(b, "text"):
                    prompt += str(b.text)
        if "JSON:" in prompt:
            yield StreamChunk(ChunkType.TEXT_DELTA, text='{"status": "complete", "summary": "done", "evidence": ["e"], "nextSteps": [], "blocker": ""}')
        else:
            yield StreamChunk(ChunkType.TEXT_DELTA, text="child-result")
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


def _setup(engine_config=None):
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    apply_subagents(ctx, {})
    apply_engine(ctx, engine_config or {"maxConcurrentAgents": 4, "maxTotalAgents": 10, "disposeGraceMs": 300})
    ctx.llm.register_adapter(["mock"], ChildTextAdapter())
    return ctx


def _parent(ctx):
    session = ctx.sessions.create()
    agent = ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))
    return agent


def _run_script(ctx, script, meta=None, args=None, parent=None, **extra):
    """启动一次运行并等待结果（同步帮助：必须在运行循环内调用）。"""
    meta = meta or {"name": "test-wf", "description": "test"}
    run = ctx.workflowEngine.start({
        "script": script,
        "meta": meta,
        **({"args": args} if args is not None else {}),
        "parent": parent or _parent(ctx),
        **extra,
    })
    return run


async def test_realm_materialize():
    assert materialize_from_realm(None, "root") is None
    assert materialize_from_realm(42) == 42
    assert materialize_from_realm(True) is True
    value = {"a": [1, 2, {"b": "x"}], "c": None}
    assert materialize_from_realm(value) == value
    # 循环
    cyclic = {}
    cyclic["self"] = cyclic
    try:
        materialize_from_realm(cyclic, "v")
        assert False, "循环引用应拒绝"
    except MaterializeError as exc:
        assert "circular" in exc.reason
    # 函数 / set / bytes / 复数
    for bad in (lambda: 1, {1, 2}, b"x", 1j):
        try:
            materialize_from_realm(bad, "v")
            assert False, f"{bad!r} 应拒绝"
        except MaterializeError:
            pass
    # 非有限数
    try:
        materialize_from_realm(float("inf"), "v")
        assert False, "inf 应拒绝"
    except MaterializeError as exc:
        assert "non-finite" in exc.reason
    # 非字符串键
    try:
        materialize_from_realm({1: "x"}, "v")
        assert False, "非字符串键应拒绝"
    except MaterializeError:
        pass
    # 嵌套 None = JSON null（Python 无 undefined/null 区分）：允许保留
    assert materialize_from_realm({"a": None}, "v") == {"a": None}
    # 嵌套 None = JSON null（Python 无 undefined/null 区分）：允许保留


async def test_realm_render_thrown():
    assert render_thrown(ValueError("boom")) == "boom"
    assert render_thrown("plain") == "plain"
    assert render_thrown(42) == "42"
    assert render_thrown(None) == "None"


async def test_meta_validate():
    meta = validate_meta({"name": "wf", "description": "d"})
    assert meta.name == "wf" and meta.description == "d"
    meta2 = validate_meta({
        "name": "wf", "description": "d", "whenToUse": "w",
        "phases": [{"title": "p", "detail": "x", "provider": "spawn", "model": "m"}],
    })
    assert meta2.phases[0].title == "p"
    assert isinstance(meta2, WorkflowMeta)
    # 非法
    for bad in (
        "not-an-object",
        {"description": "d"},
        {"name": "wf"},
        {"name": "wf", "description": "d", "unknown": 1},
        {"name": "wf", "description": "d", "phases": [{"title": ""}]},
        {"name": "wf", "description": "d", "phases": [{"title": "p", "pattern": "x"}]},
    ):
        try:
            validate_meta(bad)
            assert False, f"{bad!r} 应 META_INVALID"
        except WorkflowError as exc:
            assert exc.code == "META_INVALID"


async def test_seam_errors():
    err = WorkflowError("boom", "AGENT_CAP")
    assert err.code == "AGENT_CAP"
    assert err.fatal is True
    assert is_fatal_workflow_error(err)
    err2 = WorkflowError("x", "CANCELLED", fatal=False)
    assert err2.fatal is False
    assert not is_fatal_workflow_error(err2)
    assert not is_fatal_workflow_error(ValueError("no"))
    # 品牌
    rid = WorkflowRunId("abc")
    assert isinstance(rid, str) and rid == "abc"


async def test_engine_plain_return():
    ctx = _setup()
    run = _run_script(ctx, "return {'a': 1, 'b': [1, 2, 3]}")
    result = await run.result
    assert result.stopReason == "completed"
    assert result.value == {"a": 1, "b": [1, 2, 3]}
    assert result.agentsStarted == 0
    await run.dispose()


async def test_engine_script_parse_error():
    ctx = _setup()
    try:
        ctx.workflowEngine.start({
            "script": "return {",
            "meta": {"name": "wf", "description": "d"},
            "parent": _parent(ctx),
        })
        assert False, "语法错误应同步 SCRIPT_PARSE"
    except WorkflowError as exc:
        assert exc.code == "SCRIPT_PARSE"
    # export const meta 指引
    try:
        ctx.workflowEngine.start({
            "script": "export const meta = {}\nreturn 1",
            "meta": {"name": "wf", "description": "d"},
            "parent": _parent(ctx),
        })
        assert False, "meta 语句应 SCRIPT_PARSE"
    except WorkflowError as exc:
        assert exc.code == "SCRIPT_PARSE"
        assert "meta" in exc.message


async def test_engine_meta_invalid():
    ctx = _setup()
    try:
        ctx.workflowEngine.start({
            "script": "return 1",
            "meta": {"name": ""},
            "parent": _parent(ctx),
        })
        assert False, "非法 meta 应 META_INVALID"
    except WorkflowError as exc:
        assert exc.code == "META_INVALID"


async def test_engine_agent_text_and_events():
    ctx = _setup()
    collected = []
    ctx.on("workflow/start", lambda info: collected.append(("start", info)))
    ctx.on("workflow/agent-start", lambda info, agent: collected.append(("agent-start", agent)))
    ctx.on("workflow/agent-end", lambda info, agent: collected.append(("agent-end", agent)))
    ctx.on("workflow/end", lambda info, result: collected.append(("end", result)))
    ctx.on("workflow/phase", lambda info, title: collected.append(("phase", title)))
    ctx.on("workflow/log", lambda info, message: collected.append(("log", message)))

    run = _run_script(
        ctx,
        "phase('first')\n"
        "log('hello')\n"
        "x = await agent('child task', {'label': 'c1'})\n"
        "return {'x': x}",
    )
    result = await run.result
    await asyncio.sleep(0.01)  # 让 workflow/end 任务发布
    assert result.stopReason == "completed"
    assert result.value == {"x": "child-result"}
    assert result.agentsStarted == 1
    await run.dispose()

    names = [c[0] for c in collected]
    assert names[0] == "start"
    assert "phase" in names and "log" in names
    assert names.count("agent-start") == 1 and names.count("agent-end") == 1
    assert names[-1] == "end"
    # agent-end 配对信息
    agent_end = next(c[1] for c in collected if c[0] == "agent-end")
    assert agent_end.seq == 1 and agent_end.outcome == "completed"
    assert str(agent_end.childId)
    # end 载荷：无 value
    end = next(c[1] for c in collected if c[0] == "end")
    assert end.stopReason == "completed"
    assert not hasattr(end, "value")


async def test_engine_parallel_pipeline():
    ctx = _setup()
    script = (
        "p = await parallel([lambda: agent('a'), lambda: agent('b')])\n"
        "q = await pipeline([1, 2], lambda prev, item, index: item * 10)\n"
        "return {'p': p, 'q': q}"
    )
    run = _run_script(ctx, script)
    result = await run.result
    assert result.stopReason == "completed"
    assert result.value == {"p": ["child-result", "child-result"], "q": [10, 20]}
    assert result.agentsStarted == 2
    await run.dispose()


async def test_engine_agent_cap():
    ctx = _setup(engine_config={"maxTotalAgents": 2})
    script = "await agent('1')\nawait agent('2')\nawait agent('3')\nreturn 'never'"
    run = _run_script(ctx, script)
    result = await run.result
    assert result.stopReason == "error"
    assert "total agent cap" in (result.error or "")
    assert result.agentsStarted == 2
    await run.dispose()


async def test_engine_cancel():
    ctx = _setup()
    script = "x = await agent('long')\nreturn {'x': x}"
    run = _run_script(ctx, script)
    # 立即取消（子尚未启动完成的竞态由执行侧处理）
    run.cancel("test cancel")
    result = await run.result
    assert result.stopReason == "cancelled"
    assert "test cancel" in (result.error or "")
    await run.dispose()


async def test_engine_result_unserializable():
    ctx = _setup()
    script = "return {1, 2}"  # set 不可序列化
    run = _run_script(ctx, script)
    result = await run.result
    assert result.stopReason == "error"
    assert "not plain JSON data" in (result.error or "")
    await run.dispose()


async def test_engine_schema_structured():
    ctx = _setup()
    script = (
        "r = await agent('JSON: return report', {'schema': {'type': 'object', 'properties': {'status': {'type': 'string'}}, 'required': ['status']}})\n"
        "return {'r': r}"
    )
    run = _run_script(ctx, script)
    result = await run.result
    assert result.stopReason == "completed"
    assert result.value["r"]["status"] == "complete"
    await run.dispose()


async def test_engine_unsupported_option():
    ctx = _setup()
    script = "await agent('x', {'effort': 'high'})\nreturn 1"
    run = _run_script(ctx, script)
    result = await run.result
    assert result.stopReason == "error"
    assert "deferred" in (result.error or "")
    await run.dispose()


async def test_engine_args_global():
    ctx = _setup()
    run = _run_script(ctx, "return {'files': args['files']}", args={"files": ["a.txt", "b.txt"]})
    result = await run.result
    assert result.stopReason == "completed"
    assert result.value == {"files": ["a.txt", "b.txt"]}
    await run.dispose()


async def test_engine_cancelled_start_signal():
    from dsh_py.core.signal import CancelSignal

    ctx = _setup()
    signal = CancelSignal()
    signal.abort("pre-aborted")
    run = ctx.workflowEngine.start({
        "script": "return 'never'",
        "meta": {"name": "wf", "description": "d"},
        "parent": _parent(ctx),
        "signal": signal,
    })
    result = await run.result
    assert result.stopReason == "cancelled"
    assert result.agentsStarted == 0
    await run.dispose()


async def test_invariant_failures():
    from dsh_py.services.invariants import InvariantError, apply as apply_invariants
    from dsh_py.services.workflow.invariant import apply as apply_wf_invariant

    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    apply_invariants(ctx, {})
    apply_wf_invariant(ctx)
    # 独立引擎实例（不经 apply_engine，只测事件）
    from dsh_py.services.workflow.engine import InlineWorkflowEngine

    engine = InlineWorkflowEngine(ctx, {"maxConcurrentAgents": 1, "maxTotalAgents": 5, "disposeGraceMs": 100})
    meta = WorkflowMeta(name="wf", description="d")
    info = WorkflowRunInfo(WorkflowRunId("r1"), meta)

    # 合法 start → end（无 agent）不违规
    engine.emit_workflow_event("workflow/start", info)
    engine.emit_workflow_event("workflow/end", info, WorkflowResult(stopReason="completed", agentsStarted=0))

    # 缺失 start 的 agent-end 应响亮失败
    bad_info = WorkflowRunInfo(WorkflowRunId("r2"), meta)
    try:
        engine.emit_workflow_event("workflow/agent-end", bad_info, None)
        assert False, "无 start 的 agent-end 应 InvariantError"
    except InvariantError:
        pass
    # 合法 start → agent-start → agent-end → end
    info3 = WorkflowRunInfo(WorkflowRunId("r3"), meta)
    engine.emit_workflow_event("workflow/start", info3)
    from dsh_py.services.workflow.types import WorkflowAgentEndInfo, WorkflowAgentInfo

    engine.emit_workflow_event("workflow/agent-start", info3, WorkflowAgentInfo(seq=1, label="l", childId="c1"))
    engine.emit_workflow_event("workflow/agent-end", info3, WorkflowAgentEndInfo(seq=1, label="l", childId="c1", outcome="completed"))
    engine.emit_workflow_event("workflow/end", info3, WorkflowResult(stopReason="completed", agentsStarted=1))
    # end 前仍有未配对 agent → 违规
    info4 = WorkflowRunInfo(WorkflowRunId("r4"), meta)
    engine.emit_workflow_event("workflow/start", info4)
    engine.emit_workflow_event("workflow/agent-start", info4, WorkflowAgentInfo(seq=1, label="l", childId="c1"))
    try:
        engine.emit_workflow_event("workflow/end", info4, WorkflowResult(stopReason="completed", agentsStarted=1))
        assert False, "未配对 agent 的 end 应 InvariantError"
    except InvariantError:
        pass


async def _main():
    tests = [
        test_realm_materialize,
        test_realm_render_thrown,
        test_meta_validate,
        test_seam_errors,
        test_engine_plain_return,
        test_engine_script_parse_error,
        test_engine_meta_invalid,
        test_engine_agent_text_and_events,
        test_engine_parallel_pipeline,
        test_engine_agent_cap,
        test_engine_cancel,
        test_engine_result_unserializable,
        test_engine_schema_structured,
        test_engine_unsupported_option,
        test_engine_args_global,
        test_engine_cancelled_start_signal,
        test_invariant_failures,
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
    print(f"workflow: {'全部通过' if failures == 0 else f'{failures} 个失败'}")
    return failures


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(_main()) else 0)

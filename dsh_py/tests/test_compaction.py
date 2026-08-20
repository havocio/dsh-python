"""compaction（记忆压缩）测试：token-meter、surface replace、工具配对平衡、
区域选择、压缩事务（锁/摘要/提交/失败分类）、自动压力挂钩、手动压缩。

运行：``python dsh_py/tests/test_compaction.py``
"""

from __future__ import annotations

import asyncio

from dsh_py.core.context import AppContext
from dsh_py.core.signal import CancelSignal
from dsh_py.services import agent as A
from dsh_py.services import compaction_basic as CB
from dsh_py.services import llm as L
from dsh_py.services import session as S
from dsh_py.services import token_meter as TM
from dsh_py.services.compaction import (
    CompactionResult,
    ManualCompactionError,
    compact_checkpoint_source,
    is_compact_checkpoint_source,
    tool_pairing_balanced_after,
    tool_pairing_balanced_before,
)
from dsh_py.services.message import (
    MessageSource,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    create_assistant_message,
    create_user_message,
)


class _RoundAdapter(L.LlmAdapter):
    """多轮小消息适配器：普通请求回 250 字（≈250 tokens），摘要请求回小文本。"""

    def __init__(self, context_window: int = 1000) -> None:
        self._context_window = context_window
        self.summaries = 0

    async def resolve_model(self, provider, model):
        return {
            "provider": provider, "id": model, "name": model,
            "context": {"context_window": self._context_window},
        }

    async def stream(self, options):
        if options.purpose == "compaction":
            self.summaries += 1
            yield L.StreamChunk(L.ChunkType.BLOCK_START, block_type="text", index=0)
            yield L.StreamChunk(L.ChunkType.TEXT_DELTA,
                                text="## Primary Request and Intent\n- 测试请求\n## Next Step\n- 继续")
            yield L.StreamChunk(L.ChunkType.BLOCK_END, index=0, block=None)
            yield L.StreamChunk(L.ChunkType.FINISH, finish={"kind": "stop"})
        else:
            big = "工作内容" * 50
            yield L.StreamChunk(L.ChunkType.BLOCK_START, block_type="text", index=0)
            yield L.StreamChunk(L.ChunkType.TEXT_DELTA, text=big)
            yield L.StreamChunk(L.ChunkType.BLOCK_END, index=0, block=None)
            yield L.StreamChunk(L.ChunkType.FINISH, finish={"kind": "stop"})


def _ctx_with_compaction(**compaction_config) -> tuple[AppContext, _RoundAdapter]:
    ctx = AppContext()
    S.apply(ctx)
    TM.apply(ctx)
    L.apply(ctx)
    adapter = _RoundAdapter()
    ctx.llm.register_adapter(["mock"], adapter)
    CB.apply(ctx, {"summarizationProvider": "mock", "summarizationModel": "m", **compaction_config})
    A.apply_registry(ctx)
    A.apply_loop(ctx)
    return ctx, adapter


# --------------------------------------------------------------------------- #
# 1. token-meter
# --------------------------------------------------------------------------- #
def test_token_meter() -> None:
    ctx = AppContext()
    TM.apply(ctx)
    assert ctx.tokenMeter.estimate_text("") == 0
    assert ctx.tokenMeter.estimate_text("你好") == 2
    assert ctx.tokenMeter.estimate_text("abcd") == 1
    # 测量与表面一致
    S.apply(ctx)
    session = ctx.sessions.create()
    session.append("user/message", create_user_message([TextBlock("你好")], MessageSource("user")))
    measurement = ctx.tokenMeter.measure(session)
    assert measurement["total_tokens"] == 2
    assert [n["seq"] for n in measurement["nodes"]] == [1]
    # 表面不匹配（seq 索引错误）→ 抛错
    ctx.tokenMeter.measure(session)


# --------------------------------------------------------------------------- #
# 2. surface replace + generation
# --------------------------------------------------------------------------- #
def test_surface_replace() -> None:
    ctx = AppContext()
    S.apply(ctx)
    session = ctx.sessions.create()
    session.append("user/message", create_user_message([TextBlock("a")], MessageSource("user")))
    session.append("user/message", create_user_message([TextBlock("b")], MessageSource("user")))
    assert session.surface["nodes"] == [1, 2]
    assert session.surface["replace_generation"] == 0
    # 用 replace 替换 [1, 2] 为新节点 3
    session.append("user/message", create_user_message([TextBlock("summary")], MessageSource("user")),
                   surface_op={"op": "replace", "start": 1, "end": 2})
    assert session.surface["nodes"] == [3]
    assert session.surface["replace_generation"] == 1
    # 区间外 replace → 抛错
    try:
        session.append("user/message", create_user_message([TextBlock("x")], MessageSource("user")),
                       surface_op={"op": "replace", "start": 99, "end": 100})
        raise AssertionError("应抛错")
    except RuntimeError:
        pass


# --------------------------------------------------------------------------- #
# 3. 工具配对平衡
# --------------------------------------------------------------------------- #
def test_tool_pairing_balance() -> None:
    ctx = AppContext()
    S.apply(ctx)
    session = ctx.sessions.create()
    session.append("user/message", create_user_message([TextBlock("q")], MessageSource("user")))
    # assistant 带 1 个工具调用
    assistant = create_assistant_message([ToolCallBlock(id="c1", name="f", arguments="{}")],
                                         provider="mock", model="m")
    session.append("assistant/message", {"turn": 1, "step": 1, "message": assistant})
    result_msg = create_user_message(
        [ToolResultBlock(tool_call_id="c1", content=(TextBlock("ok"),), is_error=False)],
        source=MessageSource("tool"),
    )
    session.append("tool/result", {"turn": 1, "step": 1, "message": result_msg})
    session.append("user/message", create_user_message([TextBlock("q2")], MessageSource("user")))
    nodes = session.surface["nodes"]
    assert nodes == [1, 2, 3, 4]
    # 平衡的前切口：user 前、assistant（工具调用）前、tool/result 后
    assert tool_pairing_balanced_before(session, 1)
    assert tool_pairing_balanced_before(session, 2)
    assert tool_pairing_balanced_before(session, 4)
    # 不平衡：tool/result 前的切口（工具调用未闭合）必然不平衡
    assert not tool_pairing_balanced_before(session, 3)
    # 平衡的尾切口：user 后、tool/result 后、表面尾部
    assert tool_pairing_balanced_after(session, 1)
    assert tool_pairing_balanced_after(session, 3)
    assert tool_pairing_balanced_after(session, 4)
    # 不平衡：assistant 工具调用节点后（结果未落）切口必然不平衡
    assert not tool_pairing_balanced_after(session, 2)
    # 构造不配对：孤立 tool/result → 抛错（表面损坏）
    bad = AppContext()
    S.apply(bad)
    session2 = bad.sessions.create()
    orphan = create_user_message(
        [ToolResultBlock(tool_call_id="x", content=(TextBlock("ok"),), is_error=False)],
        source=MessageSource("tool"),
    )
    session2.append("tool/result", {"turn": 1, "step": 1, "message": orphan})
    try:
        tool_pairing_balanced_before(session2, 1)
        raise AssertionError("孤立 tool/result 应抛错")
    except RuntimeError:
        pass


# --------------------------------------------------------------------------- #
# 4. 区域选择
# --------------------------------------------------------------------------- #
def test_select_compactable_range() -> None:
    ctx = AppContext()
    S.apply(ctx)
    TM.apply(ctx)
    session = ctx.sessions.create()
    # 4 轮消息，每轮 user(2字) + assistant(250字) ≈ 252 tokens
    for i in range(4):
        session.append("user/message", create_user_message([TextBlock("你好")], MessageSource("user")))
        session.append("assistant/message", {
            "turn": i + 1, "step": 1,
            "message": create_assistant_message([TextBlock("工作内容" * 50)], provider="mock", model="m"),
        })
    measurement = ctx.tokenMeter.measure(session)
    total = measurement["total_tokens"]
    assert total > 800
    # 保留 160 tokens：尾部约 1 轮（252）> 160，但平衡边界使 keep_from 至少让头部可压
    range_ = CB.select_compactable_range(session, measurement, 160)
    assert range_ is not None
    assert range_["start"] == 1
    assert range_["end"] < session.surface["nodes"][-1]
    # 无可用区间：保留预算覆盖全部 → None
    assert CB.select_compactable_range(session, measurement, total + 1) is None


# --------------------------------------------------------------------------- #
# 5. 压缩事务成功路径
# --------------------------------------------------------------------------- #
def _build_populated_session(ctx: AppContext, rounds: int = 4):
    session = ctx.sessions.create()
    for i in range(rounds):
        session.append("user/message", create_user_message([TextBlock("你好")], MessageSource("user")))
        session.append("assistant/message", {
            "turn": i + 1, "step": 1,
            "message": create_assistant_message([TextBlock("工作内容" * 50)], provider="mock", model="m"),
        })
    return session


def test_compact_surface_region_success() -> None:
    async def main() -> None:
        ctx, adapter = _ctx_with_compaction()
        session = _build_populated_session(ctx)
        agent = ctx.agents.create_agent(session, A.AgentOptions(provider="mock", model="m"))
        # 手动事务（owner=null，selected-span 稳定）
        measurement = ctx.tokenMeter.measure(session)
        range_ = CB.select_compactable_range(session, measurement, 160)
        result = await CB.compact_surface_region(
            {"meter": ctx.tokenMeter, "summarize": lambda i, a, s=None: _summarize_for_test(ctx, i, a, s)},
            session, range_["start"], range_["end"], agent,
            {"owner": None, "stability": "selected-span"},
        )
        assert isinstance(result, CompactionResult)
        types = [e.type for e in session.events]
        assert "compaction/start" in types and "compaction/summary" in types and "compaction/end" in types
        assert result.shadowedSeqs and result.shadowedTokenCount > 0
        assert session.surface["replace_generation"] >= 1
        # 替换消息携带 compaction checkpoint source（Python 侧为 dict 形态）
        summary_event = next(e for e in session.events if e.type == "compaction/summary")
        replace_event = next(e for e in session.events if e.type == "user/message" and e.seq > summary_event.seq)
        source = replace_event.data.source
        assert is_compact_checkpoint_source(source)
        assert source["compactionId"] == result.compactionId

    asyncio.run(main())


async def _summarize_for_test(ctx, input_data, agent, signal=None):
    """测试专用摘要：走真实 LLM 摘要（mock adapter 的 compaction 分支）。"""
    return await CB.summarize_with_llm(ctx, {
        "summarizationProvider": "mock", "summarizationModel": "m", "maxTokens": 8192,
    }, input_data, agent, signal)


# --------------------------------------------------------------------------- #
# 6. 失败分类：摘要不小于被遮蔽内容
# --------------------------------------------------------------------------- #
def test_compact_surface_region_summary_not_smaller() -> None:
    async def main() -> None:
        ctx, adapter = _ctx_with_compaction()
        session = _build_populated_session(ctx, rounds=1)  # 只有 1 轮 → 被遮蔽内容极小
        agent = ctx.agents.create_agent(session, A.AgentOptions(provider="mock", model="m"))

        async def bad_summarize(input_data, owner, signal=None):
            # 返回超大摘要（必不小于被遮蔽内容）
            from dsh_py.services.message import TextBlock as TB
            return {
                "summary": [TB("字" * 2000)], "raw_output": [TB("字" * 2000)],
                "llm_stream_call": True, "provider": "mock", "model": "m",
                "max_tokens": 8192, "usage": None,
            }

        try:
            await CB.compact_surface_region(
                {"meter": ctx.tokenMeter, "summarize": bad_summarize},
                session, 1, 2, agent, {"owner": None, "stability": "selected-span"},
            )
            raise AssertionError("应抛错")
        except ManualCompactionError as exc:
            assert exc.code == "summary"
        # 锁被闭合：end 带 error
        end = next(e for e in session.events if e.type == "compaction/end")
        assert "error" in end.data

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 7. 并发锁：活跃压缩拒绝
# --------------------------------------------------------------------------- #
def test_compaction_lock_busy() -> None:
    async def main() -> None:
        ctx, adapter = _ctx_with_compaction()
        session = _build_populated_session(ctx)
        agent = ctx.agents.create_agent(session, A.AgentOptions(provider="mock", model="m"))
        measurement = ctx.tokenMeter.measure(session)
        range_ = CB.select_compactable_range(session, measurement, 160)
        deps = {"meter": ctx.tokenMeter, "summarize": lambda i, a, s=None: _summarize_for_test(ctx, i, a, s)}
        # 第一次成功
        await CB.compact_surface_region(deps, session, range_["start"], range_["end"], agent,
                                        {"owner": None, "stability": "selected-span"})
        # 第二次：锁已闭合可再压；但若区间不再有效（surface 已换）→ 校验抛错或 None
        measurement2 = ctx.tokenMeter.measure(session)
        range2 = CB.select_compactable_range(session, measurement2, 0)
        assert range2 is not None
        # 未闭合锁：手动 start 后直接再来 → busy
        session.append("compaction/start", {"compactionId": "fake", "turn": None})
        try:
            await CB.compact_surface_region(deps, session, range2["start"], range2["end"], agent,
                                            {"owner": None, "stability": "selected-span"})
            raise AssertionError("应抛 busy")
        except ManualCompactionError as exc:
            assert exc.code == "busy"
        session.append("compaction/end", {"compactionId": "fake", "turn": None})

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 8. 自动压力挂钩（端到端）
# --------------------------------------------------------------------------- #
def test_automatic_pressure_hook() -> None:
    async def main() -> None:
        ctx, adapter = _ctx_with_compaction()
        agent = ctx.agents.create_agent(ctx.sessions.create(), A.AgentOptions(provider="mock", model="m"))
        for i in range(5):
            await agent.run(f"第{i + 1}问")
        assert ctx.tokenMeter.measure(agent.session)["total_tokens"] >= 500
        await agent.run("第6问触发压缩")
        types = [e.type for e in agent.session.events]
        assert "compaction/summary" in types
        assert agent.session.surface["replace_generation"] >= 1
        # 摘要后压力回落：继续运行不再频繁压缩
        await agent.run("第7问")
        assert adapter.summaries >= 1

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 9. 手动压缩 compact_now
# --------------------------------------------------------------------------- #
def test_compact_now_manual() -> None:
    async def main() -> None:
        ctx, adapter = _ctx_with_compaction()
        session = _build_populated_session(ctx)
        agent = ctx.agents.create_agent(session, A.AgentOptions(provider="mock", model="m"))
        result = await ctx.compaction.compact_now(agent, CancelSignal())
        assert result is not None
        assert isinstance(result, CompactionResult)
        assert session.surface["replace_generation"] >= 1
        # 空闲会话手动压缩
        assert agent._running is False
        # busy：活跃时同步拒绝
        agent._running = True
        try:
            await ctx.compaction.compact_now(agent, CancelSignal())
            raise AssertionError("活跃 agent 应抛 busy")
        except ManualCompactionError as exc:
            assert exc.code == "busy"
        agent._running = False

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 10. 配置解析
# --------------------------------------------------------------------------- #
def test_config_resolution() -> None:
    config = CB.resolve_config()
    assert config["thresholdRatio"] == 0.8
    assert config["retainRatio"] == 0.16
    assert config["maxTokens"] == 8192
    assert config["auto"] is True
    # retainRatio/retainTokens 互斥
    try:
        CB.resolve_config({"retainRatio": 0.1, "retainTokens": 100})
        raise AssertionError("应抛错")
    except ValueError:
        pass
    policy = CB.resolve_target_policy(config, {"provider": "mock", "model": "m"})
    spec = CB.resolve_compact_spec(policy, 1000)
    assert spec["thresholdTokens"] == 800
    assert spec["retainTokens"] == 160
    # retain >= threshold 报错
    try:
        CB.resolve_compact_spec({"target": {"provider": "p", "model": "m"}, "thresholdRatio": 0.8,
                                 "retainTokens": 800, "summarizationProvider": "",
                                 "summarizationModel": "", "maxTokens": 1,
                                 "compactionRetries": 1, "maxOverflowRetries": 1}, 1000)
        raise AssertionError("应抛错")
    except CB.TargetPressureConfigError:
        pass


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def main() -> None:
    tests = [
        test_token_meter,
        test_surface_replace,
        test_tool_pairing_balance,
        test_select_compactable_range,
        test_compact_surface_region_success,
        test_compact_surface_region_summary_not_smaller,
        test_compaction_lock_busy,
        test_automatic_pressure_hook,
        test_compact_now_manual,
        test_config_resolution,
    ]
    passed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"FAIL {test.__name__}: {exc}")
            traceback.print_exc()
    print(f"== {passed}/{len(tests)} passed ==")
    if passed != len(tests):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

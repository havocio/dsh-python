"""tool-result-pruner 测试：字符预算解析、头/中/尾裁剪、会话表面单节点替换、
shadow-price 事件、与 compaction 的集成（pressure 路径先修剪）。

运行：``python dsh_py/tests/test_tool_result_pruner.py``
"""

from __future__ import annotations

import asyncio

from dsh_py.core.context import AppContext
from dsh_py.services import compaction_basic as CB
from dsh_py.services import llm as L
from dsh_py.services import session as S
from dsh_py.services import token_meter as TM
from dsh_py.services import tool_result_pruner as TRP
from dsh_py.services.message import (
    MessageSource,
    TextBlock,
    ToolResultBlock,
    create_assistant_message,
    create_user_message,
)


def _ctx(prune_config: dict | None = None, **compaction_config) -> AppContext:
    ctx = AppContext()
    S.apply(ctx)
    TM.apply(ctx)
    TRP.apply(ctx, prune_config)
    return ctx


# --------------------------------------------------------------------------- #
# 1. 配置解析
# --------------------------------------------------------------------------- #
def test_config_resolution() -> None:
    config = TRP.resolve_config()
    assert config == TRP.DEFAULTS
    assert TRP.PRUNE_MARKER.startswith("\n\n[...")
    # 未知键拒绝
    try:
        TRP.resolve_config({"bogus": 1})
        raise AssertionError("未知键应抛错")
    except ValueError:
        pass
    # head+marker+tail 超过 threshold 拒绝
    try:
        TRP.resolve_config({"thresholdChars": 100, "headChars": 200})
        raise AssertionError("预算超限应抛错")
    except ValueError:
        pass
    # 非负/正整数校验
    try:
        TRP.resolve_config({"headChars": -1})
        raise AssertionError("负值应抛错")
    except ValueError:
        pass


# --------------------------------------------------------------------------- #
# 2. 裁剪（measure / prune）
# --------------------------------------------------------------------------- #
def test_prune_content() -> None:
    ctx = _ctx({"thresholdChars": 100, "headChars": 40, "tailChars": 20})
    pruner = ctx.toolResultPruner
    # 预算内 → None
    small = [TextBlock("短文本")]
    assert pruner.prune_content(small) is None
    # 超预算 → 头/尾保留 + marker
    big = [TextBlock("x" * 200)]
    pruned = pruner.prune_content(big)
    assert pruned is not None
    text = pruned[0].text
    assert text.startswith("x" * 40)
    assert text.endswith("x" * 20)
    assert TRP.PRUNE_MARKER in text
    # 更小且不超阈值
    assert len(text) < 200
    assert pruner.measure_content(pruned) <= 100
    # code point 计数（CJK/emoji 不切分代理对）
    assert TRP.code_point_length("你好👋") == 3


# --------------------------------------------------------------------------- #
# 3. 会话修剪：单节点替换 + shadow-price
# --------------------------------------------------------------------------- #
def _tool_result_message(text: str) -> object:
    return create_user_message(
        [ToolResultBlock(tool_call_id="c1", content=(TextBlock(text),), is_error=False)],
        source=MessageSource("tool"),
    )


def test_prune_session() -> None:
    ctx = _ctx({"thresholdChars": 100, "headChars": 40, "tailChars": 20})
    session = ctx.sessions.create()
    session.append("user/message", create_user_message([TextBlock("q")], MessageSource("user")))
    session.append("assistant/message", {
        "turn": 1, "step": 1,
        "message": create_assistant_message([TextBlock("thinking")], provider="mock", model="m"),
    })
    session.append("tool/result", {"turn": 1, "step": 1, "message": _tool_result_message("d" * 500)})
    session.append("user/message", create_user_message([TextBlock("q2")], MessageSource("user")))
    nodes_before = list(session.surface["nodes"])
    result = ctx.toolResultPruner.prune_session(session)
    assert len(result["pruned"]) == 1
    assert result["charsRemoved"] > 0
    entry = result["pruned"][0]
    assert entry["callId"] == "c1"
    assert entry["charsBefore"] == 500
    # surface 单节点替换：节点数不变，原 seq 被新 seq 替换
    assert len(session.surface["nodes"]) == len(nodes_before)
    assert entry["originalSeq"] in nodes_before
    assert entry["replacementSeq"] not in nodes_before
    assert entry["replacementSeq"] in session.surface["nodes"]
    # compaction/prune shadow-price 事件紧邻前置
    events = session.events
    prune_event = next(e for e in events if e.type == "compaction/prune")
    assert prune_event.data["shadowedSeqs"] == [entry["originalSeq"]]
    assert prune_event.data["shadowedTokenCount"] > 0
    # 替换后的 tool/result 保留其余字段（turn/step），内容被裁剪
    replacement = next(e for e in events if e.seq == entry["replacementSeq"])
    assert replacement.data["turn"] == 1 and replacement.data["step"] == 1
    new_block = replacement.data["message"].content[0]
    assert len(new_block.content[0].text) < 100
    # 再次修剪：已达标 → 无新替换
    again = ctx.toolResultPruner.prune_session(session)
    assert again["pruned"] == []


# --------------------------------------------------------------------------- #
# 4. 与 compaction 集成：pressure 路径先修剪
# --------------------------------------------------------------------------- #
class _RoundAdapter(L.LlmAdapter):
    async def resolve_model(self, provider, model):
        return {"provider": provider, "id": model, "name": model,
                "context": {"context_window": 1000}}

    async def stream(self, options):
        if options.purpose == "compaction":
            yield L.StreamChunk(L.ChunkType.BLOCK_START, block_type="text", index=0)
            yield L.StreamChunk(L.ChunkType.TEXT_DELTA, text="## Primary Request and Intent\n- 测试\n## Next Step\n- 继续")
            yield L.StreamChunk(L.ChunkType.BLOCK_END, index=0, block=None)
            yield L.StreamChunk(L.ChunkType.FINISH, finish={"kind": "stop"})
        else:
            yield L.StreamChunk(L.ChunkType.BLOCK_START, block_type="text", index=0)
            yield L.StreamChunk(L.ChunkType.TEXT_DELTA, text="回复" * 120)  # 240 字
            yield L.StreamChunk(L.ChunkType.BLOCK_END, index=0, block=None)
            yield L.StreamChunk(L.ChunkType.FINISH, finish={"kind": "stop"})


def test_pruner_inside_compact_if_needed() -> None:
    async def main() -> None:
        ctx = _ctx({"thresholdChars": 200, "headChars": 80, "tailChars": 40})
        L.apply(ctx)
        ctx.llm.register_adapter(["mock"], _RoundAdapter())
        # 构造带超预算工具结果 + 超阈值 token 的会话
        from dsh_py.services import agent as A
        A.apply_registry(ctx)
        A.apply_loop(ctx)
        CB.apply(ctx, {"summarizationProvider": "mock", "summarizationModel": "m"})
        agent = ctx.agents.create_agent(ctx.sessions.create(), A.AgentOptions(provider="mock", model="m"))
        # 手动灌入：多轮小消息 + 一个大工具结果 → 触发 pressure
        session = agent.session
        session.request_header = {"config": {"provider": "mock", "model": "m"}, "system": None, "tools": None}
        for i in range(3):
            session.append("user/message", create_user_message([TextBlock("你好")], MessageSource("user")))
            session.append("assistant/message", {
                "turn": i + 1, "step": 1,
                "message": create_assistant_message([TextBlock("工作内容" * 50)], provider="mock", model="m"),
            })
        session.append("user/message", create_user_message([TextBlock("查数据")], MessageSource("user")))
        session.append("assistant/message", {
            "turn": 4, "step": 1,
            "message": create_assistant_message([TextBlock("查询中")], provider="mock", model="m"),
        })
        session.append("tool/result", {"turn": 4, "step": 1,
                                       "message": _tool_result_message("d" * 4000)})
        # 挂载 pruner 后 compact_if_needed：pressure 路径先修剪再测
        result = await ctx.compaction.compact_if_needed(agent, "pressure", agent._signal)
        assert result is not None or ctx.tokenMeter.measure(session)["total_tokens"] < 800
        # 修剪确实发生了：出现 compaction/prune 事件
        types = [e.type for e in session.events]
        assert "compaction/prune" in types

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def main() -> None:
    tests = [
        test_config_resolution,
        test_prune_content,
        test_prune_session,
        test_pruner_inside_compact_if_needed,
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

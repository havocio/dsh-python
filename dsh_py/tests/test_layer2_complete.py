"""第 2 层补完综合测试：llm attribution/brand/topology、session projection/
projection-cache/stats/session-query、agent 声明式校验/resume/cancel 三源融合。

运行：``python dsh_py/tests/test_layer2_complete.py``
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from dsh_py.core.context import AppContext
from dsh_py.core.signal import CancelSignal
from dsh_py.services import agent as A
from dsh_py.services import llm as L
from dsh_py.services import projection as P
from dsh_py.services import projection_cache as PC
from dsh_py.services import session as S
from dsh_py.services import session_query as SQ
from dsh_py.services import session_stats as SS
from dsh_py.services.attribution import APP_IDENTITY, attribution_headers, user_agent
from dsh_py.services.brand import CallId, MessageId, ProviderRequestId
from dsh_py.services.session_persistence import JsonlSessionPersistence
from dsh_py.services.projection import ProjectionDefinition


class _EchoAdapter(L.LlmAdapter):
    """冒烟用回显适配器：固定文本 + 用量。"""

    def __init__(self, text: str = "你好 郑州 你好") -> None:
        self._text = text

    async def stream(self, options):
        yield L.StreamChunk(L.ChunkType.BLOCK_START, block_type="text", index=0)
        yield L.StreamChunk(L.ChunkType.TEXT_DELTA, text=self._text)
        yield L.StreamChunk(L.ChunkType.BLOCK_END, index=0, block=None)
        yield L.StreamChunk(L.ChunkType.USAGE, usage={"outputTokens": 7})
        yield L.StreamChunk(L.ChunkType.FINISH, finish={"kind": "stop"})


# --------------------------------------------------------------------------- #
# 1. attribution / brand
# --------------------------------------------------------------------------- #
def test_attribution() -> None:
    ua = user_agent()
    assert ua == f"{APP_IDENTITY.product}/{APP_IDENTITY.version} (+{APP_IDENTITY.url})"
    assert ua.startswith("deepseek-harness/")
    headers = attribution_headers()
    assert headers["user-agent"] == ua
    # 白标覆盖：传入自定义身份而非抑制归属
    custom = attribution_headers(type("I", (), {"product": "my-app", "version": "9", "url": "https://x.dev"})())
    assert custom["user-agent"] == "my-app/9 (+https://x.dev)"


def test_brand() -> None:
    assert MessageId("m") == "m"
    assert CallId("c") == "c"
    assert ProviderRequestId("r") == "r"


# --------------------------------------------------------------------------- #
# 2. llm topology：适配器拓扑通知 + configurable providers
# --------------------------------------------------------------------------- #
def test_topology_adapters_updated() -> None:
    ctx = AppContext()
    L.apply(ctx)
    seen: list[str] = []
    ctx.on("llm/adapters-updated", lambda: seen.append("upd"))
    ctx.llm.register_adapter(["a", "b"], _EchoAdapter())
    assert len(seen) == 1
    handle = ctx.llm.register_adapter(["c"], _EchoAdapter())
    assert len(seen) == 2
    handle.replace(["d", "e"])
    assert len(seen) == 3
    assert {p.id for p in ctx.llm.list_providers()} == {"a", "b", "d", "e"}
    handle()
    assert len(seen) == 4
    assert {p.id for p in ctx.llm.list_providers()} == {"a", "b"}
    # 监听器失败被 contained：不影响提交，也不阻断后续监听器
    bad = ctx.on("llm/adapters-updated", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    ctx.llm.register_adapter(["z"], _EchoAdapter())
    assert {p.id for p in ctx.llm.list_providers()} >= {"z"}


def test_topology_configurable_providers() -> None:
    ctx = AppContext()
    L.apply(ctx)
    handle = ctx.llm.register_configurable_providers([
        {"provider": "p1", "displayName": "P1", "settingsNs": "ns1"},
        {"provider": "p2", "displayName": "P2", "settingsNs": "ns2"},
    ])
    assert [e["provider"] for e in ctx.llm.list_configurable_providers()] == ["p1", "p2"]
    try:
        ctx.llm.register_configurable_providers([{"provider": "p1", "displayName": "X", "settingsNs": "x"}])
        raise AssertionError("should reject duplicate configurable provider")
    except RuntimeError:
        pass
    handle.replace([{"provider": "p3", "displayName": "P3", "settingsNs": "ns3"}])
    assert [e["provider"] for e in ctx.llm.list_configurable_providers()] == ["p3"]
    handle()
    assert ctx.llm.list_configurable_providers() == []


# --------------------------------------------------------------------------- #
# 3. session projection：注册 / 驱动 / 快照 / 检查点 / 恢复
# --------------------------------------------------------------------------- #
def test_projection_drive_and_snapshot() -> None:
    ctx = AppContext()
    S.apply(ctx)
    P.apply(ctx)
    session = ctx.sessions.create()

    def word_counter() -> ProjectionDefinition:
        return ProjectionDefinition(
            key="wordCount",
            schema=None,  # 无 schema：validate 语义退化为透传
            init=lambda: {"words": 0, "seen": []},
            apply=lambda state, event: state if event.type != "user/message"
            else {"words": state["words"] + 1, "seen": [*state["seen"], event.seq]},
            view=lambda state: {"words": state["words"], "seen": state["seen"]},
            state_version=1,
        )

    # 带 schema 的严格统计（用 stats 的 schema 验证 view 输出）
    def custom_schema() -> ProjectionDefinition:
        import dsh_py.core.schema as z
        return ProjectionDefinition(
            key="messages",
            schema=z.object({"count": z.integer()}),
            init=lambda: {"count": 0},
            apply=lambda state, event: {**state, "count": state["count"] + 1}
            if event.type == "user/message" else state,
            view=lambda state: {"count": state["count"]},
        )

    changed: list[tuple] = []
    off = ctx.sessionProjections.on_changed(lambda session, key, value, seq: changed.append((key, value, seq)))
    dispose = ctx.sessionProjections.register(word_counter())
    ctx.sessionProjections.register(custom_schema())
    from dsh_py.services.message import Message, TextBlock, create_user_message, MessageSource
    session.append("user/message", create_user_message([TextBlock("hi")], MessageSource("user")))
    session.append("user/message", create_user_message([TextBlock("there")], MessageSource("user")))
    snap = ctx.sessionProjections.snapshot(session)
    assert snap["as_of_seq"] == 1  # 两条事件后 seq=2，水印 = seq-1
    assert snap["values"]["wordCount"]["words"] == 2
    assert snap["values"]["messages"]["count"] == 2
    assert any(key == "wordCount" for key, _, _ in changed)
    # checkpoint / view_checkpoint / restore（尾部重放：checkpoint 早于日志末端）
    cp = ctx.sessionProjections.checkpoint(session)
    assert cp["wordCount"]["ver"] == 1 and cp["wordCount"]["seq"] == 2
    viewed = ctx.sessionProjections.view_checkpoint(cp)
    assert viewed["wordCount"]["words"] == 2
    stale_cp = {"wordCount": {"ver": 1, "seq": 1, "val": {"words": 1, "seen": [1]}}}
    floor = ctx.sessionProjections.restore_floor(stale_cp)
    assert floor == 0  # messages 单元无行 → 起点被拉低到 0（全量重折）
    tail = [e for e in session.events if e.seq >= floor]
    restored = ctx.sessionProjections.restore(stale_cp, tail, floor)
    assert restored["snapshot"]["values"]["wordCount"]["words"] == 2
    assert restored["snapshot"]["as_of_seq"] == 2
    # 全量重折（无 checkpoint 的冷读兜底）
    full = ctx.sessionProjections.restore({}, session.events, 0)
    assert full["snapshot"]["values"]["wordCount"]["words"] == 2
    # 日志收缩（崩溃修复截断）检测：行声称越过日志末端 → 拒绝
    try:
        ctx.sessionProjections.restore(cp, [], 2)
        raise AssertionError("收缩日志应拒绝陈旧行")
    except RuntimeError:
        pass
    # 卸载后键消失
    dispose()
    assert "wordCount" not in ctx.sessionProjections.snapshot(session)["values"]


# --------------------------------------------------------------------------- #
# 4. session stats：纯 fold 指标
# --------------------------------------------------------------------------- #
def test_session_stats_projection() -> None:
    ctx = AppContext()
    S.apply(ctx)
    P.apply(ctx)
    SS.apply(ctx)
    session = ctx.sessions.create()
    t0 = 100.0
    session.append("turn/start", {"turn": 1})
    session.append("step/start", {"turn": 1, "step": 1})
    session.append("assistant/chunk", {"turn": 1, "step": 1, "chunk": L.StreamChunk(L.ChunkType.TEXT_DELTA, text="a")})
    session.append("assistant/message", {
        "turn": 1, "step": 1,
        "message": object(),
        "usage": {"outputTokens": 10},
    })
    session.append("tool/call", {"turn": 1, "step": 1, "callId": "c1", "name": "f", "arguments": "{}"})
    session.append("tool/result", {"turn": 1, "step": 1, "callId": "c1",
                                   "message": object()})
    session.append("step/end", {"turn": 1, "step": 1})
    session.append("turn/end", {"turn": 1})
    stats = ctx.sessionProjections.snapshot(session)["values"]["sessionStats"]
    assert stats["turns"] == 1
    assert stats["steps"] == 1
    assert stats["llmMs"] >= 0
    assert stats["ttftSteps"] == 1
    assert stats["decodeTokens"] == 10
    assert stats["toolMs"] >= 0


# --------------------------------------------------------------------------- #
# 5. projection-cache：写后读 / 冷读阶梯 / 身份绑定
# --------------------------------------------------------------------------- #
def test_projection_cache_roundtrip() -> None:
    ctx = AppContext()
    S.apply(ctx)
    P.apply(ctx)
    SS.apply(ctx)
    tmp = tempfile.mkdtemp()
    back = JsonlSessionPersistence(ctx, os.path.join(tmp, "sessions"))
    ctx.sessions.attach_persistence(back)
    PC.apply(ctx, {"writeEveryEvents": 2, "path": os.path.join(tmp, "cache.json")})
    session = ctx.sessions.create()
    session.append("turn/start", {"turn": 1})
    session.append("step/start", {"turn": 1, "step": 1})
    session.append("step/end", {"turn": 1, "step": 1})
    session.append("turn/end", {"turn": 1})
    ctx.sessionProjectionCache.write(session)
    cached = ctx.sessionProjectionCache.cached_snapshot(session.header)
    assert cached is not None
    assert cached["values"]["sessionStats"]["turns"] == 1
    cold = ctx.sessionProjectionCache.cold_snapshot(session.header.id)
    assert cold["values"]["sessionStats"]["steps"] == 1
    # turn/end 是强制写点：事件驱动也产生缓存行
    session2 = ctx.sessions.create()
    session2.append("turn/start", {"turn": 1})
    session2.append("step/start", {"turn": 1, "step": 1})
    session2.append("step/end", {"turn": 1, "step": 1})
    session2.append("turn/end", {"turn": 1})
    assert ctx.sessionProjectionCache.cached_snapshot(session2.header) is not None
    ctx.sessionProjectionCache.dispose()


def test_projection_cache_identity_binding() -> None:
    """同一 id 重建的会话（不同 created_at）不得读到旧记录。"""
    ctx = AppContext()
    S.apply(ctx)
    P.apply(ctx)
    SS.apply(ctx)
    tmp = tempfile.mkdtemp()
    back = JsonlSessionPersistence(ctx, os.path.join(tmp, "sessions"))
    ctx.sessions.attach_persistence(back)
    PC.apply(ctx, {"path": os.path.join(tmp, "cache.json")})
    session = ctx.sessions.create()
    session.append("turn/end", {"turn": 1})
    ctx.sessionProjectionCache.write(session)
    sid = session.header.id
    # 重新 create（新生命周期）：header.created_at 不同 → 缓存行被拒
    again = ctx.sessions.prepare(session_id=sid)
    ctx.sessions.enter(again)
    assert ctx.sessionProjectionCache.cached_snapshot(again.header) is None
    ctx.sessionProjectionCache.dispose()


# --------------------------------------------------------------------------- #
# 6. session-query：读取 / 过滤 / 关键词检索
# --------------------------------------------------------------------------- #
def test_session_query() -> None:
    ctx = AppContext()
    S.apply(ctx)
    SQ.apply(ctx)
    session = ctx.sessions.create()
    from dsh_py.services.message import TextBlock, create_user_message, MessageSource
    session.append("user/message", create_user_message([TextBlock("今天郑州的天气如何")], MessageSource("user")))
    session.append("turn/end", {"turn": 1})
    # read 窗口
    records = ctx.sessionQuery.read(session, start_seq=1, limit=1)
    assert len(records) == 1 and records[0]["type"] == "user/message"
    # filter
    filtered = ctx.sessionQuery.filter_events(session, event_type="turn/end")
    assert len(filtered) == 1
    # search（CJK 单字 + 交集）
    hits = ctx.sessionQuery.search(session, "郑州")
    assert len(hits) >= 1
    assert "郑州" in hits[0]["text"]
    assert ctx.sessionQuery.search(session, "不存在的词") == []
    # 增量索引：追加后无需重建
    session.append("user/message", create_user_message([TextBlock("北京的天")], MessageSource("user")))
    assert len(ctx.sessionQuery.search(session, "北京")) == 1


# --------------------------------------------------------------------------- #
# 7. 声明式 agents 校验 / resume / cancel 三源融合（端到端）
# --------------------------------------------------------------------------- #
def test_declarative_validation() -> None:
    ctx = AppContext()
    S.apply(ctx)
    A.apply_registry(ctx)
    A.apply_loop(ctx)
    # sessionId 与 resumeSessionId 互斥
    try:
        A.apply_loop(ctx, {"agents": [{"id": "x", "sessionId": "s1", "resumeSessionId": "s2"}]})
        raise AssertionError("互斥校验应抛错")
    except RuntimeError as exc:
        assert "互斥" in str(exc)
    # 重复精确身份
    try:
        A.apply_loop(ctx, {"agents": [{"id": "x", "sessionId": "s1"}, {"id": "y", "sessionId": "s1"}]})
        raise AssertionError("重复身份校验应抛错")
    except RuntimeError as exc:
        assert "重复" in str(exc)


def test_agent_resume_and_cancel_fusion() -> None:
    async def main() -> None:
        ctx = AppContext()
        S.apply(ctx)
        L.apply(ctx)
        ctx.llm.register_adapter(["mock"], _EchoAdapter())
        A.apply_registry(ctx)
        A.apply_loop(ctx)
        tmp = tempfile.mkdtemp()
        back = JsonlSessionPersistence(ctx, os.path.join(tmp, "sessions"))
        ctx.sessions.attach_persistence(back)
        agent = ctx.agents.create_agent(ctx.sessions.create(), A.AgentOptions(provider="mock", model="m"))
        await agent.run("郑州天气")
        sid = agent.session.header.id
        started: list[str] = []
        ctx.on("agent/session-start", lambda payload: started.append(payload["source"]))
        caller = CancelSignal()
        resumed = ctx.agentLoop.resume(sid, signal=caller)
        assert resumed._source == "resume"
        assert len(resumed.session.events) == len(agent.session.events)
        assert started[-1] == "resume"
        # 三源融合：调用方取消 → 融合信号传导
        caller.abort("caller-cancel")
        assert resumed._signal.aborted
        assert resumed._signal.reason == "caller-cancel"
        # 工厂 teardown 信号：AgentLoop 卸载 → 传导
        factory_sig = CancelSignal()
        a3 = ctx.agentLoop.create_agent(ctx.sessions.create(), A.AgentOptions(provider="mock", model="m"),
                                        signal=CancelSignal.any([factory_sig]))
        factory_sig.abort("teardown")
        assert a3._signal.aborted

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def main() -> None:
    tests = [
        test_attribution,
        test_brand,
        test_topology_adapters_updated,
        test_topology_configurable_providers,
        test_projection_drive_and_snapshot,
        test_session_stats_projection,
        test_projection_cache_roundtrip,
        test_projection_cache_identity_binding,
        test_session_query,
        test_declarative_validation,
        test_agent_resume_and_cancel_fusion,
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

"""session-query 完整版测试：语料库（live-preferred）、精确读、表面分类、
会话/事件过滤、事件窗口、全文检索分页游标、谱系与事件追踪、错误分类。

运行：``python dsh_py/tests/test_session_query_full.py``
"""

from __future__ import annotations

import os
import tempfile

from dsh_py.core.context import AppContext
from dsh_py.services import session as S
from dsh_py.services import session_query as SQ
from dsh_py.services.message import MessageSource, TextBlock, create_user_message
from dsh_py.services.session_persistence import JsonlSessionPersistence


def _ctx(persist: bool = True) -> tuple[AppContext, str]:
    ctx = AppContext()
    S.apply(ctx)
    SQ.apply(ctx)
    tmp = ""
    if persist:
        tmp = tempfile.mkdtemp()
        back = JsonlSessionPersistence(ctx, os.path.join(tmp, "sessions"))
        ctx.sessions.attach_persistence(back)
    return ctx, tmp


def _fill(session, rounds: int = 2, parent: str | None = None):
    if parent:
        session.header.parent_session = parent
    for i in range(rounds):
        session.append("user/message", create_user_message([TextBlock(f"郑州天气第{i + 1}轮")], MessageSource("user")))
        session.append("assistant/message", {"turn": i + 1, "step": 1,
                                             "message": create_user_message([TextBlock("工作内容" * 10)], MessageSource("user"))})
    session.append("turn/end", {"turn": rounds})


# --------------------------------------------------------------------------- #
# 1. 语料库 / 精确读
# --------------------------------------------------------------------------- #
def test_corpus_and_reads() -> None:
    ctx, _ = _ctx()
    s1 = ctx.sessions.create()
    _fill(s1)
    records = ctx.sessionQuery.list_sessions()
    assert len(records) == 1
    assert records[0]["live"] is True
    # read_session：完整日志
    loaded = ctx.sessionQuery.read_session(s1.header.id)
    assert loaded["session"].id == s1.header.id
    assert len(loaded["events"]) >= 5
    # read_surface：当前表面（含 compaction 替换后只含当前节点）
    surface = ctx.sessionQuery.read_surface(s1.header.id)
    assert surface["capturedThroughSeq"] == loaded["events"][-1].seq
    assert all(e.type in ("user/message", "assistant/message") for e in surface["events"])
    # 不存在 → 错误分类
    try:
        ctx.sessionQuery.read_session("nope")
        raise AssertionError("应抛 not found")
    except SQ.SessionQueryError as exc:
        assert exc.code == "SESSION_QUERY_SESSION_NOT_FOUND"


# --------------------------------------------------------------------------- #
# 2. 表面分类：替换后 shadowed
# --------------------------------------------------------------------------- #
def test_surface_classification() -> None:
    ctx, _ = _ctx(persist=False)
    session = ctx.sessions.create()
    _fill(session, rounds=1)
    # 做一次 surface 替换（模拟 compaction replace）
    old_nodes = list(session.surface["nodes"])
    session.append("user/message", create_user_message([TextBlock("summary")], MessageSource("user")),
                   surface_op={"op": "replace", "start": old_nodes[0], "end": old_nodes[-1]})
    records = ctx.sessionQuery.list_events(session.header.id)
    by_seq = {r["seq"]: r for r in records}
    # 被替换的旧节点 → shadowed；新节点 → current；结构性事件 → log-only
    for seq in old_nodes:
        assert by_seq[seq]["surface"] == "shadowed", f"seq {seq} 应为 shadowed"
    assert by_seq[session.events[-1].seq]["surface"] == "current"
    assert by_seq[1]["type"] == "user/message"  # 早期非表面事件（turn/end 前）不可能是 log-only 的 turn/start
    # turn/start 等结构性事件 → log-only
    for seq, r in by_seq.items():
        if r["type"] in ("turn/end", "turn/start"):
            assert r["surface"] == "log-only"


# --------------------------------------------------------------------------- #
# 3. 过滤：会话级 + 事件级
# --------------------------------------------------------------------------- #
def test_filters() -> None:
    ctx, _ = _ctx()
    a = ctx.sessions.create()
    b = ctx.sessions.create()
    _fill(a)
    _fill(b, parent=a.header.id)
    # 会话级：id / parent
    by_id = ctx.sessionQuery.filter_sessions([{"kind": "id", "values": [a.header.id]}])
    assert len(by_id) == 1 and by_id[0]["header"].id == a.header.id
    children = ctx.sessionQuery.filter_sessions([{"kind": "parent", "values": [a.header.id]}])
    assert len(children) == 1 and children[0]["header"].id == b.header.id
    # created-at 范围
    early = ctx.sessionQuery.filter_sessions([{"kind": "created-at", "to": a.header.created_at + 0.5}])
    assert len(early) == 2
    # 事件级：type（结构性事件不进入检索文档，用可检索类型）/ text
    users = ctx.sessionQuery.filter_session_events(a.header.id, [{"kind": "type", "values": ["user/message"]}])
    assert len(users) == 2
    zhengzhou = ctx.sessionQuery.filter_session_events(a.header.id, [{"kind": "text", "text": "郑州"}])
    assert len(zhengzhou) >= 1 and "郑州" in zhengzhou[0]["text"]
    # seq 范围
    seqs = ctx.sessionQuery.filter_session_events(a.header.id, [{"kind": "seq", "from": 1, "to": 2}])
    assert [d["seq"] for d in seqs] == [1, 2]
    # 非法过滤 → 错误分类
    try:
        ctx.sessionQuery.filter_sessions([{"kind": "bogus"}])
        raise AssertionError("应抛 invalid filter")
    except SQ.SessionQueryError as exc:
        assert exc.code == "SESSION_QUERY_INVALID_FILTER"


# --------------------------------------------------------------------------- #
# 4. 事件窗口
# --------------------------------------------------------------------------- #
def test_read_event_window() -> None:
    ctx, _ = _ctx(persist=False)
    session = ctx.sessions.create()
    _fill(session, rounds=2)
    # 目标 seq=2（user/message），前后各 1
    window = ctx.sessionQuery.read_event({"sessionId": session.header.id, "seq": 2, "before": 1, "after": 1})
    assert window["target"].seq == 2
    assert window["startSeq"] == 1 and window["endSeq"] == 3
    assert [e.seq for e in window["events"]] == [1, 2, 3]
    # 超窗口 → 拒绝
    try:
        ctx.sessionQuery.read_event({"sessionId": session.header.id, "seq": 2, "before": 999})
        raise AssertionError("应抛 invalid window")
    except SQ.SessionQueryError as exc:
        assert exc.code == "SESSION_QUERY_INVALID_WINDOW"
    # 目标不存在
    try:
        ctx.sessionQuery.read_event({"sessionId": session.header.id, "seq": 999})
        raise AssertionError("应抛 event not found")
    except SQ.SessionQueryError as exc:
        assert exc.code == "SESSION_QUERY_EVENT_NOT_FOUND"


# --------------------------------------------------------------------------- #
# 5. 全文检索：分页游标 + 跨会话
# --------------------------------------------------------------------------- #
def test_search_pagination_and_sessions() -> None:
    ctx, _ = _ctx(persist=False)
    session = ctx.sessions.create()
    _fill(session, rounds=3)  # 3 条含"郑州"的 user/message
    page1 = ctx.sessionQuery.search_events(session, "郑州", page_size=2)
    assert len(page1["hits"]) == 2
    assert page1["cursor"] is not None
    page2 = ctx.sessionQuery.search_events(session, "郑州", page_size=2, cursor=page1["cursor"])
    assert len(page2["hits"]) == 1
    assert page2["cursor"] is None
    # 不重叠
    seqs1 = {h["seq"] for h in page1["hits"]}
    seqs2 = {h["seq"] for h in page2["hits"]}
    assert not (seqs1 & seqs2)
    # 非法游标
    try:
        ctx.sessionQuery.search_events(session, "郑州", cursor="garbage")
        raise AssertionError("应抛 invalid cursor")
    except SQ.SessionQueryError as exc:
        assert exc.code == "SESSION_QUERY_INVALID_CURSOR"
    # 跨会话
    other = ctx.sessions.create()
    other.append("user/message", create_user_message([TextBlock("北京天气")], MessageSource("user")))
    result = ctx.sessionQuery.search_sessions("郑州")
    assert len(result["hits"]) == 1
    assert result["hits"][0]["session"].id == session.header.id


# --------------------------------------------------------------------------- #
# 6. 追踪：谱系 + 事件来源
# --------------------------------------------------------------------------- #
def test_tracing() -> None:
    ctx, _ = _ctx(persist=False)
    parent = ctx.sessions.create()
    _fill(parent)
    child = ctx.sessions.create()
    _fill(child, parent=parent.header.id)
    grand = ctx.sessions.create()
    _fill(grand, parent=child.header.id)
    trace = ctx.sessionQuery.trace_session(child.header.id)
    assert trace["target"]["header"].id == child.header.id
    assert [a["header"].id for a in trace["ancestors"]] == [parent.header.id]
    assert [d["session"]["header"].id for d in trace["descendants"]] == [grand.header.id]
    # 事件追踪：compaction replace 后被遮蔽 → sources 标注 shadowed
    session = ctx.sessions.create()
    _fill(session, rounds=1)
    old_nodes = list(session.surface["nodes"])
    session.append("user/message", create_user_message([TextBlock("summary")], MessageSource("user")),
                   surface_op={"op": "replace", "start": old_nodes[0], "end": old_nodes[-1]})
    ev = ctx.sessionQuery.trace_event(session.header.id, old_nodes[0])
    assert any(s.get("kind") == "shadowed" for s in ev["sources"])


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def main() -> None:
    tests = [
        test_corpus_and_reads,
        test_surface_classification,
        test_filters,
        test_read_event_window,
        test_search_pagination_and_sessions,
        test_tracing,
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

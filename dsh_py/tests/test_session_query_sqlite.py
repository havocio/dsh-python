"""session-query-sqlite 后端测试：FTS5 持久化索引、增量 reconcile、游标与禁用态。

运行：``python dsh_py/tests/test_session_query_sqlite.py``
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.services import session as S
from dsh_py.services import session_query as SQ
from dsh_py.services import session_query_sqlite as SQSQ
from dsh_py.services.session_persistence import JsonlSessionPersistence
from dsh_py.services.message import MessageSource, TextBlock, create_user_message


def _ctx(persist: bool = True, config: dict | None = None):
    ctx = AppContext()
    S.apply(ctx)
    SQSQ.apply(ctx, config)
    tmp = ""
    if persist:
        tmp = tempfile.mkdtemp()
        back = JsonlSessionPersistence(ctx, os.path.join(tmp, "sessions"))
        ctx.sessions.attach_persistence(back)
    return ctx, tmp


def _fill(session, rounds: int = 2):
    for i in range(rounds):
        session.append("user/message",
                       create_user_message([TextBlock(f"郑州天气第{i + 1}轮")], MessageSource("user")))
        session.append("assistant/message",
                       create_user_message([TextBlock("工作内容摘要" * 5)], MessageSource("assistant")))
    session.append("turn/end", {"turn": rounds})


def test_sqlite_search_basic():
    ctx, _ = _ctx(persist=False)
    session = ctx.sessions.create()
    _fill(session)
    # 会话内检索
    res = ctx.sessionQuery.search_events({"sessionId": session.header.id, "query": "郑州", "limit": 20})
    assert len(res["hits"]) == 2
    assert res["hits"][0]["snippet"]  # 片段非空
    assert res["cursor"] is None
    # 跨会话检索
    other = ctx.sessions.create()
    other.append("user/message", create_user_message([TextBlock("北京天气")], MessageSource("user")))
    res2 = ctx.sessionQuery.search_sessions({"query": "郑州"})
    assert len(res2["hits"]) == 1
    assert res2["hits"][0]["session"].id == session.header.id
    assert "郑州" in res2["hits"][0]["strongest"]["snippet"]


def test_sqlite_cursor_pagination():
    ctx, _ = _ctx(persist=False)
    session = ctx.sessions.create()
    _fill(session, rounds=5)  # 5 条含"郑州"
    page1 = ctx.sessionQuery.search_events({"sessionId": session.header.id, "query": "郑州", "limit": 2})
    assert len(page1["hits"]) == 2
    assert page1["cursor"] is not None
    page2 = ctx.sessionQuery.search_events(
        {"sessionId": session.header.id, "query": "郑州", "limit": 2, "cursor": page1["cursor"]})
    assert len(page2["hits"]) == 2
    assert page2["cursor"] is not None
    page3 = ctx.sessionQuery.search_events(
        {"sessionId": session.header.id, "query": "郑州", "limit": 2, "cursor": page2["cursor"]})
    assert len(page3["hits"]) == 1
    assert page3["cursor"] is None
    # 三页不重叠
    seqs = {h["seq"] for p in (page1, page2, page3) for h in p["hits"]}
    assert len(seqs) == 5
    # 非法游标
    try:
        ctx.sessionQuery.search_events({"sessionId": session.header.id, "query": "郑州", "cursor": "garbage"})
        raise AssertionError("应抛 invalid cursor")
    except SQ.SessionQueryError as exc:
        assert exc.code == "SESSION_QUERY_INVALID_CURSOR"


def test_sqlite_stale_cursor_after_corpus_change():
    ctx, _ = _ctx(persist=False)
    session = ctx.sessions.create()
    _fill(session, rounds=5)  # 5 条含"郑州"的 user 事件
    page = ctx.sessionQuery.search_events(
        {"sessionId": session.header.id, "query": "郑州", "limit": 2})
    cursor = page["cursor"]
    assert cursor is not None  # 多页才有游标
    # 语料变化：追加一条新事件 → 指纹变化 → generation 自增
    session.append("user/message",
                   create_user_message([TextBlock("新增郑州留言")], MessageSource("user")))
    try:
        ctx.sessionQuery.search_events(
            {"sessionId": session.header.id, "query": "郑州", "limit": 2, "cursor": cursor})
        raise AssertionError("应抛 stale cursor")
    except SQ.SessionQueryError as exc:
        assert exc.code == "SESSION_QUERY_STALE_CURSOR"


def test_sqlite_openat_never_disabled():
    ctx, _ = _ctx(persist=False, config={"openAt": "never"})
    session = ctx.sessions.create()
    _fill(session)
    try:
        ctx.sessionQuery.search_events({"sessionId": session.header.id, "query": "郑州"})
        raise AssertionError("openAt=never 应禁用检索")
    except SQ.SessionQueryError as exc:
        assert exc.code == "SESSION_QUERY_SEARCH_DISABLED"
    try:
        ctx.sessionQuery.search_sessions({"query": "郑州"})
        raise AssertionError("openAt=never 应禁用检索")
    except SQ.SessionQueryError as exc:
        assert exc.code == "SESSION_QUERY_SEARCH_DISABLED"


def test_sqlite_event_filter():
    ctx, _ = _ctx(persist=False)
    session = ctx.sessions.create()
    _fill(session)
    # 仅 user/message 类型过滤（assistant 不含"郑州"）
    res = ctx.sessionQuery.search_events({
        "sessionId": session.header.id, "query": "郑州",
        "filters": [{"kind": "type", "values": ["user/message"]}], "limit": 20})
    assert len(res["hits"]) == 2
    res2 = ctx.sessionQuery.search_events({
        "sessionId": session.header.id, "query": "郑州",
        "filters": [{"kind": "type", "values": ["assistant/message"]}], "limit": 20})
    # assistant 消息不含"郑州"，命中应为空
    assert len(res2["hits"]) == 0
    # 会话级 cwd 过滤：空 cwd 会话不被任何 cwd 命中
    res3 = ctx.sessionQuery.search_sessions({
        "query": "郑州", "sessionFilters": [{"kind": "cwd", "values": ["/nonexistent"]}]})
    assert len(res3["hits"]) == 0


def test_sqlite_persisted_indexing():
    """纯持久化（非 live）会话也能被 SQLite 索引检索（跨 ctx 共享 persistence）。"""
    tmp = tempfile.mkdtemp()
    # ctx1：创建并落盘
    ctx1 = AppContext()
    S.apply(ctx1)
    back = JsonlSessionPersistence(ctx1, os.path.join(tmp, "sessions"))
    ctx1.sessions.attach_persistence(back)
    s = ctx1.sessions.create()
    s.append("user/message", create_user_message([TextBlock("广州美食推荐")], MessageSource("user")))
    # ctx2：仅 attach 同一 persistence，无 live 会话
    ctx2 = AppContext()
    S.apply(ctx2)
    back2 = JsonlSessionPersistence(ctx2, os.path.join(tmp, "sessions"))
    ctx2.sessions.attach_persistence(back2)
    SQSQ.apply(ctx2, {"path": ":memory:", "openAt": "startup"})
    res = ctx2.sessionQuery.search_sessions({"query": "广州"})
    assert len(res["hits"]) == 1
    assert res["hits"][0]["session"].id == s.header.id


def test_sqlite_file_persistence_path():
    """文件数据库路径：重启后索引状态可保留（generation 恢复）。

    重启需共享同一持久化目录（会话 JSONL）与同一索引库文件（sqlite）。
    """
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "index.sqlite")
    pdir = os.path.join(tmp, "sessions")
    # ctx1：创建并落盘
    ctx1 = AppContext()
    S.apply(ctx1)
    back1 = JsonlSessionPersistence(ctx1, pdir)
    ctx1.sessions.attach_persistence(back1)
    SQSQ.apply(ctx1, {"path": db_path, "openAt": "startup"})
    session = ctx1.sessions.create()
    _fill(session)
    res1 = ctx1.sessionQuery.search_sessions({"query": "郑州"})
    assert len(res1["hits"]) == 1
    # 重新打开同一数据库与持久化目录（模拟重启）
    ctx2 = AppContext()
    S.apply(ctx2)
    back2 = JsonlSessionPersistence(ctx2, pdir)
    ctx2.sessions.attach_persistence(back2)
    SQSQ.apply(ctx2, {"path": db_path, "openAt": "startup"})
    res2 = ctx2.sessionQuery.search_sessions({"query": "郑州"})
    assert len(res2["hits"]) == 1
    assert res2["hits"][0]["session"].id == session.header.id


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"== {passed}/{len(tests)} passed ==")

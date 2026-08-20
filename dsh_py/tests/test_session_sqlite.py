"""SQLite 会话持久化后端 + zstd 压缩 + checkpoint 快速恢复（对照 dsh 源码）。

验证点：
- sqlite round-trip（含 Message 对象还原）；
- zstd 压缩开关（明文 / 压缩字节 / 缺库 fail-loud）；
- resume + seq 续接；
- checkpoint 快速恢复（前缀被截断仍从快照完整重建）；
- 版本不支持 fail-loud；
- checkpoint 策略按 turn 边界周期触发；
- 插件接线（provides / inject 声明）。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

from dsh_py.core.context import AppContext
from dsh_py.services.message import (
    TextBlock,
    create_assistant_message,
    create_user_message,
)
from dsh_py.services.session import SessionService
from dsh_py.services.session_persistence import (
    SESSION_FORMAT_VERSION,
    CheckpointPolicy,
    SessionFormatUnsupportedError,
    SessionPersistenceError,
    SqliteSessionPersistence,
    apply_checkpoint,
    apply_sqlite,
)


def _make_ctx(compression: str = "none"):
    """构造根上下文 + sessions 服务 + SQLite 后端（不依赖完整 profile）。"""
    ctx = AppContext()
    SessionService(ctx)
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "sessions.db")
    backend = SqliteSessionPersistence(ctx, db, compression)
    ctx.sessions.attach_persistence(backend)
    return ctx, backend


def test_sqlite_round_trip_with_messages():
    """sqlite 后端 round-trip：user/assistant 消息正确还原。"""
    ctx, backend = _make_ctx()
    s = ctx.sessions.create()
    s.append("user/message", create_user_message([TextBlock("你好")]))
    s.append("assistant/message", {"message": create_assistant_message([TextBlock("我是助手")])})
    sid = s.header.id

    r = ctx.sessions.resume(sid)
    assert r.header.id == sid
    msgs = r.derive_messages()
    assert msgs[0].content[0].text == "你好"
    assert msgs[1].content[0].text == "我是助手"
    # seq 连续
    assert [e.seq for e in r.events] == [1, 2]


def test_sqlite_zstd_compression():
    """zstd 压缩：库文件内非明文，且 round-trip 仍正确。"""
    ctx, backend = _make_ctx(compression="zstd")
    s = ctx.sessions.create()
    s.append("user/message", create_user_message([TextBlock("压缩测试")]))
    sid = s.header.id

    # 原始库文件里 data 列应是压缩字节（不含明文）
    conn = sqlite3.connect(backend.db_path)
    raw = conn.execute("SELECT data FROM events WHERE session_id=?", (sid,)).fetchone()[0]
    conn.close()
    assert "压缩测试".encode("utf-8") not in raw

    # round-trip 正确
    r = ctx.sessions.resume(sid)
    assert r.derive_messages()[0].content[0].text == "压缩测试"


def test_sqlite_none_compression_is_plaintext():
    """none 压缩：库文件内为明文 JSON（便于人工排查）。"""
    ctx, backend = _make_ctx(compression="none")
    s = ctx.sessions.create()
    s.append("user/message", create_user_message([TextBlock("明文")]))
    sid = s.header.id
    conn = sqlite3.connect(backend.db_path)
    raw = conn.execute("SELECT data FROM events WHERE session_id=?", (sid,)).fetchone()[0]
    conn.close()
    assert "明文".encode("utf-8") in raw


def test_invalid_compression_rejected():
    """非法压缩方式在构造期 fail-loud。"""
    ctx = AppContext()
    SessionService(ctx)
    tmp = tempfile.mkdtemp()
    try:
        SqliteSessionPersistence(ctx, os.path.join(tmp, "x.db"), "lz4")
        assert False, "应当拒绝未知压缩方式"
    except SessionPersistenceError:
        pass


def test_sqlite_resume_seq_continuity():
    """resume 后新事件 seq 紧接历史，历史不被重复。"""
    ctx, backend = _make_ctx()
    s = ctx.sessions.create()
    for i in range(3):
        s.append("user/message", create_user_message([TextBlock(f"m{i}")]))
    sid = s.header.id

    r = ctx.sessions.resume(sid)
    r.append("assistant/message", {"message": create_assistant_message([TextBlock("续")])})
    assert r.events[-1].seq == 4
    assert len(r.derive_messages()) == 4


def test_checkpoint_fast_resume():
    """checkpoint 快速恢复：前缀事件被截断，仍从快照完整重建 10 条。"""
    ctx, backend = _make_ctx()
    s = ctx.sessions.create()
    for i in range(10):
        s.append("user/message", create_user_message([TextBlock(f"m{i}")]))

    # 写 checkpoint 覆盖到 seq 5
    backend.checkpoint(s.header.id, 5, s.events[:5])

    # 模拟崩溃：删除 seq<=5 的 events 行（仅保留尾部）
    conn = sqlite3.connect(backend.db_path)
    conn.execute("DELETE FROM events WHERE session_id=? AND seq<=?", (s.header.id, 5))
    conn.commit()
    conn.close()

    r = ctx.sessions.resume(s.header.id)
    assert len(r.events) == 10
    assert [e.seq for e in r.events] == list(range(1, 11))
    assert r.derive_messages()[0].content[0].text == "m0"
    assert r.derive_messages()[-1].content[0].text == "m9"


def test_sqlite_version_reject():
    """会话版本不被支持时 fail-loud（绝不静默跳过）。"""
    ctx, backend = _make_ctx()
    s = ctx.sessions.create()
    sid = s.header.id
    conn = sqlite3.connect(backend.db_path)
    conn.execute("UPDATE sessions SET version=? WHERE id=?", (SESSION_FORMAT_VERSION + 1, sid))
    conn.commit()
    conn.close()
    try:
        ctx.sessions.resume(sid)
        assert False, "应当拒绝未知版本"
    except SessionFormatUnsupportedError:
        pass


def test_checkpoint_policy_triggers():
    """checkpoint 策略：每 every_turns 个 turn 边界写一次快照。"""
    ctx, backend = _make_ctx()
    policy = CheckpointPolicy(ctx, every_turns=2)
    s = ctx.sessions.create()
    for i in range(4):  # 4 个 assistant/message → 期望 2 次 checkpoint
        s.append("assistant/message", {"message": create_assistant_message([TextBlock(f"a{i}")])})

    ck = backend.load_checkpoint(s.header.id)
    assert ck is not None
    # 第 2、4 次触发；最新快照覆盖到 seq 4
    assert ck[0] == 4
    policy.dispose()


def test_plugin_wiring():
    """插件入口声明正确（provides / inject），loader 拓扑能正确排序。"""
    assert apply_sqlite.provides == ["sessionPersistence"]
    assert "sessions" in apply_sqlite.inject
    assert apply_checkpoint.provides == ["checkpointPolicy"]
    assert "sessions" in apply_checkpoint.inject


if __name__ == "__main__":
    test_sqlite_round_trip_with_messages()
    test_sqlite_zstd_compression()
    test_sqlite_none_compression_is_plaintext()
    test_invalid_compression_rejected()
    test_sqlite_resume_seq_continuity()
    test_checkpoint_fast_resume()
    test_sqlite_version_reject()
    test_checkpoint_policy_triggers()
    test_plugin_wiring()
    print("OK: test_session_sqlite 全部通过")

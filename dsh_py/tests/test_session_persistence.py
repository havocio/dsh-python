"""会话持久化（SessionPersistence + JSONL 后端 + resume）的验证（第 2 层 Session 完整版）。

运行：python dsh_py/tests/test_session_persistence.py
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import load_profile
from dsh_py.services.message import TextBlock, create_assistant_message, create_user_message
from dsh_py.services.session import SessionEvent, SessionHeader
from dsh_py.services.session_persistence import (
    JsonlSessionPersistence,
    SessionFormatUnsupportedError,
    apply as apply_persistence,
)


def test_jsonl_round_trip_and_resume():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = AppContext()
        load_profile(ctx, ["dsh_py.services.session:apply"])
        apply_persistence(ctx, {"dir": tmp})

        # create 自动落盘（登记 header）
        session = ctx.sessions.create(cwd="/workspace/a")
        session.append("turn/start", {"turn": 1})
        session.append("user/message", create_user_message([TextBlock("你好")]))
        session.append("assistant/message", {
            "message": create_assistant_message([TextBlock("回复")]),
            "usage": {"inputTokens": 3, "outputTokens": 2},
        })
        session.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})

        # 工件存在：header 首行 + 4 条事件行
        artifact = os.path.join(tmp, f"{session.header.id}.jsonl")
        assert os.path.exists(artifact)
        with open(artifact, encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
        assert json.loads(lines[0])["type"] == "session"
        assert len(lines) == 5

        # 后端 list 能看到该会话
        headers = ctx.sessionPersistence.list()
        assert any(h.id == session.header.id for h in headers)

        # resume：从持久化恢复，事件与 seq 完整续接
        ctx2 = AppContext()
        load_profile(ctx2, ["dsh_py.services.session:apply"])
        apply_persistence(ctx2, {"dir": tmp})
        restored = ctx2.sessions.resume(session.header.id)
        assert restored.header.id == session.header.id
        assert restored.header.cwd == "/workspace/a"
        assert [ev.type for ev in restored.events] == [
            "turn/start", "user/message", "assistant/message", "turn/end",
        ]
        # 消息对象被还原为 Message
        user_msg = restored.events[1].data
        assert user_msg.content[0].text == "你好"
        # seq 续接：新追加事件 seq 从已有最大值继续
        restored.append("turn/start", {"turn": 2})
        assert restored.events[-1].seq == 5
        # 恢复出的会话也被登记进 store
        assert ctx2.sessions.get(session.header.id) is restored


def test_torn_tail_line_discarded():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = AppContext()
        load_profile(ctx, ["dsh_py.services.session:apply"])
        apply_persistence(ctx, {"dir": tmp})
        session = ctx.sessions.create()
        session.append("turn/start", {"turn": 1})

        # 模拟 torn 尾：手工追加一条残缺 JSON 行
        with open(ctx.sessionPersistence.locate(session.header), "a", encoding="utf-8") as f:
            f.write('{"type": "turn/end", "seq": 2, "time": 1.0, "data": {"reason": \n')

        restored = ctx.sessions.resume(session.header.id)
        # 完整前缀保留（turn/start），torn 尾被丢弃
        assert [ev.type for ev in restored.events] == ["turn/start"]
        # seq 从保留的最大值续接
        restored.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
        assert restored.events[-1].seq == 2


def test_unsupported_version_fails_loud():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = AppContext()
        load_profile(ctx, ["dsh_py.services.session:apply"])
        apply_persistence(ctx, {"dir": tmp})
        session = ctx.sessions.create()
        # 篡改版本为 99
        path = ctx.sessionPersistence.locate(session.header)
        with open(path, encoding="utf-8") as f:
            first = f.readline()
        first = first.replace('"version": 0', '"version": 99', 1)
        with open(path, "r+", encoding="utf-8") as f:
            content = f.read()
        content = content.replace('"version": 0', '"version": 99', 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        try:
            ctx.sessions.resume(session.header.id)
        except SessionFormatUnsupportedError as e:
            assert "版本" in str(e)
        else:  # pragma: no cover
            raise AssertionError("版本不支持应 fail loud")


def test_resume_without_persistence_raises():
    ctx = AppContext()
    load_profile(ctx, ["dsh_py.services.session:apply"])
    try:
        ctx.sessions.resume("whatever")
    except RuntimeError as e:
        assert "持久化" in str(e)
    else:  # pragma: no cover
        raise AssertionError("未挂持久化后端时 resume 应报错")


def test_prepare_enter_semantics():
    ctx = AppContext()
    load_profile(ctx, ["dsh_py.services.session:apply"])
    prepared = ctx.sessions.prepare(cwd="/tmp")
    # 未 enter：store 中没有
    assert ctx.sessions.get(prepared.header.id) is None
    ctx.sessions.enter(prepared)
    assert ctx.sessions.get(prepared.header.id) is prepared
    assert prepared.header.id in ctx.sessions.list()


async def main():
    test_jsonl_round_trip_and_resume()
    test_torn_tail_line_discarded()
    test_unsupported_version_fails_loud()
    test_resume_without_persistence_raises()
    test_prepare_enter_semantics()
    print("OK: 会话持久化测试通过（JSONL round-trip、resume 恢复、torn 容错、版本拒绝、prepare/enter）")


if __name__ == "__main__":
    asyncio.run(main())

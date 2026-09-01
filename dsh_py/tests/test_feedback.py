"""feedback 族集成验证（command-feedback / message-feedback，第 3 层）。

运行：python dsh_py/tests/test_feedback.py

覆盖：
- command-feedback：/feedback 命令空输入报错、有文本追加 feedback/record 事件
  并应答（会话 id + 匿名用户 id）；record_feedback 空文本抛 TypeError；
- message-feedback：list/put/delete 全业务语义——会话身份栅栏、目标必须为
  已定稿 assistant 消息、CAS 版本匹配、无操作不换版本、note 空白/超限拒绝、
  删除缺席幂等、版本冲突、会话不存在；typert 远程作用域装配后可 invoke。
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.commands import apply as commands_apply
from dsh_py.services.message import TextBlock, create_assistant_message
from dsh_py.services.storage import apply as storage_apply
from dsh_py.services.storage_domain import apply as storage_domain_apply
from dsh_py.services.storage_json import apply as storage_json_apply
from dsh_py.services.typert import apply as typert_apply

import dsh_py.plugins.command_feedback as command_feedback
from dsh_py.services.message_feedback import apply as message_feedback_apply


def _ctx():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    return ctx


async def _invoke(ctx, name, agent, raw_input="", signal=None):
    """按现行命令 API 执行（``execute(agent, line, signal)``），解包 ``.result``。"""
    from dsh_py.core.signal import CancelSignal

    line = f"/{name}" + (f" {raw_input}" if raw_input else "")
    execution = await ctx.commands.execute(agent, line, signal or CancelSignal())
    return execution.result if execution is not None else None


def _mf_ctx(tmp):
    """装配 message-feedback 所需的 storage 家族（json 后端 + domain 路由）。"""
    ctx = _ctx()
    storage_apply(ctx)
    storage_json_apply(ctx, {"root": os.path.join(tmp, "json")})
    storage_domain_apply(ctx, {"backend": "json"})
    return ctx


class _Agent:
    def __init__(self, session):
        self.session = session
        self.id = session.header.id


def _session_with_assistant_message(ctx, text="hi"):
    session = ctx.sessions.create(cwd=None)  # create = prepare + enter（活所有者可查）
    message = create_assistant_message([TextBlock(text)])
    session.append("assistant/message", {"message": message})
    return session, message


# --------------------------------------------------------------------------- #
# command-feedback
# --------------------------------------------------------------------------- #
async def test_feedback_command_records_event():
    ctx = _ctx()
    commands_apply(ctx)
    command_feedback.apply(ctx)
    session = ctx.sessions.prepare(cwd=None)
    agent = _Agent(session)

    # 空输入 → 错误，不落事件
    err = await _invoke(ctx, "feedback", agent, "   ")
    assert err.kind == "error" and "Feedback text is required" in err.text
    assert not any(e.type == "feedback/record" for e in session.events)

    # 有文本 → 成功，追加 log-only 事件 + 应答
    ok = await _invoke(ctx, "feedback", agent, " 很好用，希望支持更多格式  ")
    assert ok.kind == "success"
    assert f"Feedback recorded for session {session.header.id}" in ok.text
    assert "Anonymous user:" in ok.text
    records = [e for e in session.events if e.type == "feedback/record"]
    assert len(records) == 1
    assert records[0].data["text"] == "很好用，希望支持更多格式"  # 已 trim


def test_record_feedback_rejects_empty():
    from dsh_py.plugins.command_feedback import record_feedback
    class S:
        def append(self, *a):
            raise AssertionError("不应追加")
    try:
        record_feedback(S(), "   ")
    except TypeError:
        pass
    else:
        raise AssertionError("空文本应抛 TypeError")


# --------------------------------------------------------------------------- #
# message-feedback
# --------------------------------------------------------------------------- #
async def test_message_feedback_full_lifecycle():
    with tempfile.TemporaryDirectory() as tmp:
        await _full_lifecycle_impl(tmp)


async def _full_lifecycle_impl(tmp):
    ctx = _mf_ctx(tmp)
    message_feedback_apply(ctx, {"maxNoteBytes": 20})
    session, message = _session_with_assistant_message(ctx, "hello")
    mid = message.id

    # 未知会话 → session-not-found
    unknown = await ctx.messageFeedback.list({"sessionId": "nope"})
    assert unknown["ok"] is False and unknown["error"]["code"] == "session-not-found"

    # 初始为空
    empty = await ctx.messageFeedback.list({"sessionId": session.header.id})
    assert empty["ok"] is True and empty["value"]["items"] == []

    # 目标必须是 assistant 消息：伪造 id → target-not-found
    bad = await ctx.messageFeedback.put({
        "sessionId": session.header.id, "messageId": "no-such-id",
        "rating": "positive", "ifVersion": None,
    })
    assert bad["ok"] is False and bad["error"]["code"] == "target-not-found"

    # put 创建（ifVersion=None 要求不存在）
    put1 = await ctx.messageFeedback.put({
        "sessionId": session.header.id, "messageId": mid,
        "rating": "positive", "note": "很准确", "ifVersion": None,
    })
    assert put1["ok"] is True
    item1 = put1["value"]
    assert item1["messageId"] == mid and item1["rating"] == "positive"
    assert item1["note"] == "很准确" and item1["version"]
    assert item1["createdAt"] <= item1["updatedAt"]

    # 版本冲突：旧版本 → version-conflict（携带当前项）
    conflict = await ctx.messageFeedback.put({
        "sessionId": session.header.id, "messageId": mid,
        "rating": "negative", "ifVersion": "stale-version",
    })
    assert conflict["ok"] is False and conflict["error"]["code"] == "version-conflict"
    assert conflict["error"]["current"]["rating"] == "positive"

    # 匹配无操作（同值同版本）→ 不换版本
    noop = await ctx.messageFeedback.put({
        "sessionId": session.header.id, "messageId": mid,
        "rating": "positive", "note": "很准确", "ifVersion": item1["version"],
    })
    assert noop["ok"] is True and noop["value"]["version"] == item1["version"]

    # material 更新（正确版本）→ 新版本，createdAt 保留
    put2 = await ctx.messageFeedback.put({
        "sessionId": session.header.id, "messageId": mid,
        "rating": "negative", "ifVersion": item1["version"],
    })
    assert put2["ok"] is True
    item2 = put2["value"]
    assert item2["version"] != item1["version"]
    assert item2["rating"] == "negative" and "note" not in item2
    assert item2["createdAt"] == item1["createdAt"]
    assert item2["updatedAt"] >= item1["updatedAt"]

    # note 空白 / 超限
    blank = await ctx.messageFeedback.put({
        "sessionId": session.header.id, "messageId": mid,
        "rating": "positive", "note": "   ", "ifVersion": item2["version"],
    })
    assert blank["ok"] is False and blank["error"]["code"] == "note-blank"
    too_big = await ctx.messageFeedback.put({
        "sessionId": session.header.id, "messageId": mid,
        "rating": "positive", "note": "x" * 21, "ifVersion": item2["version"],
    })
    assert too_big["ok"] is False and too_big["error"]["code"] == "note-too-large"
    assert too_big["error"]["maxBytes"] == 20 and too_big["error"]["actualBytes"] == 21

    # list：第一条创建顺序 + 快照
    listed = await ctx.messageFeedback.list({"sessionId": session.header.id})
    assert listed["ok"] is True
    assert [i["rating"] for i in listed["value"]["items"]] == ["negative"]

    # delete：版本不符 → conflict；精确匹配 → absent:true；再删幂等
    del_conflict = await ctx.messageFeedback.delete({
        "sessionId": session.header.id, "messageId": mid, "ifVersion": "wrong",
    })
    assert del_conflict["ok"] is False and del_conflict["error"]["code"] == "version-conflict"
    del_ok = await ctx.messageFeedback.delete({
        "sessionId": session.header.id, "messageId": mid, "ifVersion": item2["version"],
    })
    assert del_ok["ok"] is True and del_ok["value"]["absent"] is True
    del_again = await ctx.messageFeedback.delete({
        "sessionId": session.header.id, "messageId": mid, "ifVersion": item2["version"],
    })
    assert del_again["ok"] is True and del_again["value"]["absent"] is True
    final_list = await ctx.messageFeedback.list({"sessionId": session.header.id})
    assert final_list["ok"] is True and final_list["value"]["items"] == []


async def test_message_feedback_remote_invoke():
    with tempfile.TemporaryDirectory() as tmp:
        await _remote_invoke_impl(tmp)


async def _remote_invoke_impl(tmp):
    ctx = _mf_ctx(tmp)
    typert_apply(ctx)
    message_feedback_apply(ctx, {"maxNoteBytes": 20})
    session, message = _session_with_assistant_message(ctx, "remote")

    from dsh_py.services import typert as T
    # 经 typert 注册表调用（scope=messageFeedback, method=list；wire 契约单 request 对象）
    result = await ctx.typertRegistry.invoke(T.InvocationDescriptor(
        id="r1", service="messageFeedback", method="list",
        args={"request": {"sessionId": session.header.id}},
    ))
    assert result.ok is True and result.value["value"]["items"] == []


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    sync_tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and not asyncio.iscoroutinefunction(v)]
    fails = []
    for t in sync_tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            fails.append((t.__name__, repr(e)))
    for t in tests:
        try:
            asyncio.run(t())
        except Exception as e:  # noqa: BLE001
            fails.append((t.__name__, repr(e)))
    for name, err in fails:
        print(f"FAIL {name}: {err}")
    total = len(tests) + len(sync_tests)
    print(f"{total} 项，{len(fails)} 失败")
    if fails:
        raise SystemExit(f"\n{len(fails)} 项失败")


if __name__ == "__main__":
    _run_all()

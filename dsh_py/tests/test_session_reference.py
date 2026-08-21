"""context/session-reference 验证（第 3 层）。

运行：python dsh_py/tests/test_session_reference.py

覆盖：
- URI：roundtrip 编解码、canonical 校验（非 canonical / 非法 payload 拒绝）；
- mention：格式渲染与文本提取（Markdown + 裸 URI）；
- 投影：user/assistant 会话保留、注入/插件上下文排除、checkpoint 判定；
- 保留：整条删除（先非 checkpoint）→ 最长截断 → stats 精确；
- tag-safe JSON：``<`` 转义；
- resolver：listCandidates 排除 self + cwd 亲和排序 + query 过滤；prepare
  的 self/去重/超限校验 + readSurface 投影 + 预算 + prompt 组装 + recall source。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.message import MessageSource, TextBlock, create_assistant_message, create_user_message
from dsh_py.services.session_query import apply as session_query_apply
from dsh_py.services.session_reference import (
    SessionReferenceError,
    apply as session_reference_apply,
    decode_session_reference_uri,
    encode_session_reference_uri,
    format_session_reference_mention,
    parse_session_reference_text,
    retain_referenced_session,
    stringify_tag_safe_json,
)


def _ctx():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    session_query_apply(ctx)
    session_reference_apply(ctx)
    return ctx


class _Agent:
    def __init__(self, session):
        self.session = session
        self.id = session.header.id


# --------------------------------------------------------------------------- #
# URI / mention
# --------------------------------------------------------------------------- #
def test_uri_roundtrip_and_canonical():
    for sid in ("abc-123", "中文会话 id", "with space / slash\\back", "x" * 200):
        uri = encode_session_reference_uri(sid)
        assert uri.startswith("dsh-session:")
        assert decode_session_reference_uri(uri) == sid
    # canonical：非最小 base64url 拒绝
    sid = "abc"
    uri = encode_session_reference_uri(sid)
    padded = uri + "="  # 带 padding 不是 canonical 形式
    try:
        decode_session_reference_uri(padded)
    except SessionReferenceError:
        pass
    else:
        raise AssertionError("非 canonical URI 应拒绝")
    try:
        decode_session_reference_uri("https://evil")
    except SessionReferenceError:
        pass
    else:
        raise AssertionError("非 scheme URI 应拒绝")


def test_mention_roundtrip_and_parse():
    sid = "sess-1"
    mention = format_session_reference_mention({"sessionId": sid, "label": "旧会话 [1]"})
    assert mention.startswith("@[旧会话 [1\\]](")
    parsed = parse_session_reference_text(
        f"See {mention} and {encode_session_reference_uri('sess-2')} for details.",
    )
    assert parsed["references"][0]["sessionId"] == "sess-1"
    assert parsed["references"][0]["label"] == "旧会话 [1]"  # 转义还原
    assert parsed["references"][1]["sessionId"] == "sess-2"
    assert "dsh-session:" not in parsed["text"]  # 已被替换为可读 @label


# --------------------------------------------------------------------------- #
# 序列化 / 投影 / 保留
# --------------------------------------------------------------------------- #
def test_tag_safe_json():
    raw = stringify_tag_safe_json({"a": "<script>alert(1)</script>"})
    assert "<" not in raw and "\\u003c" in raw
    import json
    assert json.loads(raw)["a"] == "<script>alert(1)</script>"  # 解析不变


def _surface_snapshot(session):
    return {
        "session": session.header,
        "capturedThroughSeq": session.seq,
        "events": list(session.events),
    }


def test_projection_and_retention():
    ctx = _ctx()
    source = ctx.sessions.create(cwd="/proj")
    source.append("user/message", create_user_message([TextBlock("hello")]))
    source.append("assistant/message", {"message": create_assistant_message([TextBlock("world")])})
    source.append("tool/result", {"message": create_user_message([TextBlock("tool-out")])})
    # 插件注入上下文应被排除（非 user 来源）
    source.append("user/message", create_user_message(
        [TextBlock("injected")], MessageSource("plugin", plugin="x", form="recall")))

    snapshot = _surface_snapshot(source)
    retained = retain_referenced_session(snapshot, "src", 65_536)
    assert retained is not None
    data = retained["data"]
    roles = [i["role"] for i in data["conversation"]]
    assert roles == ["user", "assistant"]  # tool/injected 排除
    assert data["cwd"] == "/proj"
    assert data["capturedThroughSeq"] == source.seq
    assert retained["stats"]["originalMessages"] == 2
    assert retained["stats"]["omittedMessages"] == 0

    # 紧预算：低于固定结构（32-hex sessionId 约 159B）→ budget-exceeded
    too_small = retain_referenced_session(snapshot, "src", 100)
    assert too_small is None
    # 删除路径：删一条非 checkpoint 消息即收敛
    tight1 = retain_referenced_session(snapshot, "src", 165)
    assert tight1 is not None and tight1["stats"]["truncated"] is True
    assert tight1["stats"]["omittedMessages"] == 1 and tight1["stats"]["retainedMessages"] == 1
    # 截断路径：删除后仍超 → 截断最长文本
    tight2 = retain_referenced_session(snapshot, "src", 161)
    assert tight2 is not None and tight2["stats"]["truncated"] is True
    assert tight2["stats"]["omittedBytes"] > 0


# --------------------------------------------------------------------------- #
# resolver
# --------------------------------------------------------------------------- #
async def test_list_candidates():
    ctx = _ctx()
    a1 = ctx.sessions.create(cwd="/proj")
    a2 = ctx.sessions.create(cwd="/proj")
    a3 = ctx.sessions.create(cwd="/elsewhere")
    agent = _Agent(a1)

    candidates = await ctx.sessionReferenceResolver.listCandidates(agent)
    ids = [c["sessionId"] for c in candidates]
    assert a1.header.id not in ids  # 排除自身
    assert a2.header.id in ids and a3.header.id in ids
    # cwd 亲和：同 cwd 的排前面
    assert ids.index(a2.header.id) < ids.index(a3.header.id)
    assert candidates[0]["label"] == a2.header.id  # 无标题 → 回退 id

    # query 过滤（按 session id 子串）
    q = await ctx.sessionReferenceResolver.listCandidates(agent, query=a2.header.id[:8])
    assert [c["sessionId"] for c in q] == [a2.header.id]

    # limit
    limited = await ctx.sessionReferenceResolver.listCandidates(agent, limit=1)
    assert len(limited) == 1


async def test_prepare_validation_and_prompt():
    ctx = _ctx()
    source = ctx.sessions.create(cwd="/proj")
    source.append("user/message", create_user_message([TextBlock("secret info")]))
    target = ctx.sessions.create(cwd="/proj")
    agent = _Agent(target)

    # self 引用拒绝
    try:
        await ctx.sessionReferenceResolver.prepare(
            agent, [{"type": "text", "text": "hi"}], [{"sessionId": target.header.id}],
        )
    except SessionReferenceError as e:
        assert e.code == "SESSION_REFERENCE_SELF_REFERENCE"
    else:
        raise AssertionError("self 引用应拒绝")

    # 超限拒绝（maxReferences=1 时 2 个引用）
    ctx2 = _ctx()
    del ctx2
    ctx3 = AppContext()
    load_profile(ctx3, CORE_PROFILE)
    session_query_apply(ctx3)
    session_reference_apply(ctx3, {"maxReferences": 1})
    src2 = ctx3.sessions.prepare(cwd="/proj")
    src3 = ctx3.sessions.prepare(cwd="/proj")
    target2 = ctx3.sessions.create(cwd="/proj")
    agent2 = _Agent(target2)
    try:
        await ctx3.sessionReferenceResolver.prepare(
            agent2, [{"type": "text", "text": "hi"}],
            [{"sessionId": src2.header.id}, {"sessionId": src3.header.id}],
        )
    except SessionReferenceError as e:
        assert e.code == "SESSION_REFERENCE_TOO_MANY"
    else:
        raise AssertionError("超限应拒绝")

    # 正常 prepare：prompt 组装 + recall source
    result = await ctx.sessionReferenceResolver.prepare(
        agent, [{"type": "text", "text": "hi"}], [{"sessionId": source.header.id, "label": "旧会话"}],
    )
    assert result["content"] == [{"type": "text", "text": "hi"}]
    context = result["additionalContext"]
    assert context.source.kind == "plugin"
    assert context.source.plugin == "session-reference"
    assert context.source.form == "recall"
    text = context.content[0].text
    assert "## Referenced sessions" in text
    assert "secret info" in text
    assert "<referenced-sessions>" in text
    assert "<" not in text.split("## Referenced sessions", 1)[1].split("<referenced-sessions>", 1)[0] \
        .replace("\\u003c", "")  # 数据区无字面 <


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

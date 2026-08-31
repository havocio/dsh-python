"""session-query 模型侧工具冒烟（tool-session-query，对标 dsh-tool-session-query）。

纯 assert + __main__ 风格：python dsh_py/tests/test_tool_session_query.py

覆盖 5 个工具（session_search / session_event_search / session_trace /
session_event_trace / session_event_read）+ 工作目录授权 + 跨后端（内存版与
SQLite 版）一致性。会话与事件用 dsh_py 原生构造，cwd 经 header 直接赋值。
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.services import session as S
from dsh_py.services import session_query as SQ
from dsh_py.services.tools import ToolService
from dsh_py.services.system_prompt import SystemPrompt
from dsh_py.services.message import MessageSource, TextBlock, create_user_message
import dsh_py.plugins.tool_session_query as T


class FakeAgent:
    """最小会话拥有者：仅需 session.header.{cwd,id}。"""

    def __init__(self, cwd, sid="sess-1"):
        self.session = type("S", (), {"header": type("H", (), {"cwd": cwd, "id": sid})()})()


def build_context(with_sqlite=False, config=None):
    ctx = AppContext()
    S.apply(ctx)
    if with_sqlite:
        import dsh_py.services.session_query_sqlite as SQSQ
        SQSQ.apply(ctx, config or {"openAt": "startup"})
    else:
        SQ.apply(ctx)
    ToolService(ctx)
    SystemPrompt(ctx)
    T.apply(ctx)
    return ctx


def _make_session(ctx, cwd, texts):
    s = ctx.sessions.create()
    s.header.cwd = cwd
    for t in texts:
        s.append("user/message", create_user_message([TextBlock(t)], MessageSource("user")))
    return s


async def call(ctx, agent, name, args):
    text, is_error, _ctxs = await ctx.tools.execute_with_agent(name, json.dumps(args), agent=agent)
    return text, is_error


# --------------------------------------------------------------------------- #
async def test_session_event_search():
    ctx = build_context()
    s = _make_session(ctx, "/ws/a", ["郑州天气如何", "北京天气如何", "工作是写代码"])
    agent = FakeAgent("/ws/a")
    text, err = await call(ctx, agent, "session_event_search",
                          {"sessionId": s.header.id, "query": "郑州"})
    assert not err, text
    assert "郑州" in text
    assert "命中" in text
    # 无命中
    text2, err2 = await call(ctx, agent, "session_event_search",
                             {"sessionId": s.header.id, "query": "上海"})
    assert not err2 and "未找到" in text2, text2
    print("  ✓ session_event_search 命中/空结果")


async def test_session_search_workspace_scope():
    ctx = build_context()
    s = _make_session(ctx, "/ws/a", ["郑州美食推荐", "北京美食推荐"])
    # 同工作目录：命中
    agent = FakeAgent("/ws/a")
    text, err = await call(ctx, agent, "session_search", {"query": "郑州"})
    assert not err, text
    assert s.header.id in text, text
    # 异工作目录：被 cwd 过滤拦截，0 命中
    agent2 = FakeAgent("/ws/other")
    text2, err2 = await call(ctx, agent2, "session_search", {"query": "郑州"})
    assert not err2 and "未找到匹配会话" in text2, text2
    print("  ✓ session_search 工作目录自动过滤（同目录命中/异目录拦截）")


async def test_session_trace():
    ctx = build_context()
    parent = _make_session(ctx, "/ws/a", ["父会话：项目启动"])
    child = _make_session(ctx, "/ws/a", ["子会话：实现细节"])
    child.header.parent_session = parent.header.id
    agent = FakeAgent("/ws/a")
    text, err = await call(ctx, agent, "session_trace", {"sessionId": child.header.id})
    assert not err, text
    assert parent.header.id in text, text
    assert "祖先链" in text
    print("  ✓ session_trace 祖先链包含父会话")


async def test_session_event_trace():
    ctx = build_context()
    s = _make_session(ctx, "/ws/a", ["第一条", "第二条", "第三条"])
    agent = FakeAgent("/ws/a")
    text, err = await call(ctx, agent, "session_event_trace",
                          {"sessionId": s.header.id, "seq": 2})
    assert not err, text
    assert "seq=2" in text or "2" in text
    print("  ✓ session_event_trace 返回目标事件信息")


async def test_session_event_read():
    ctx = build_context()
    s = _make_session(ctx, "/ws/a", ["事件A", "事件B", "事件C", "事件D"])
    agent = FakeAgent("/ws/a")
    text, err = await call(ctx, agent, "session_event_read",
                          {"sessionId": s.header.id, "seq": 2, "before": 1, "after": 1})
    assert not err, text
    assert "事件B" in text
    # 窗口应覆盖 seq 1..3
    assert "事件A" in text and "事件C" in text, text
    print("  ✓ session_event_read 上下文窗口正确")


async def test_authorization_denied():
    ctx = build_context()
    s = _make_session(ctx, "/ws/a", ["郑州天气如何"])
    # 调用方 cwd 不同 → 越界拒绝
    agent = FakeAgent("/ws/other")
    text, err = await call(ctx, agent, "session_event_search",
                          {"sessionId": s.header.id, "query": "郑州"})
    assert err, "越界访问应判为错误"
    assert "越界" in text, text
    # trace 同样拒绝
    text2, err2 = await call(ctx, agent, "session_trace", {"sessionId": s.header.id})
    assert err2 and "越界" in text2, text2
    print("  ✓ 工作目录越界 → 授权拒绝")


async def test_missing_required_arg():
    ctx = build_context()
    agent = FakeAgent("/ws/a")
    text, err = await call(ctx, agent, "session_event_search", {"query": "郑州"})  # 缺 sessionId
    assert err, "缺 sessionId 应报错"
    print("  ✓ 缺必需参数 → 错误")


async def test_sqlite_backend_smoke():
    ctx = build_context(with_sqlite=True, config={"openAt": "startup"})
    s = _make_session(ctx, "/ws/a", ["广州美食推荐", "深圳美食推荐"])
    agent = FakeAgent("/ws/a")
    text, err = await call(ctx, agent, "session_event_search",
                          {"sessionId": s.header.id, "query": "广州"})
    assert not err, text
    assert "广州" in text
    text2, err2 = await call(ctx, agent, "session_search", {"query": "深圳"})
    assert not err2 and "深圳" in text2, text2
    print("  ✓ SQLite 后端下工具同样可用")


async def main():
    await test_session_event_search()
    await test_session_search_workspace_scope()
    await test_session_trace()
    await test_session_event_trace()
    await test_session_event_read()
    await test_authorization_denied()
    await test_missing_required_arg()
    await test_sqlite_backend_smoke()
    print("test_tool_session_query: 全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())

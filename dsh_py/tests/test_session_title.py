"""会话标题系列（session-title + 两个 LLM provider 插件）冒烟。

纯 assert + __main__ 风格：python dsh_py/tests/test_session_title.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.services.llm import ChunkType, LlmAdapter, LlmService, StreamChunk
from dsh_py.services.message import MessageSource, TextBlock, create_user_message
from dsh_py.services.session import SessionService
from dsh_py.services.projection import SessionProjectionRegistry
from dsh_py.services.session_title import (
    SessionTitleProviderId,
    fallback_session_title,
    fold_session_title,
    normalize_session_title,
    truncate_title_utf8,
)
import dsh_py.services.session_title as st
import dsh_py.plugins.session_title_first_prompt_llm as first_prompt
import dsh_py.plugins.session_title_all_prompts_llm as all_prompts


class TitleMockAdapter(LlmAdapter):
    def __init__(self, text="生成的标题"):
        self.text = text

    async def stream(self, options):
        yield StreamChunk(ChunkType.TEXT_DELTA, text=self.text)
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


def build_context(with_provider="first-prompt"):
    ctx = AppContext()
    llm = LlmService(ctx)
    llm.register_adapter(["mock"], TitleMockAdapter())
    ctx.provide("llm", llm)
    sessions = SessionService(ctx)
    ctx.provide("sessions", sessions)
    proj = SessionProjectionRegistry(ctx)
    ctx.provide("sessionProjections", proj)
    st.apply(ctx, {
        "fallbackMaxWords": 8,
        "fallbackMaxBytes": 64,
        "maxTitleBytes": 200,
    })
    if with_provider == "first-prompt":
        first_prompt.apply(ctx, {
            "targetWords": 6, "targetCjkCharacters": 12, "maxInputBytes": 4096,
            "maxOutputTokens": 32, "timeoutMs": 5000,
            "provider": "mock", "model": "m",
        })
    elif with_provider == "all-prompts":
        all_prompts.apply(ctx, {
            "targetWords": 6, "targetCjkCharacters": 12, "maxInputBytes": 4096,
            "maxOutputTokens": 32, "timeoutMs": 5000,
            "provider": "mock", "model": "m",
        })
    return ctx, sessions


def append_user(session, text):
    session.append("user/message", create_user_message([TextBlock(text)], MessageSource("user")))


async def test_pure_helpers():
    assert truncate_title_utf8("hello世界", 5) == "hello"
    assert normalize_session_title("  \x1b[31mhi  you  \x1b[0m ", 200) == "hi you"
    assert fallback_session_title("alpha beta gamma delta", 2, 200) == "alpha beta"
    snap = fold_session_title([
        type("E", (), {"type": "x", "seq": 1, "time": 0.0, "data": {}})(),
        type("E", (), {"type": "session/title", "seq": 2, "time": 1.0,
                       "data": st.SessionTitleEventData(
                           title="T", message_seqs=(1,),
                           source=st.SessionTitleSource(kind="fallback"))})(),
    ])
    assert snap is not None and snap.title == "T" and snap.event_seq == 2


async def test_fallback_only_no_provider():
    ctx, sessions = build_context(with_provider=None)
    session = sessions.create(cwd=None)
    append_user(session, "Please help me refactor the auth module")
    await asyncio.sleep(0.05)
    svc = ctx.sessionTitle
    snap = svc.get(session)
    assert snap is not None
    assert snap.source.kind == "fallback"
    assert "refactor" in snap.title.lower() or "auth" in snap.title.lower()


async def test_first_prompt_provider_overrides_fallback():
    ctx, sessions = build_context(with_provider="first-prompt")
    session = sessions.create(cwd=None)
    session.request_header = {"config": {"provider": "mock", "model": "m"}}
    append_user(session, "如何写一个 fastapi 服务")
    await asyncio.sleep(0.1)
    svc = ctx.sessionTitle
    snap = svc.get(session)
    assert snap is not None
    # provider 覆盖回退：最新是 provider 来源
    assert snap.source.kind == "provider"
    assert snap.source.provider == SessionTitleProviderId("session-title-first-prompt-llm")
    # 回退标题也应存在于更早的事件中
    fallback_events = [e for e in session.events if e.type == "session/title" and e.data.source.kind == "fallback"]
    assert fallback_events
    # 投影单元应反映最新标题
    proj = ctx.sessionProjections.snapshot(session)
    assert proj["values"].get("title") == snap.title


async def test_rename_pins_and_refresh():
    ctx, sessions = build_context(with_provider="first-prompt")
    session = sessions.create(cwd=None)
    session.request_header = {"config": {"provider": "mock", "model": "m"}}
    append_user(session, "explore the dataset")
    await asyncio.sleep(0.1)
    svc = ctx.sessionTitle
    renamed = svc.rename(session, "我的研究发现")
    assert renamed.source.kind == "user"
    assert svc.get(session).title == "我的研究发现"
    # 重命名钉住后，再追加消息不应被自动覆盖
    append_user(session, "another prompt that should not regenerate title")
    await asyncio.sleep(0.1)
    assert svc.get(session).title == "我的研究发现"


async def test_all_prompts_regenerates():
    ctx, sessions = build_context(with_provider="all-prompts")
    session = sessions.create(cwd=None)
    session.request_header = {"config": {"provider": "mock", "model": "m"}}
    append_user(session, "first question about docker")
    await asyncio.sleep(0.1)
    svc = ctx.sessionTitle
    first = svc.get(session)
    append_user(session, "second follow-up about kubernetes")
    await asyncio.sleep(0.1)
    second = svc.get(session)
    assert first is not None and second is not None
    assert second.event_seq > first.event_seq
    assert second.source.kind == "provider"


async def main():
    await test_pure_helpers()
    await test_fallback_only_no_provider()
    await test_first_prompt_provider_overrides_fallback()
    await test_rename_pins_and_refresh()
    await test_all_prompts_regenerates()
    print("test_session_title: 全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())

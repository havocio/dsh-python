"""示例插件的验证（Step 6）。

运行：python dsh_py/tests/test_plugins.py
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import bootstrap
from dsh_py.services.agent import AgentOptions
from dsh_py.services.llm import ChunkType, LlmAdapter, StreamChunk
from dsh_py.services.message import MessageSource, as_text

import dsh_py.plugins.long_term_memory as ltm
import dsh_py.plugins.system_instructions as si


class EchoAdapter(LlmAdapter):
    """固定回复的 mock，便于观察记忆/指令注入。"""
    async def stream(self, options):
        yield StreamChunk(ChunkType.TEXT_DELTA, text="我已记录。")
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


async def test_long_term_memory_recall():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = AppContext()
        bootstrap(ctx)
        ctx.llm.register_adapter(["mock"], EchoAdapter())
        ltm.apply(ctx, {"storage_dir": tmp, "capture": True})

        session = ctx.sessions.create()
        agent = ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))

        await agent.run("请记住我的名字叫 张三")
        await agent.run("张三 是谁？")

        # 记忆文件应已落盘
        mem_file = os.path.join(tmp, "memories.jsonl")
        assert os.path.exists(mem_file)
        lines = [json.loads(l) for l in open(mem_file, encoding="utf-8") if l.strip()]
        assert any("张三" in e["text"] for e in lines)

        # 第二轮应注入 recall 上下文（step1 的 user/message 含 form='recall'）
        recalled = [
            ev for ev in session.events
            if ev.type == "user/message"
            and getattr(ev.data.source, "form", "") == "recall"
        ]
        assert recalled, "第二轮未注入长记忆召回"
        assert any("张三" in as_text(m.content) for m in [ev.data for ev in recalled])


async def test_system_instructions_inject():
    ctx = AppContext()
    bootstrap(ctx)
    ctx.llm.register_adapter(["mock"], EchoAdapter())
    si.apply(ctx, {"instructions": "你只能用中文回答。"})

    session = ctx.sessions.create()
    agent = ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))
    await agent.run("hello")

    injected = [
        ev for ev in session.events
        if ev.type == "user/message"
        and getattr(ev.data.source, "form", "") == "instructions"
    ]
    assert injected, "未注入系统指令"
    assert "只能用中文" in as_text(injected[0].data.content)


def main():
    asyncio.run(test_long_term_memory_recall())
    asyncio.run(test_system_instructions_inject())
    print("OK: 示例插件测试通过（长记忆捕获+召回、系统指令注入）")


if __name__ == "__main__":
    main()

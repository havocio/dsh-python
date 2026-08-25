"""agent-instructions 验证（第 3 层 context 子包）。

运行：python dsh_py/tests/test_agent_instructions.py

覆盖：
- 纯渲染预算：字节上限下省略（最广文件）与最具体文件截断；
- 基线注入：首个请求前 AGENTS.md 作为 agent-instructions 来源进入耐久上下文；
- 动态协调：成功 write 触碰后，第二轮把文件增量刷新进上下文。
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import bootstrap
from dsh_py.services.agent import AgentOptions
from dsh_py.services.agent_instructions.files import (
    LoadedInstructionFile,
    discover_baseline_instruction_files,
    load_baseline_instruction_set,
)
from dsh_py.services.agent_instructions.render import render_workspace_instruction_set
from dsh_py.services.fs import apply as fs_apply
from dsh_py.services.agent_instructions import apply as ai_apply
from dsh_py.services.llm import ChunkType, LlmAdapter, StreamChunk
from dsh_py.services.message import Message

AGENTS_MD = "# Project Rules\nUse type hints everywhere.\nKeep functions small.\n"


class EchoAdapter(LlmAdapter):
    """简单 mock：一轮直接收尾回复。"""

    async def stream(self, options):
        yield StreamChunk(ChunkType.TEXT_DELTA, text="ok")
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


def _mk_tmp_with_agents(content: str) -> str:
    tmp = tempfile.mkdtemp()
    with open(os.path.join(tmp, "AGENTS.md"), "w", encoding="utf-8") as fh:
        fh.write(content)
    return tmp


def _find_workspace_messages(session):
    out = []
    for ev in session.events:
        if ev.type == "user/message" and isinstance(ev.data, Message):
            src = ev.data.source
            if getattr(src, "plugin", "") == "agent-instructions":
                out.append(ev.data)
    return out


# --------------------------------------------------------------------------- #
# 纯渲染预算
# --------------------------------------------------------------------------- #
def test_render_omits_broadest_when_over_budget():
    broad = LoadedInstructionFile("/a/AGENTS.md", "AGENTS.md", "B" * 5000)
    mid = LoadedInstructionFile("/a/sub/AGENTS.md", "sub/AGENTS.md", "M" * 5000)
    spec = LoadedInstructionFile("/a/sub/deep/AGENTS.md", "sub/deep/AGENTS.md", "S" * 5000)
    rendered = render_workspace_instruction_set([broad, mid, spec], {"maxBytes": 5200})
    # 预算只够最具体的一份 + 提示行 → 其余省略
    assert rendered["rendered"].text
    assert "Workspace instruction budget" in rendered["rendered"].text
    # 存活文件应为最具体的那份
    assert rendered["included"] == [spec]


def test_render_truncates_most_specific():
    big = LoadedInstructionFile("/a/AGENTS.md", "AGENTS.md", "X" * 20000)
    rendered = render_workspace_instruction_set([big], {"maxBytes": 1500})
    text = rendered["rendered"].text
    assert text
    # 截断后仍被 <system-reminder> 包裹，且字节数不超过预算
    assert text.startswith("<system-reminder>")
    assert len(text.encode("utf-8")) <= 1500
    assert rendered["rendered"].truncated


def test_discovery_finds_agents_md():
    tmp = _mk_tmp_with_agents(AGENTS_MD)
    try:
        found = discover_baseline_instruction_files({
            "cwd": tmp, "dshHome": tempfile.mkdtemp(), "maxBytes": 100000})
        paths = {f.absolute_path for f in found}
        assert os.path.join(tmp, "AGENTS.md") in paths
    finally:
        pass


# --------------------------------------------------------------------------- #
# 基线注入（集成）
# --------------------------------------------------------------------------- #
async def test_baseline_instruction_injected():
    tmp = _mk_tmp_with_agents(AGENTS_MD)
    ctx = AppContext()
    bootstrap(ctx)
    ctx.llm.register_adapter(["mock"], EchoAdapter())
    fs_apply(ctx)
    ai_apply(ctx, {"maxBytes": 200_000})

    session = ctx.sessions.create(cwd=tmp)
    agent = ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))
    await agent.run("hello")

    msgs = _find_workspace_messages(session)
    assert msgs, "应注入至少一条 agent-instructions 基线消息"
    combined = "\n".join(str(b.text) for m in msgs for b in m.content if b.__class__.__name__ == "TextBlock")
    assert "Use type hints everywhere." in combined
    # 基线消息带 changes 且 baseline 标记
    assert any(m.source.baseline is True for m in msgs)
    assert any(m.source.changes for m in msgs)


# --------------------------------------------------------------------------- #
# 动态协调：文件编辑后刷新
# --------------------------------------------------------------------------- #
async def test_dynamic_reconcile_after_write():
    tmp = _mk_tmp_with_agents(AGENTS_MD)
    ctx = AppContext()
    bootstrap(ctx)
    ctx.llm.register_adapter(["mock"], EchoAdapter())
    fs_apply(ctx)
    ai_apply(ctx, {"maxBytes": 200_000})

    session = ctx.sessions.create(cwd=tmp)
    agent = ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))
    await agent.run("hello")  # 第一轮：基线 v1 注入

    # 编辑 AGENTS.md（模拟 write 工具触碰）
    new_content = AGENTS_MD + "\nRULE_V2_UNIQUE: prefer composition over inheritance.\n"
    agents_path = os.path.join(tmp, "AGENTS.md")
    with open(agents_path, "w", encoding="utf-8") as fh:
        fh.write(new_content)

    # 广播 tools/result（成功 write，file_path=agents_path）
    ctx.emit("tools/result", {
        "name": "write", "arguments": {"file_path": agents_path},
        "agent": agent, "signal": agent._signal, "token": "t1", "parent": None,
    }, {"isError": False})
    # 让后台投影任务跑完
    await asyncio.sleep(0.05)

    await agent.run("again")  # 第二轮：应包含 v2 增量

    # 第二轮后，会话里应出现提到 RULE_V2_UNIQUE 的 workspace 消息
    matched = [
        m for m in _find_workspace_messages(session)
        if any("RULE_V2_UNIQUE" in b.text for b in m.content if b.__class__.__name__ == "TextBlock")
    ]
    assert matched, "文件编辑后第二轮应把 v2 增量刷进 workspace 上下文"


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_baseline_instruction_injected())
    asyncio.run(test_dynamic_reconcile_after_write())
    test_render_omits_broadest_when_over_budget()
    test_render_truncates_most_specific()
    test_discovery_finds_agents_md()
    print("agent-instructions: 全部通过")

"""context/long-term-memory 验证（第 3 层）。

运行：python dsh_py/tests/test_long_term_memory.py

覆盖：
- tokenize/overlap：拉丁词 + CJK 单字分词与共享词计数；
- MemoryStore：追加/内容去重/关键词检索（>0 命中取前 10）/近期窗口兜底/坏行跳过；
- build_context：超限截断；
- as_text：TextBlock 对象与 dict 块兼容；
- pre-step 召回注入：step==1 注入 recall 形态 plugin 消息、step!=1 跳过、
  reject 决策跳过、重复文本不重复注入；
- capture：turn/end 把 user/assistant 回合配对写入 JSONL 并去重。
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.message import TextBlock, create_assistant_message, create_user_message

import dsh_py.plugins.long_term_memory as ltm
from dsh_py.plugins.long_term_memory import MemoryStore, build_context, overlap, tokenize


def _ctx(tmp, config=None):
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    cfg = {"storageDir": tmp}
    if config:
        cfg.update(config)
    ltm.apply(ctx, cfg)
    return ctx


class _Agent:
    def __init__(self, session):
        self.session = session


async def _pre_step(ctx, agent, messages, step=1, turn=1, inner=None):
    async def default_decision():
        return {"kind": "enter", "messages": []}
    return await ctx.waterfall(
        "agent/pre-step",
        {"agent": agent, "messages": messages, "turn": turn, "step": step, "signal": None},
        inner=inner or default_decision,
    )


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
def test_tokenize_and_overlap():
    # 拉丁词 + CJK 单字
    assert "hello" in tokenize("Hello world")
    assert "世界" not in tokenize("世界")  # CJK 按单字
    assert "世" in tokenize("世界") and "界" in tokenize("世界")
    assert overlap(["a", "b"], ["b", "c"]) == 1
    assert overlap([], ["x"]) == 0


def test_as_text_compatible():
    from dsh_py.plugins.long_term_memory import as_text
    assert as_text([TextBlock("你好"), TextBlock("世界")]) == "你好世界"
    assert as_text([{"type": "text", "text": "dict"}]) == "dict"
    assert as_text([TextBlock("a"), {"type": "image", "text": "no"}]) == "a"
    assert as_text([]) == ""


def test_build_context_truncates():
    entries = [ltm.MemoryEntry("1", 0, "x" * 50, []), ltm.MemoryEntry("2", 0, "y" * 50, [])]
    text = build_context(entries, 4000)
    assert text.startswith("以下是与当前任务相关的跨会话长期记忆")
    assert "x" * 50 in text and "y" * 50 in text
    tight = build_context(entries, 70)
    assert "y" * 50 not in tight  # 超限截断


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #
def test_store_append_retrieve_and_dedup():
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(tmp)
        store.append_entry(ltm.MemoryEntry("1", 1, "讨论过量子计算", []))
        store.append_entry(ltm.MemoryEntry("2", 2, "讨论过菜谱", []))
        assert store.has("讨论过量子计算") is True
        assert store.has("不存在的内容") is False
        # 关键词检索：量子 → 命中第一条
        hits = store.retrieve("量子纠缠", 5)
        assert hits and hits[0].id == "1"
        # 无命中 → 近期窗口兜底
        fallback = store.retrieve("zzzzzz", 5)
        assert [e.id for e in fallback] == ["1", "2"]
        # 文件持久化 + 重载
        store2 = MemoryStore(tmp)
        assert store2.has("讨论过菜谱") is True
        # 坏行跳过
        with open(os.path.join(tmp, "memories.jsonl"), "a", encoding="utf-8") as f:
            f.write("{not-json}\n")
        store3 = MemoryStore(tmp)
        assert len(store3._cache) == 2


def test_store_recent_window_limit():
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(tmp)
        for i in range(8):
            store.append_entry(ltm.MemoryEntry(str(i), i, f"记忆 {i}", []))
        fallback = store.retrieve("no-match-xyz", 3)
        assert len(fallback) == 3 and fallback[-1].id == "7"


# --------------------------------------------------------------------------- #
# 集成：pre-step 召回注入
# --------------------------------------------------------------------------- #
async def test_pre_step_injects_recall_on_first_step():
    with tempfile.TemporaryDirectory() as tmp:
        # 先写记忆文件再 apply（store 在加载时快照）
        with open(os.path.join(tmp, "memories.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "1", "time": 1, "text": "用户偏好：使用 Python", "tags": []}) + "\n")
        ctx = _ctx(tmp)
        session = ctx.sessions.prepare(cwd=None)
        agent = _Agent(session)

        messages = [create_user_message([TextBlock("你会什么语言？")])]
        decision = await _pre_step(ctx, agent, messages, step=1)
        assert decision["kind"] == "enter"
        assert len(decision["messages"]) == 1
        recall = decision["messages"][0]
        assert recall.source.kind == "plugin"
        assert recall.source.plugin == "long-term-memory"
        assert recall.source.form == "recall"
        assert "Python" in ltm.as_text(recall.content)

        # step!=1：跳过
        decision2 = await _pre_step(ctx, agent, messages, step=2)
        assert decision2["messages"] == []

        # reject 决策：跳过
        async def reject_inner():
            return {"kind": "reject"}
        decision3 = await _pre_step(ctx, agent, messages, step=1, inner=reject_inner)
        assert decision3["kind"] == "reject"


async def test_pre_step_dedup_and_no_query():
    with tempfile.TemporaryDirectory() as tmp:
        # 先写记忆文件再 apply
        with open(os.path.join(tmp, "memories.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "1", "time": 1, "text": "喜欢喝咖啡", "tags": []}) + "\n")
        ctx = _ctx(tmp)
        session = ctx.sessions.prepare(cwd=None)
        agent = _Agent(session)

        # 下游已含相同召回文本 → 不重复注入
        async def inner_with_same():
            return {"kind": "enter", "messages": [create_user_message([TextBlock(
                "以下是与当前任务相关的跨会话长期记忆（来自以往对话，仅供参考）：\n- 喜欢喝咖啡",
            )])]}
        messages = [create_user_message([TextBlock("咖啡好喝吗？")])]
        decision = await _pre_step(ctx, agent, messages, step=1, inner=inner_with_same)
        assert len(decision["messages"]) == 1  # 未追加

        # 无用户文本 → 跳过
        empty = await _pre_step(ctx, agent, [], step=1)
        assert empty["messages"] == []


# --------------------------------------------------------------------------- #
# 集成：turn/end 捕获
# --------------------------------------------------------------------------- #
async def test_capture_pairs_turns_and_dedups():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(tmp)
        session = ctx.sessions.prepare(cwd=None)
        session.append("user/message", create_user_message([TextBlock("帮我写个脚本")]))
        session.append("assistant/message", {"message": create_assistant_message([TextBlock("好的，脚本如下")])})
        session.append("user/message", create_user_message([TextBlock("再优化一下")]))
        session.append("assistant/message", {"message": create_assistant_message([TextBlock("已完成优化")])})
        # 触发 turn/end
        session.append("turn/end", {"reason": "done"})

        store = MemoryStore(tmp)
        assert len(store._cache) == 2
        texts = [e.text for e in store._cache]
        assert any("帮我写个脚本" in t and "好的，脚本如下" in t for t in texts)
        assert any("再优化一下" in t and "已完成优化" in t for t in texts)
        # 去重：再次触发相同内容不重复写入
        session2 = ctx.sessions.prepare(cwd=None)
        session2.append("user/message", create_user_message([TextBlock("帮我写个脚本")]))
        session2.append("assistant/message", {"message": create_assistant_message([TextBlock("好的，脚本如下")])})
        session2.append("turn/end", {"reason": "done"})
        store2 = MemoryStore(tmp)
        assert len(store2._cache) == 2

        # 文件真实存在
        assert os.path.exists(os.path.join(tmp, "memories.jsonl"))


async def test_capture_disabled():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(tmp, {"capture": False})
        session = ctx.sessions.prepare(cwd=None)
        session.append("user/message", create_user_message([TextBlock("x")]))
        session.append("assistant/message", {"message": create_assistant_message([TextBlock("y")])})
        session.append("turn/end", {"reason": "done"})
        store = MemoryStore(tmp)
        assert store._cache == []


def _run_all():
    fails = 0
    sync_tests = [
        test_tokenize_and_overlap,
        test_as_text_compatible,
        test_build_context_truncates,
        test_store_append_retrieve_and_dedup,
        test_store_recent_window_limit,
    ]
    async_tests = [
        test_pre_step_injects_recall_on_first_step,
        test_pre_step_dedup_and_no_query,
        test_capture_pairs_turns_and_dedups,
        test_capture_disabled,
    ]
    for fn in sync_tests:
        try:
            fn()
            print(f"OK   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"FAIL {fn.__name__}: {e}")
    for fn in async_tests:
        try:
            asyncio.run(fn())
            print(f"OK   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(sync_tests) + len(async_tests) - fails} 项通过，{fails} 项失败")
    if fails:
        raise SystemExit(f"\n{fails} 项失败")


if __name__ == "__main__":
    _run_all()

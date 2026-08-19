"""跨会话长记忆插件（移植 dsh 的 ``dsh-long-term-memory``）。

- 捕获：每个 ``turn/end`` 把「用户提问 + 助手回复」去重后持久化到 JSONL。
- 召回：每个 turn 的第一步，按关键词重叠检索相关记忆，注入一条
  ``form:'recall'`` 的上下文消息，放在模型可见历史最前面。

插件不发起任何模型调用、也不改核心——只监听 ``agent/pre-step`` 与
``session/event`` 两个 seam，与 dsh 的 ``agent-instructions`` 同构。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from dsh_py.services.message import MessageSource, TextBlock, as_text, create_user_message

name = "long-term-memory"


def _tokenize(text: str) -> list[str]:
    """轻量分词：拉丁词 + 中文单字。"""
    lower = text.lower()
    latin = re.findall(r"[a-z0-9]+", lower)
    cjk = re.findall(r"[一-鿿]", lower)
    return latin + cjk


def _overlap(a: list[str], b: list[str]) -> int:
    """计算两个词表共享词数。"""
    s = set(a)
    return sum(1 for t in b if t in s)


def apply(ctx: Any, config: Optional[dict] = None) -> None:
    """注册长记忆插件。配置项：storage_dir / max_injected_chars / recent_count / capture。"""
    config = config or {}
    storage_dir = config.get("storage_dir") or os.environ.get("DSH_LONG_TERM_MEMORY_DIR") or os.path.join(os.getcwd(), ".dsh", "long-term-memory")
    os.makedirs(storage_dir, exist_ok=True)
    file_path = os.path.join(storage_dir, "memories.jsonl")
    max_chars = config.get("max_injected_chars", 4000)
    recent_count = config.get("recent_count", 5)
    capture = config.get("capture", True)

    def load() -> list[dict]:
        if not os.path.exists(file_path):
            return []
        entries: list[dict] = []
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return entries

    def append(entry: dict) -> None:
        with open(file_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    cache: list[dict] = load()

    def retrieve(query: str) -> list[dict]:
        q = _tokenize(query)
        if not q:
            return cache[-recent_count:]
        scored = [(e, _overlap(_tokenize(e["text"]), q)) for e in cache]
        matched = [e for e, s in scored if s > 0]
        return matched[:10] if matched else cache[-recent_count:]

    def build_context(entries: list[dict]) -> str:
        head = "以下是与当前任务相关的跨会话长期记忆（来自以往对话，仅供参考）："
        body = ""
        for e in entries:
            line = f"- {e['text']}\n"
            if len(head) + len(body) + len(line) > max_chars:
                break
            body += line
        return f"{head}\n{body.strip()}"

    def last_user_text(messages: list[Any]) -> str:
        for m in reversed(messages):
            if getattr(m, "role", None) == "user":
                return as_text(m.content)
        return ""

    # 召回：在每轮第一步注入（先于下游默认决策之后执行）
    @ctx.on("agent/pre-step")
    async def recall(payload: dict, nxt) -> Any:
        decision = await nxt()
        if decision["kind"] == "reject":
            return decision
        if payload.get("step") != 1:
            return decision
        query = last_user_text(payload.get("messages", []))
        if not query:
            return decision
        entries = retrieve(query)
        if not entries:
            return decision
        text = build_context(entries)
        msg = create_user_message(
            [TextBlock(text)],
            MessageSource("plugin", plugin="long-term-memory", form="recall"),
        )
        if any(m.role == "user" and as_text(m.content) == text for m in decision["messages"]):
            return decision
        return {"kind": "enter", "messages": [msg, *decision["messages"]]}

    if capture:
        @ctx.on("session/event")
        def capture_turn(session: Any, event: Any) -> None:
            if event.type != "turn/end":
                return
            turns: list[dict] = []
            pending_user = ""
            for ev in session.events:
                if ev.type == "user/message":
                    src = ev.data.source
                    if getattr(src, "kind", None) == "user":
                        text = as_text(ev.data.content)
                        if text:
                            pending_user = text
                elif ev.type == "assistant/message":
                    content = ev.data["message"].content
                    text = as_text(content)
                    if pending_user and text:
                        turns.append({"user": pending_user[:2000], "assistant": text[:2000]})
                        pending_user = ""
            for t in turns:
                text = f"User: {t['user']}\nAssistant: {t['assistant']}".strip()
                if not text:
                    continue
                if any(e["text"] == text for e in cache):
                    continue
                entry = {"id": os.urandom(8).hex(), "time": event.time, "text": text, "tags": []}
                cache.append(entry)
                append(entry)

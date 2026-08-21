"""
跨会话长期记忆插件（对齐 dsh ``context/long-term-memory``）。

- **捕获**：每次 ``turn/end``，把人类提示与助手回复配对持久化到耐用 JSONL
  存储，按内容键控去重。
- **召回**：每个回合第一步，检索相关过往记忆（关键词重叠 + 近期窗口兜底），
  以 ``recall`` 形态的 plugin 上下文消息注入到模型可见历史之前。

插件不发起任何模型调用、不修改 harness 核心；只监听 ``agent/pre-step`` 与
``session/event`` 两个 seam，与 dsh 的 ``agent-instructions`` 插件同构。

**与 dsh 的差异（已注明）**：
- 存储默认目录 ``./.dsh/long-term-memory``（对齐 dsh）；测试可注入 ``storageDir``。
- dsh 的 ``createUserMessage`` 携带 ``clientTimeZone`` 等字段，dsh_py 的
  ``MessageSource`` 无该字段（同 time-context 差异）。
- dsh 的 pre-step payload 类型为 ``UserMessage[]``；dsh_py 为 ``Message`` 列表，
  判定仅用 ``role == 'user'``。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.services.message import MessageSource, TextBlock, create_user_message

PLUGIN_NAME = "long-term-memory"

DEFAULT_STORAGE_DIR = os.path.join(".dsh", "long-term-memory")
DEFAULT_MAX_INJECTED_CHARS = 4000
DEFAULT_RECENT_COUNT = 5
CAPTURE_TEXT_LIMIT = 2000

Config = z.object({
    "storageDir": z.string().optional(),       # 记忆目录；缺省 $DSH_LONG_TERM_MEMORY_DIR 或 ./.dsh/long-term-memory
    "maxInjectedChars": z.integer().optional(),  # 每回合注入的召回文本上限（默认 4000）
    "capture": z.boolean().optional(),         # 是否把对话回合持久化为记忆（默认 true）
    "recentCount": z.integer().optional(),     # 无关键词命中时的近期记忆兜底条数（默认 5）
})


class MemoryEntry:
    """一条持久化记忆记录。"""

    def __init__(self, id: str, time: int, text: str, tags: Optional[list] = None) -> None:
        self.id = id          # 稳定 id
        self.time = time      # 捕获时刻 Unix epoch 毫秒
        self.text = text      # 紧凑捕获文本（user + assistant 回合）
        self.tags = tags or []  # 可选标签（为未来语义检索预留）

    def to_dict(self) -> dict:
        return {"id": self.id, "time": self.time, "text": self.text, "tags": list(self.tags)}

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryEntry":
        return cls(
            id=str(data.get("id", "")),
            time=int(data.get("time", 0)),
            text=str(data.get("text", "")),
            tags=[str(t) for t in data.get("tags", [])],
        )


def as_text(content: Any) -> str:
    """提取消息内容块中的纯文本（兼容 dsh_py 的 TextBlock 对象与 dict 块）。"""
    parts: list[str] = []
    for block in content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def tokenize(text: str) -> list:
    """轻量分词：拉丁词 + CJK 单字。"""
    lower = text.lower()
    latin = re.findall(r"[a-z0-9]+", lower)
    cjk = re.findall(r"[\u4e00-\u9fff]", lower)
    return latin + cjk


def overlap(a: list, b: list) -> int:
    """两个词表共享词数。"""
    return len(set(a) & set(b))


class MemoryStore:
    """JSONL 记忆存储：加载/追加/检索。"""

    def __init__(self, storage_dir: str) -> None:
        os.makedirs(storage_dir, exist_ok=True)
        self.file = os.path.join(storage_dir, "memories.jsonl")
        self._cache: list = self._load()

    def _load(self) -> list:
        if not os.path.exists(self.file):
            return []
        entries: list = []
        try:
            with open(self.file, "r", encoding="utf-8") as f:
                for line in f:
                    trimmed = line.strip()
                    if not trimmed:
                        continue
                    try:
                        entries.append(MemoryEntry.from_dict(json.loads(trimmed)))
                    except Exception:  # noqa: BLE001 坏行跳过
                        continue
        except OSError:
            return []
        return entries

    def _append(self, entry: MemoryEntry) -> None:
        with open(self.file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")

    def append_entry(self, entry: MemoryEntry) -> None:
        self._cache.append(entry)
        self._append(entry)

    def has(self, text: str) -> bool:
        return any(e.text == text for e in self._cache)

    def retrieve(self, query: str, recent_count: int) -> list:
        """关键词重叠检索（最多 10 条），无命中回退近期窗口。"""
        q = tokenize(query)
        if not q:
            return self._cache[-recent_count:]
        scored = sorted(
            ((e, overlap(tokenize(e.text), q)) for e in self._cache),
            key=lambda p: p[1],
            reverse=True,
        )
        matched = [e for e, score in scored if score > 0]
        return matched[:10] if matched else self._cache[-recent_count:]


def build_context(entries: list, max_chars: int) -> str:
    """渲染召回上下文：头部 + 逐条记忆，超出 max_chars 截断。"""
    head = "以下是与当前任务相关的跨会话长期记忆（来自以往对话，仅供参考）："
    body = ""
    for entry in entries:
        line = f"- {entry.text}\n"
        if len(head) + len(body) + len(line) > max_chars:
            break
        body += line
    return f"{head}\n{body.rstrip()}"


def last_user_text(messages: list) -> str:
    """取消息列表中最后一条 user 消息的纯文本。"""
    for message in reversed(messages):
        if message.role == "user":
            return as_text(message.content)
    return ""


def _user_text_of(data: Any) -> str:
    """turn/end 捕获：user/message 事件数据的纯文本（仅 kind=='user'）。"""
    source = getattr(data, "source", None)
    if source is None or source.kind != "user":
        return ""
    return as_text(getattr(data, "content", []))


def _assistant_text_of(data: Any) -> str:
    """turn/end 捕获：assistant/message 事件数据（dict 包装 message）的纯文本。"""
    message = data.get("message") if isinstance(data, dict) else getattr(data, "message", None)
    if message is None:
        return ""
    return as_text(getattr(message, "content", []))


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：注册 pre-step 召回注入 + turn/end 捕获。

    配置键兼容 dsh 驼峰（``storageDir``/``maxInjectedChars``/``recentCount``）
    与既有 snake_case（``storage_dir``）。注意：loader 的 schema 校验会把未提供
    的可选键填成显式 ``None``，故所有读取必须用 ``or`` 兜底，不可 ``.get(k, d)``。
    """
    cfg = config or {}
    storage_dir = (
        cfg.get("storage_dir") or cfg.get("storageDir")
        or os.environ.get("DSH_LONG_TERM_MEMORY_DIR") or DEFAULT_STORAGE_DIR
    )
    max_chars = int(cfg.get("maxInjectedChars") or DEFAULT_MAX_INJECTED_CHARS)
    recent_count = int(cfg.get("recentCount") or DEFAULT_RECENT_COUNT)
    capture_raw = cfg.get("capture")
    capture = True if capture_raw is None else bool(capture_raw)

    store = MemoryStore(storage_dir)

    @ctx.on("agent/pre-step")
    async def on_pre_step(event: dict, next):
        # 先委托下游：下游贡献者已 produce 决策，我们再注入召回
        decision = await next()
        if decision.get("kind") == "reject":
            return decision
        if event.get("step") != 1:
            return decision
        query = last_user_text(event.get("messages", []))
        if not query:
            return decision
        entries = store.retrieve(query, recent_count)
        if not entries:
            return decision
        text = build_context(entries, max_chars)
        desired = create_user_message(
            [TextBlock(text)],
            source=MessageSource("plugin", plugin=PLUGIN_NAME, form="recall"),
        )
        # 避免重复注入完全相同的召回文本
        if any(
            m.role == "user" and as_text(m.content) == text
            for m in decision.get("messages", [])
        ):
            return decision
        return {"kind": "enter", "messages": [desired, *decision.get("messages", [])]}

    if capture:
        @ctx.on("session/event")
        def on_session_event(session, event) -> None:
            if event.type != "turn/end":
                return
            # 遍历会话日志，把 user/assistant 回合配对为记忆
            turns: list = []
            pending_user = ""
            for ev in session.events:
                if ev.type == "user/message":
                    text = _user_text_of(ev.data)
                    if text:
                        pending_user = text
                elif ev.type == "assistant/message":
                    text = _assistant_text_of(ev.data)
                    if pending_user:
                        turns.append((pending_user, text))
                        pending_user = ""
            for user_text, assistant_text in turns:
                text = (
                    f"User: {user_text[:CAPTURE_TEXT_LIMIT]}\n"
                    f"Assistant: {assistant_text[:CAPTURE_TEXT_LIMIT]}"
                ).strip()
                if not text or store.has(text):
                    continue
                store.append_entry(MemoryEntry(str(uuid.uuid4()), int(time.time() * 1000), text, []))


apply.Config = Config
apply.name = PLUGIN_NAME

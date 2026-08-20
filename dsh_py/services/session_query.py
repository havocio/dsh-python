"""会话查询（session-query，完整版，对标 dsh 的 ``dsh-session-query``）：
组合的会话历史读取、追踪、过滤与全文检索。

统一 live-preferred 会话查询服务：

- **语料库**（:class:`SessionCorpus`）：活会话优先（``ctx.sessions``），持久化兜底，
  列表确定性 newest-first；
- **精确读**：:meth:`SessionQueryEngine.read_session`（完整日志，replay 校验）、
  ``read_surface``（当前模型表面）、``read_event``（目标 + 有界上下文窗口）；
- **过滤**：会话级（id/cwd/created-at/parent/availability）与事件级
  （seq/time/type/surface/text）谓词，AND 组合，纯提供方无关；
- **全文检索**：:meth:`SessionQueryEngine.search_events` / ``search_sessions``
  走倒排索引 + 分页游标（与 dsh 的全文索引后端差异：本实现为会话内存倒排）；
- **追踪**：:meth:`SessionQueryEngine.trace_session`（父链 + 后代树）与
  ``trace_event``（来源事件 + 位置替换链）。

旧核心子集 API（``read`` / ``filter_events`` / ``search`` / ``drop``）保留兼容。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.session import Session, SessionEvent, SessionHeader

# 默认单侧原始事件上下文窗口上限（对齐 dsh 的 SESSION_QUERY_READ_WINDOW_MAX）
READ_WINDOW_MAX = 50
# 批量读的最大并发持久化检查数（对齐 dsh 的 DEFAULT_PERSISTED_INSPECT_CONCURRENCY）
PERSISTED_INSPECT_CONCURRENCY = 4


class SessionQueryError(RuntimeError):
    """会话查询的稳定机器路由失败分类（code 为闭集成员）。"""

    def __init__(self, message: str, code: str, cause: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.cause = cause

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


# --------------------------------------------------------------------------- #
# 文本提取 / 过滤
# --------------------------------------------------------------------------- #
# 单词切分：字母数字序列 + CJK 单字
_WORD = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
_NON_SURFACE_TYPES = {"turn/start", "turn/end", "step/start", "step/end", "tool/call",
                       "compaction/start", "compaction/end", "compaction/summary",
                       "compaction/prune", "session/end-seed", "request/header"}


def tokenize(text: str) -> list[str]:
    """把文本切成检索词：字母数字词小写 + CJK 单字。"""
    return [token.lower() for token in _WORD.findall(text)]


def extract_event_text(event: SessionEvent) -> str:
    """提取一条会话事件的检索文本（对齐 dsh 的 ``extractSessionEventText``）。"""
    t = event.type
    data = event.data
    if t in _NON_SURFACE_TYPES:
        return ""
    if t in ("user/message", "assistant/message", "tool/result"):
        msg = data.get("message") if isinstance(data, dict) else data
        return _message_text(msg)
    if t == "assistant/chunk":
        chunk = data.get("chunk") if isinstance(data, dict) else None
        if chunk is not None:
            return chunk.text or ""
    return ""


def _message_text(message: Any) -> str:
    if message is None:
        return ""
    content = getattr(message, "content", ()) or ()
    parts: list[str] = []
    for block in content:
        if hasattr(block, "text") and block.text:
            parts.append(block.text)
        elif hasattr(block, "arguments") and block.arguments:
            parts.append(block.arguments)
        elif hasattr(block, "content") and hasattr(block, "tool_call_id"):
            parts.append(_message_text(block.content))
    return "".join(parts)


def compile_session_text_filter(text: str) -> re.Pattern:
    """编译字面大小写不敏感、空白灵活的语义文本匹配（正则注入安全）。"""
    trimmed = text.strip()
    if not trimmed:
        raise SessionQueryError("session text filter 必须包含非空白文本", "SESSION_QUERY_INVALID_FILTER")
    pattern = r"\s+".join(re.escape(part) for part in trimmed.split())
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)


def materialize_session_result_filters(filters: list) -> list:
    """拷贝并校验会话级过滤条件（id/cwd/created-at/parent/availability）。"""
    if not isinstance(filters, list):
        raise SessionQueryError("filters 必须是数组", "SESSION_QUERY_INVALID_FILTER")
    out: list[dict] = []
    for f in filters:
        kind = f.get("kind")
        if kind in ("id", "cwd", "parent", "availability"):
            values = list(f.get("values", []))
            if not all(isinstance(v, str) or (kind != "id" and v is None) for v in values):
                raise SessionQueryError(f"{kind} filter values 必须是字符串数组", "SESSION_QUERY_INVALID_FILTER")
            if kind == "availability":
                for v in values:
                    if v not in ("live", "persisted"):
                        raise SessionQueryError(f"availability filter 含未知值 {v!r}", "SESSION_QUERY_INVALID_FILTER")
            out.append({"kind": kind, "values": values})
        elif kind == "created-at":
            out.append({"kind": kind, **copy_range(kind, f)})
        else:
            raise SessionQueryError(f"未知 filter kind {kind!r}", "SESSION_QUERY_INVALID_FILTER")
    return out


def materialize_session_event_result_filters(filters: list) -> list:
    """拷贝并校验事件级过滤条件（seq/time/type/surface/text）。"""
    if not isinstance(filters, list):
        raise SessionQueryError("filters 必须是数组", "SESSION_QUERY_INVALID_FILTER")
    out: list[dict] = []
    for f in filters:
        kind = f.get("kind")
        if kind in ("seq", "time"):
            out.append({"kind": kind, **copy_range(kind, f)})
        elif kind == "type":
            values = list(f.get("values", []))
            if not all(isinstance(v, str) for v in values):
                raise SessionQueryError("type filter values 必须是字符串数组", "SESSION_QUERY_INVALID_FILTER")
            out.append({"kind": kind, "values": values})
        elif kind == "surface":
            values = list(f.get("values", []))
            for v in values:
                if v not in ("current", "shadowed", "log-only"):
                    raise SessionQueryError(f"surface filter 含未知值 {v!r}", "SESSION_QUERY_INVALID_FILTER")
            out.append({"kind": kind, "values": values})
        elif kind == "text":
            if not isinstance(f.get("text"), str):
                raise SessionQueryError("text filter text 必须是字符串", "SESSION_QUERY_INVALID_FILTER")
            out.append({"kind": kind, "text": f["text"]})
        else:
            raise SessionQueryError(f"未知 filter kind {kind!r}", "SESSION_QUERY_INVALID_FILTER")
    return out


def copy_range(kind: str, f: dict) -> dict:
    """拷贝并校验范围（from/to 有限且 from <= to）。"""
    out: dict = {}
    if f.get("from") is not None:
        if not isinstance(f["from"], (int, float)):
            raise SessionQueryError(f"{kind} filter from 必须有限", "SESSION_QUERY_INVALID_FILTER")
        out["from"] = f["from"]
    if f.get("to") is not None:
        if not isinstance(f["to"], (int, float)):
            raise SessionQueryError(f"{kind} filter to 必须有限", "SESSION_QUERY_INVALID_FILTER")
        out["to"] = f["to"]
    if "from" in out and "to" in out and out["from"] > out["to"]:
        raise SessionQueryError(f"{kind} filter from 必须 <= to", "SESSION_QUERY_INVALID_FILTER")
    return out


def _matches_range(value: float, range_: dict) -> bool:
    return (range_.get("from") is None or value >= range_["from"]) \
        and (range_.get("to") is None or value <= range_["to"])


def filter_session_results(records: list, filters: list) -> list:
    """AND 应用会话级过滤（列表值在子句内 OR）。"""
    out = list(records)
    for f in filters:
        kind = f["kind"]
        if kind == "id":
            out = [r for r in out if r["header"].id in f["values"]]
        elif kind == "cwd":
            out = [r for r in out if (r["header"].cwd or None) in f["values"]]
        elif kind == "created-at":
            out = [r for r in out if _matches_range(r["header"].created_at, f)]
        elif kind == "parent":
            out = [r for r in out if (r["header"].parent_session or None) in f["values"]]
        elif kind == "availability":
            out = [r for r in out if any(
                (v == "live" and r["live"]) or (v == "persisted" and r["persisted"]) for v in f["values"]
            )]
    return out


def filter_session_event_documents(documents: list, filters: list) -> list:
    """AND 应用事件级过滤（含字面文本匹配）。"""
    out = list(documents)
    for f in filters:
        kind = f["kind"]
        if kind == "seq":
            out = [d for d in out if _matches_range(d["seq"], f)]
        elif kind == "time":
            out = [d for d in out if _matches_range(d["time"], f)]
        elif kind == "type":
            out = [d for d in out if d["type"] in f["values"]]
        elif kind == "surface":
            out = [d for d in out if d["surface"] in f["values"]]
        elif kind == "text":
            pattern = compile_session_text_filter(f["text"])
            out = [d for d in out if pattern.search(d["text"])]
    return out


# --------------------------------------------------------------------------- #
# 表面分类 / 事件文档
# --------------------------------------------------------------------------- #
def classify_surface(events: list[SessionEvent], session: Optional[Session] = None) -> dict:
    """把每个事件 seq 分类为 current / shadowed / log-only。"""
    result: dict[int, str] = {}
    if session is None:
        # 无会话对象：从日志推导表面（重建 nodes 与 replacements）
        nodes = [e.seq for e in events if e.type in ("user/message", "assistant/message", "tool/result")]
        for seq in nodes:
            result[seq] = "current"
        return result
    for seq in session.surface["nodes"]:
        result[seq] = "current"
    for replacement in session._replacements:
        for seq in replacement["shadowedSeqs"]:
            result[seq] = "shadowed"
    return result


def build_session_event_records(session_id: str, events: list[SessionEvent],
                                session: Optional[Session] = None) -> list[dict]:
    """把完整原始日志投影为表面感知的事件记录（seq 升序）。"""
    surface_by_seq = classify_surface(events, session)
    return [{
        "sessionId": session_id, "seq": e.seq, "type": e.type, "time": e.time,
        "surface": surface_by_seq.get(e.seq, "log-only"),
    } for e in events]


def build_session_event_search_documents(session_id: str, events: list[SessionEvent],
                                         session: Optional[Session] = None) -> list[dict]:
    """构建可检索的语义文档（有文本的事件；结构性事件省略）。"""
    surface_by_seq = classify_surface(events, session)
    documents: list[dict] = []
    for event in events:
        text = extract_event_text(event)
        if not text:
            continue
        documents.append({
            "sessionId": session_id, "seq": event.seq, "type": event.type,
            "time": event.time, "surface": surface_by_seq.get(event.seq, "log-only"),
            "text": text,
        })
    return documents


# --------------------------------------------------------------------------- #
# 语料库（live-preferred）
# --------------------------------------------------------------------------- #
def _compare_sessions(a: dict, b: dict) -> int:
    return -1 if a["header"].created_at > b["header"].created_at \
        else (1 if a["header"].created_at < b["header"].created_at
              else (-1 if a["header"].id < b["header"].id else 1))


class SessionCorpus:
    """live-preferred 逻辑语料库：活会话优先，持久化兜底。"""

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    def _persistence(self) -> Any:
        return getattr(self._ctx.sessions, "_persistence", None)

    def list_sessions(self) -> list[dict]:
        """列出完整逻辑语料库（活会话优先；确定性 newest-first）。"""
        records: dict[str, dict] = {}
        persistence = self._persistence()
        if persistence is not None:
            for header in persistence.list():
                records[header.id] = {"header": header, "live": False, "persisted": True}
        for session_id in self._ctx.sessions.list():
            session = self._ctx.sessions.get(session_id)
            if session is None:
                continue
            sid = session.header.id
            durable = records.get(sid)
            if durable is not None and (
                durable["header"].created_at != session.header.created_at
                or durable["header"].cwd != session.header.cwd
            ):
                raise SessionQueryError(
                    f"会话 {sid} 的活 header 与持久化 header 不兼容",
                    "SESSION_QUERY_SOURCE_CONFLICT",
                )
            records[sid] = {"header": session.header, "live": True,
                            "persisted": durable is not None}
        return sorted(records.values(), key=lambda r: (-r["header"].created_at, r["header"].id))

    def load(self, session_id: str) -> dict:
        """解析一个逻辑来源（活快照优先；持久化兜底 + header 兼容校验）。"""
        live = self._ctx.sessions.get(session_id)
        if live is not None:
            return {"header": live.header, "events": list(live.events), "session": live}
        persistence = self._persistence()
        if persistence is None:
            raise SessionQueryError(f"会话 {session_id!r} 不存在", "SESSION_QUERY_SESSION_NOT_FOUND")
        inspection = persistence.load(session_id)
        if inspection is None:
            raise SessionQueryError(f"会话 {session_id!r} 不存在", "SESSION_QUERY_SESSION_NOT_FOUND")
        return {"header": inspection["meta"], "events": inspection["events"], "session": None}


# --------------------------------------------------------------------------- #
# 查询引擎
# --------------------------------------------------------------------------- #
class SessionQueryEngine(Service):
    """``sessionQuery`` 服务：统一 live-preferred 会话查询（``ctx.sessionQuery``）。"""

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx, "sessionQuery")
        config = config or {}
        self._read_window_max = int(config.get("readWindowMax", READ_WINDOW_MAX))
        self._corpus = SessionCorpus(ctx)
        # 全文检索倒排索引：session_id -> {term: [seq, ...]} + 已索引 seq
        self._index: dict[str, dict[str, list[int]]] = {}
        self._indexed_upto: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # 语料库 / 精确读
    # ------------------------------------------------------------------ #
    def list_sessions(self) -> list[dict]:
        """列出完整逻辑语料库（newest-first 克隆记录）。"""
        return [{"header": r["header"], "live": r["live"], "persisted": r["persisted"]}
                for r in self._corpus.list_sessions()]

    def read_session(self, session_id: str) -> dict:
        """读一个完整逻辑会话日志（replay 校验：表面折叠无效时拒绝）。"""
        loaded = self._corpus.load(session_id)
        classify_surface(loaded["events"], loaded.get("session"))
        return {"session": loaded["header"], "events": list(loaded["events"])}

    def read_surface(self, session_id: str) -> dict:
        """读一个会话的完整当前模型表面（单次语料库观察）。"""
        loaded = self._corpus.load(session_id)
        events = loaded["events"]
        surface_by_seq = classify_surface(events, loaded.get("session"))
        surface_events = [e for e in events if surface_by_seq.get(e.seq) == "current"]
        return {
            "session": loaded["header"],
            "capturedThroughSeq": events[-1].seq if events else None,
            "events": surface_events,
        }

    def list_events(self, session_id: str) -> list[dict]:
        """列出轻量原始日志事件记录（seq 升序）。"""
        loaded = self._corpus.load(session_id)
        return build_session_event_records(session_id, loaded["events"], loaded.get("session"))

    # ------------------------------------------------------------------ #
    # 过滤
    # ------------------------------------------------------------------ #
    def filter_sessions(self, filters: list) -> list[dict]:
        """以提供方无关谓词过滤完整逻辑语料库（AND；newest-first）。"""
        owned = materialize_session_result_filters(filters)
        return [{"header": r["header"], "live": r["live"], "persisted": r["persisted"]}
                for r in filter_session_results(self._corpus.list_sessions(), owned)]

    def filter_session_events(self, session_id: str, filters: list) -> list[dict]:
        """以元数据与字面文本谓词过滤一个会话的语义文档（seq 升序）。"""
        owned = materialize_session_event_result_filters(filters)
        loaded = self._corpus.load(session_id)
        documents = build_session_event_search_documents(session_id, loaded["events"], loaded.get("session"))
        return filter_session_event_documents(documents, owned)

    def read_event(self, request: dict) -> dict:
        """读目标事件加有界原始日志上下文窗口。

        ``request``: ``{"sessionId", "seq", "before"?, "after"?}``（窗口 0..readWindowMax）。
        """
        session_id = request["sessionId"]
        seq = request["seq"]
        before = self._window("before", request.get("before"))
        after = self._window("after", request.get("after"))
        loaded = self._corpus.load(session_id)
        events = loaded["events"]
        target = events[seq - 1] if 1 <= seq <= len(events) else None
        if target is None or target.seq != seq:
            raise SessionQueryError(f"会话 {session_id!r} 在 seq {seq} 无事件", "SESSION_QUERY_EVENT_NOT_FOUND")
        start_seq = max(1, seq - before)
        end_seq = min(len(events), seq + after)
        window = [e for e in events if start_seq <= e.seq <= end_seq]
        return {
            "session": loaded["header"],
            "target": target,
            "events": window,
            "startSeq": start_seq,
            "endSeq": end_seq,
        }

    def _window(self, name: str, value: Any) -> int:
        if value is None:
            return 0
        if not isinstance(value, int) or value < 0 or value > self._read_window_max:
            raise SessionQueryError(
                f"{name} 必须是 0..{self._read_window_max} 的整数",
                "SESSION_QUERY_INVALID_WINDOW",
            )
        return value

    # ------------------------------------------------------------------ #
    # 全文检索（倒排 + 分页游标）
    # ------------------------------------------------------------------ #
    def _ensure_indexed(self, session: Session) -> None:
        """把会话日志中尚未索引的事件增量折进倒排索引。"""
        session_id = session.header.id
        index = self._index.setdefault(session_id, {})
        upto = self._indexed_upto.get(session_id, 0)
        for event in session.events:
            if event.seq <= upto:
                continue
            for term in set(tokenize(extract_event_text(event))):
                index.setdefault(term, []).append(event.seq)
            upto = event.seq
        self._indexed_upto[session_id] = upto

    def search_events(self, session: Session, query: str, page_size: int = 20,
                      cursor: Optional[str] = None) -> dict:
        """在一个会话内全文检索（倒排交集 + seq 升序 + 分页游标）。

        返回 ``{"session": header, "hits": [...], "cursor": str|None}``；
        游标为不透明 continuation token（JSON 编码的 offset）。
        """
        self._ensure_indexed(session)
        terms = [t for t in tokenize(query) if t]
        hit_seqs: Optional[set[int]] = None
        index = self._index.get(session.header.id, {})
        for term in terms:
            seqs = set(index.get(term, []))
            hit_seqs = seqs if hit_seqs is None else hit_seqs & seqs
            if not hit_seqs:
                break
        documents = build_session_event_search_documents(
            session.header.id, session.events, session,
        )
        if hit_seqs is None:
            matched = []
        else:
            by_seq = {d["seq"]: d for d in documents}
            matched = [by_seq[s] for s in sorted(hit_seqs) if s in by_seq]
        offset = self._cursor_offset(cursor)
        page = matched[offset:offset + page_size]
        next_cursor = self._encode_cursor(offset + len(page)) if offset + len(page) < len(matched) else None
        return {"session": session.header, "hits": page, "cursor": next_cursor}

    def search_sessions(self, query: str, page_size: int = 20,
                        cursor: Optional[str] = None) -> dict:
        """跨语料库全文检索并分页（按最强命中事件排名）。"""
        hits: list[dict] = []
        for record in self._corpus.list_sessions():
            session = self.ctx.sessions.get(record["header"].id)
            if session is None:
                continue
            self._ensure_indexed(session)
            terms = [t for t in tokenize(query) if t]
            if not terms:
                continue
            index = self._index.get(session.header.id, {})
            hit_seqs: Optional[set[int]] = None
            for term in terms:
                seqs = set(index.get(term, []))
                hit_seqs = seqs if hit_seqs is None else hit_seqs & seqs
                if not hit_seqs:
                    break
            if not hit_seqs:
                continue
            documents = build_session_event_search_documents(
                session.header.id, session.events, session,
            )
            by_seq = {d["seq"]: d for d in documents}
            matched = [by_seq[s] for s in sorted(hit_seqs) if s in by_seq]
            if matched:
                hits.append({"session": record["header"], "hitCount": len(matched),
                             "strongest": matched[0]})
        hits.sort(key=lambda h: -h["hitCount"])
        offset = self._cursor_offset(cursor)
        page = hits[offset:offset + page_size]
        next_cursor = self._encode_cursor(offset + len(page)) if offset + len(page) < len(hits) else None
        return {"hits": page, "cursor": next_cursor}

    @staticmethod
    def _cursor_offset(cursor: Optional[str]) -> int:
        if cursor is None:
            return 0
        try:
            payload = json.loads(cursor)
            offset = int(payload.get("offset", 0))
            return offset if offset >= 0 else 0
        except (ValueError, TypeError):
            raise SessionQueryError("无效的检索游标", "SESSION_QUERY_INVALID_CURSOR")

    @staticmethod
    def _encode_cursor(offset: int) -> str:
        return json.dumps({"offset": offset})

    # ------------------------------------------------------------------ #
    # 追踪
    # ------------------------------------------------------------------ #
    def trace_session(self, session_id: str) -> dict:
        """追踪已知祖先与后代（父链 + 后代树；祖先环拒绝）。

        返回 ``{"target": record, "ancestors": [record...], "descendants": [node...]}``。
        """
        records = self._corpus.list_sessions()
        by_id = {r["header"].id: r for r in records}
        if session_id not in by_id:
            raise SessionQueryError(f"会话 {session_id!r} 不存在", "SESSION_QUERY_SESSION_NOT_FOUND")
        target = by_id[session_id]
        # 祖先：沿 parent_session 外扩
        ancestors: list[dict] = []
        seen: set[str] = set()
        current = target["header"].parent_session
        while current is not None:
            if current in seen:
                raise SessionQueryError(f"会话谱系存在环（{current}）", "SESSION_QUERY_INVALID_LINEAGE")
            seen.add(current)
            record = by_id.get(current)
            if record is None:
                break
            ancestors.append(record)
            current = record["header"].parent_session
        # 后代：以目标为根建树
        children = [r for r in records if r["header"].parent_session == session_id]

        def build_tree(root_id: str) -> dict:
            node = {"session": by_id[root_id], "descendants": []}
            for child in [r for r in records if r["header"].parent_session == root_id]:
                node["descendants"].append(build_tree(child["header"].id))
            return node

        descendants = [build_tree(c["header"].id) for c in children]
        return {"target": target, "ancestors": ancestors, "descendants": descendants}

    def trace_event(self, session_id: str, seq: int) -> dict:
        """追踪一个事件：直接来源事件（source_event_seqs）与位置替换链。

        返回 ``{"session": header, "target": record, "sources": [record...],
        "replacement": record|None}``。
        """
        loaded = self._corpus.load(session_id)
        events = loaded["events"]
        target = events[seq - 1] if 1 <= seq <= len(events) else None
        if target is None or target.seq != seq:
            raise SessionQueryError(f"会话 {session_id!r} 在 seq {seq} 无事件", "SESSION_QUERY_EVENT_NOT_FOUND")
        record = {"seq": target.seq, "type": target.type, "time": target.time,
                  "surface": classify_surface(events, loaded.get("session")).get(target.seq, "log-only")}
        # 直接来源：surface replace（压缩/修剪）中被遮蔽 → shadowedBy 替换者
        sources = []
        if loaded.get("session") is not None:
            session = loaded["session"]
            for replacement in session._replacements:
                if seq in replacement["shadowedSeqs"]:
                    sources.append({"seq": seq, "shadowedBy": replacement["newSeq"], "kind": "shadowed"})
        return {"session": loaded["header"], "target": record, "sources": sources,
                "replacement": None}

    def drop(self, session_id: str) -> None:
        """丢弃一个会话的检索索引（会话销毁时回收）。"""
        self._index.pop(session_id, None)
        self._indexed_upto.pop(session_id, None)

    # ------------------------------------------------------------------ #
    # 旧核心子集 API（兼容保留）
    # ------------------------------------------------------------------ #
    def read(self, session: Session, start_seq: int = 0, end_seq: Optional[int] = None,
             limit: Optional[int] = None) -> list[dict]:
        """按 seq 窗口读事件记录（exact read）。"""
        records: list[dict] = []
        upper = end_seq if end_seq is not None else session.seq
        for event in session.events:
            if event.seq < start_seq:
                continue
            if event.seq > upper:
                break
            records.append({"seq": event.seq, "type": event.type, "time": event.time, "data": event.data})
        return records[:limit] if limit is not None else records

    def filter_events(self, session: Session, event_type: Optional[str] = None,
                      since: Optional[float] = None, until: Optional[float] = None,
                      limit: Optional[int] = None) -> list[dict]:
        """按事件类型与时间范围过滤（旧 API）。"""
        records: list[dict] = []
        for event in session.events:
            if event_type is not None and event.type != event_type:
                continue
            if since is not None and event.time < since:
                continue
            if until is not None and event.time > until:
                continue
            records.append({"seq": event.seq, "type": event.type, "time": event.time, "data": event.data})
        return records[:limit] if limit is not None else records

    def search(self, session: Session, query: str) -> list[dict]:
        """关键词全文检索（旧 API：无分页，返回带 text 的命中记录）。"""
        page = self.search_events(session, query, page_size=10 ** 9)
        return page["hits"]


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``sessionQuery`` 服务（统一会话查询引擎）。"""
    SessionQueryEngine(ctx, config)


apply.provides = ["sessionQuery"]  # 声明：本插件提供 sessionQuery 服务

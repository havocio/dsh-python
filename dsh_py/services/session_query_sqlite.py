"""基于 SQLite（FTS5）的持久化会话全文检索后端。

对标 dsh 的 ``@deepseek-ai/dsh-session-query-sqlite``：在 ``session_query``
（内存倒排）之上，用标准库 ``sqlite3`` + FTS5 把会话语料索引进可持久化数据库，
避免每次启动重建索引，并在海量会话时提供更高效的全文检索。

本模块只替换 ``SessionQueryEngine`` 的检索方法（``search_sessions`` /
``search_events``），其余 ``read_*`` / ``filter_*`` / ``trace_*`` / ``list_sessions``
完全复用父类实现（它们不依赖内存倒排索引，而是走 ``SessionCorpus``）。

设计要点（与 dsh 对齐的务实简化）：
- **零额外依赖**：仅用 Python 标准库 ``sqlite3``（CPython 3.13 自带 FTS5）。
- **CJK 检索**：``unicode61`` 分词器会把一段连续 CJK 字符当作**单个分词**，
  导致 ``MATCH '"郑州"'`` 永远无法命中子串。为此索引时对相邻 CJK 字符插入空格，
  使每个字成为独立分词；查询端同样把 CJK 词展开为空格分隔的短语并加引号，
  从而 ``"郑 州"`` 精确匹配相邻二字。ASCII 词按原样分词（大小写不敏感）。
- **展示文本**：FTS 只负责召回，命中高亮与片段截取在 Python 端基于**原始文本**
  完成（移植 dsh 的 ``makeSnippet`` 算法），避免 FTS ``highlight`` 把注入空格
  带进展示。
- **增量 reconcile**：live 会话每次检测指纹变化后重建临时索引；persisted 会话按
  ``header.version`` 作 revision 代理增量更新。仅当语料实际变化才自增
  ``global_generation``，使分页游标在语料不变时保持有效、变化时报 ``STALE_CURSOR``。
- **openAt**：``startup``（apply 时同步打开）/ ``first-search``（首次检索惰性打开）/
  ``never``（检索一律报 ``SESSION_QUERY_SEARCH_DISABLED``，且永不导入/打开 SQLite）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import uuid
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.session_query import (
    SessionQueryEngine,
    SessionQueryError,
    build_session_event_search_documents,
    filter_session_event_documents,
    filter_session_results,
    materialize_session_event_result_filters,
    materialize_session_result_filters,
)

# FTS5 highlight 标记（与 dsh 的 FTS_HIGHLIGHT_START/END 一致）
HL_START = ""
HL_END = ""

# 派生索引的 application id（保护无关数据库不被误清空）
APPLICATION_ID = 0x44534851
# 本后端 schema 版本（与 dsh 独立编号）
SCHEMA_VERSION = 1

# 分页与片段默认值
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
SNIPPET_CHARS = 240

# CJK 统一表意文字基本区（覆盖常用汉字；扩展区罕见字不索引但不影响召回）
_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]+|[A-Za-z0-9_]+")


# --------------------------------------------------------------------------- #
# 文本/查询归一化
# --------------------------------------------------------------------------- #
def cjk_split(text: str) -> str:
    """在相邻 CJK 字符之间插入空格，使 ``unicode61`` 把每个字拆成独立分词。

    仅对 CJK 连续段插空；ASCII 词与 digits 保持原样（其本身即被 unicode61 分词）。
    """
    out: list[str] = []
    prev_cjk = False
    for ch in text:
        is_cjk = "一" <= ch <= "鿿" or "豈" <= ch <= "﫿" or "㐀" <= ch <= "䶿"
        if is_cjk and prev_cjk:
            out.append(" ")
        out.append(ch)
        prev_cjk = is_cjk
    return "".join(out)


def extract_query_terms(query: str) -> list[str]:
    """把查询拆成检索词（CJK 连续段 + ASCII 词），用于 FTS 召回与展示高亮。"""
    return _CJK_RE.findall(query)


def fts_query(query: str) -> str:
    """把用户查询编译为 FTS5 MATCH 串：CJK 段展开为空格短语并加引号，ASCII 词原样加引号。"""
    clauses: list[str] = []
    for term in extract_query_terms(query):
        is_cjk = "一" <= term[0] <= "鿿" or "豈" <= term[0] <= "﫿" or "㐀" <= term[0] <= "䶿"
        if is_cjk:
            spaced = " ".join(term)
            clauses.append('"' + spaced.replace('"', '""') + '"')
        else:
            clauses.append('"' + term.replace('"', '""') + '"')
    return " ".join(clauses)


def sanitize_fts_text(text: str) -> str:
    """写入 FTS5 前去除 NUL 与保留标记，避免冲突。"""
    return text.replace("\0", "�").replace(HL_START, "�").replace(HL_END, "�")


def _mark_text(orig: str, terms: list[str], hl_start: str, hl_end: str) -> tuple[str, int]:
    """在原始文本上标注命中区间并返回 ``(带标记文本, 命中次数)``。

    CJK 词按字面子串匹配；ASCII 词大小写不敏感匹配；多词 AND 各计一次命中。
    """
    n = len(orig)
    flags = [False] * n
    count = 0
    low = orig.lower()
    for term in terms:
        if not term:
            continue
        ascii_term = term.isascii()
        needle = term.lower() if ascii_term else term
        start = 0
        while True:
            idx = low.find(needle, start) if ascii_term else orig.find(term, start)
            if idx < 0:
                break
            count += 1
            for i in range(idx, idx + len(term)):
                flags[i] = True
            start = idx + len(term)
    out: list[str] = []
    in_mark = False
    for i, ch in enumerate(orig):
        if flags[i] and not in_mark:
            out.append(hl_start)
            in_mark = True
        elif not flags[i] and in_mark:
            out.append(hl_end)
            in_mark = False
        out.append(ch)
    if in_mark:
        out.append(hl_end)
    return "".join(out), count


def make_snippet(marked_text: str, max_chars: int) -> str:
    """从带 ``highlight`` 标记的文本生成不超过 ``max_chars`` 的片段（移植 dsh）。"""
    chars: list[str] = []
    match_start: Optional[int] = None
    for ch in marked_text:
        if ch == HL_START:
            if match_start is None:
                match_start = len(chars)
            continue
        if ch == HL_END:
            continue
        if ch.isspace():
            if chars and chars[-1] != " ":
                chars.append(" ")
        else:
            chars.append(ch)
    if chars and chars[-1] == " ":
        chars.pop()
    text = "".join(chars)
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    matched = min(match_start or 0, len(text) - 1)
    start = max(0, matched - max_chars // 3)
    prefix = "…" if start > 0 else ""
    suffix = "…"
    content = max_chars - len(prefix) - len(suffix)
    if content < 1:
        start = matched
        suffix = ""
        content = max_chars - len(prefix)
    elif matched >= start + content:
        start = matched - content + 1
    end = min(len(text), start + content)
    if end == len(text):
        suffix = ""
        content = max_chars - len(prefix)
        start = max(0, end - content)
    end = min(len(text), start + content)
    return f"{prefix}{text[start:end]}{suffix}"


def _request_fingerprint(scope: str, query: str, filters: Any, limit: int) -> str:
    """规范化请求指纹，绑定到不透明游标。"""
    return json.dumps(
        {"scope": scope, "query": query, "filters": _canonical(filters), "limit": limit},
        sort_keys=True,
    )


def _canonical(filters: Any) -> Any:
    """对过滤器做确定性规范化（值排序后序列化）。"""
    if not isinstance(filters, list):
        return filters
    out = []
    for f in filters:
        if isinstance(f, dict) and "values" in f:
            out.append({**f, "values": sorted(f["values"], key=lambda v: (v is None, str(v)))})
        else:
            out.append(f)
    return sorted(out, key=lambda x: json.dumps(x, sort_keys=True))


# --------------------------------------------------------------------------- #
# SQLite 后端引擎
# --------------------------------------------------------------------------- #
class SqliteSessionQueryEngine(SessionQueryEngine):
    """``ctx.sessionQuery`` 的 SQLite 全文索引实现。"""

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        config = config or {}
        # 父类构造会注册 sessionQuery 并创建内存倒排索引（本后端不使用，但无害）
        super().__init__(ctx, config)

        path = config.get("path", ":memory:")
        if not isinstance(path, str) or not path.strip():
            raise SessionQueryError("session-search path 不能为空", "SESSION_QUERY_INVALID_CONFIG")
        open_at = config.get("openAt", "startup")
        if open_at not in ("startup", "first-search", "never"):
            raise SessionQueryError("openAt 取值必须是 startup/first-search/never", "SESSION_QUERY_INVALID_CONFIG")
        journal = config.get("journalMode", "wal")
        if journal not in ("wal", "delete", "truncate", "persist"):
            raise SessionQueryError("journalMode 不支持", "SESSION_QUERY_INVALID_CONFIG")
        default_limit = config.get("defaultLimit", DEFAULT_LIMIT)
        max_limit = config.get("maxLimit", MAX_LIMIT)
        if not isinstance(max_limit, int) or isinstance(max_limit, bool) or max_limit < 1:
            raise SessionQueryError("maxLimit 必须是正整数", "SESSION_QUERY_INVALID_CONFIG")
        if (not isinstance(default_limit, int) or isinstance(default_limit, bool)
                or default_limit < 1 or default_limit > max_limit):
            raise SessionQueryError("defaultLimit 必须是 1..maxLimit 的正整数", "SESSION_QUERY_INVALID_CONFIG")
        snippet_chars = config.get("snippetChars", SNIPPET_CHARS)
        if not isinstance(snippet_chars, int) or isinstance(snippet_chars, bool) or snippet_chars < 1:
            raise SessionQueryError("snippetChars 必须是正整数", "SESSION_QUERY_INVALID_CONFIG")

        self.config = {
            "path": path, "openAt": open_at, "journalMode": journal,
            "defaultLimit": default_limit, "maxLimit": max_limit, "snippetChars": snippet_chars,
        }
        self._instance = uuid.uuid4().hex
        self._db: Optional[sqlite3.Connection] = None
        self._global_generation = 0
        self._last_live_fp: dict[str, str] = {}
        self._last_persisted_rev: dict[str, str] = {}
        self._closed = False

    # ------------------------------------------------------------------ #
    # 生命周期 / 打开
    # ------------------------------------------------------------------ #
    def _assert_search_enabled(self) -> None:
        if self.config["openAt"] == "never":
            raise SessionQueryError(
                "此部署禁用了会话全文检索（openAt=never）",
                "SESSION_QUERY_SEARCH_DISABLED",
            )

    def _ensure_ready(self) -> None:
        if self._db is None and not self._closed:
            self._open_sync()

    def _open_sync(self) -> None:
        """同步打开并初始化数据库（SQLite 打开是同步的，无需事件循环）。"""
        if self._db is not None or self._closed:
            return
        path = self.config["path"]
        try:
            if path != ":memory:":
                abs_path = os.path.abspath(path)
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                self._db = sqlite3.connect(abs_path)
            else:
                self._db = sqlite3.connect(":memory:")
            self._db.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            self._ensure_schema()
            row = self._db.execute("SELECT global_generation FROM search_state WHERE singleton=1").fetchone()
            self._global_generation = int(row[0]) if row else 0
            # 恢复上次索引状态，使重启后游标（若语料未变）仍有效
            self._last_live_fp = {
                r[0]: r[1] for r in self._db.execute("SELECT id, fingerprint FROM live_sessions")
            }
            self._last_persisted_rev = {
                r[0]: r[1] for r in self._db.execute("SELECT id, revision FROM persisted_sessions")
            }
        except Exception as exc:  # pragma: no cover - 由调用方转译为索引失败
            if self._db is not None:
                self._db.close()
                self._db = None
            raise SessionQueryError(
                f"session-search SQLite 索引打开失败：{exc}", "SESSION_QUERY_INDEX_FAILED",
            ) from exc

    def _ensure_schema(self) -> None:
        assert self._db is not None
        db = self._db
        db.execute("""
            CREATE TABLE IF NOT EXISTS search_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                global_generation INTEGER NOT NULL
            )
        """)
        db.execute("INSERT OR IGNORE INTO search_state (singleton, global_generation) VALUES (1, 0)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS persisted_sessions (
                id TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                cwd TEXT,
                parent_session TEXT,
                seed_length INTEGER,
                revision TEXT NOT NULL,
                generation INTEGER NOT NULL
            )
        """)
        db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS persisted_docs USING fts5(
                text,
                session_id UNINDEXED,
                seq UNINDEXED,
                type UNINDEXED,
                time UNINDEXED,
                surface UNINDEXED,
                codepoint_length UNINDEXED,
                orig UNINDEXED,
                tokenize='unicode61'
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS live_sessions (
                id TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                cwd TEXT,
                parent_session TEXT,
                seed_length INTEGER,
                fingerprint TEXT NOT NULL,
                persisted INTEGER NOT NULL,
                generation INTEGER NOT NULL
            )
        """)
        db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS live_docs USING fts5(
                text,
                session_id UNINDEXED,
                seq UNINDEXED,
                type UNINDEXED,
                time UNINDEXED,
                surface UNINDEXED,
                codepoint_length UNINDEXED,
                orig UNINDEXED,
                tokenize='unicode61'
            )
        """)
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        """关闭数据库（部署卸载时回收）。"""
        if self._db is not None:
            self._db.close()
            self._db = None
        self._closed = True

    # ------------------------------------------------------------------ #
    # 增量 reconcile
    # ------------------------------------------------------------------ #
    def _persistence(self) -> Any:
        return getattr(self.ctx.sessions, "_persistence", None)

    def _fingerprint(self, header: Any, events: Any) -> str:
        payload = json.dumps(
            {"header": header.__dict__ if hasattr(header, "__dict__") else header,
             "event_count": len(events)},
            sort_keys=True, default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _reconcile(self) -> None:
        if self._db is None:
            return
        persistence = self._persistence()
        persisted_ids = set()
        if persistence is not None:
            for h in persistence.list():
                persisted_ids.add(h.id)

        # 当前 live 指纹
        cur_live: dict[str, str] = {}
        for sid in self.ctx.sessions.list():
            session = self.ctx.sessions.get(sid)
            if session is None:
                continue
            cur_live[sid] = self._fingerprint(session.header, session.events)
        # 当前 persisted 版本
        cur_pers: dict[str, str] = {}
        if persistence is not None:
            for h in persistence.list():
                cur_pers[h.id] = str(h.version)

        if cur_live == self._last_live_fp and cur_pers == self._last_persisted_rev:
            return  # 语料未变，游标保持有效

        # live：整体重建（live 易变）
        self._db.execute("DELETE FROM live_sessions")
        self._db.execute("DELETE FROM live_docs")
        for sid, fp in cur_live.items():
            session = self.ctx.sessions.get(sid)
            if session is None:
                continue
            header = session.header
            docs = build_session_event_search_documents(header.id, session.events, session)
            self._insert_session("live", header, fp, 1 if sid in persisted_ids else 0, docs)

        # persisted：按 version 增量
        if persistence is not None:
            for h in persistence.list():
                if h.id in cur_live:
                    continue  # live 优先，不索引持久化副本
                if cur_pers.get(h.id) == self._last_persisted_rev.get(h.id) and self._persisted_exists(h.id):
                    continue  # 已索引且未变
                inspection = persistence.load(h.id)
                if inspection is None:
                    continue
                events = inspection.get("events") or []
                docs = build_session_event_search_documents(h.id, events, None)
                self._db.execute("DELETE FROM persisted_docs WHERE session_id=?", (h.id,))
                self._db.execute("DELETE FROM persisted_sessions WHERE id=?", (h.id,))
                self._insert_session("persisted", h, str(h.version), 0, docs)

        self._db.commit()
        self._last_live_fp = cur_live
        self._last_persisted_rev = cur_pers
        self._global_generation += 1

    def _persisted_exists(self, session_id: str) -> bool:
        row = self._db.execute("SELECT 1 FROM persisted_sessions WHERE id=?", (session_id,)).fetchone()
        return row is not None

    def _insert_session(self, table: str, header: Any, rev: str, persisted_flag: int, docs: list[dict]) -> None:
        assert self._db is not None
        if table == "live":
            self._db.execute(
                "INSERT INTO live_sessions "
                "(id, version, created_at, cwd, parent_session, seed_length, fingerprint, persisted, generation) "
                "VALUES (?,?,?,?,?,?,?,?,0)",
                (header.id, header.version, int(header.created_at), header.cwd,
                 header.parent_session, getattr(header, "seed_length", 0), rev, persisted_flag),
            )
        else:
            self._db.execute(
                "INSERT INTO persisted_sessions "
                "(id, version, created_at, cwd, parent_session, seed_length, revision, generation) "
                "VALUES (?,?,?,?,?,?,?,0)",
                (header.id, header.version, int(header.created_at), header.cwd,
                 header.parent_session, getattr(header, "seed_length", 0), rev),
            )
        rows = []
        for d in docs:
            clean = sanitize_fts_text(d["text"])
            rows.append((cjk_split(clean), clean, d["sessionId"], d["seq"], d["type"],
                         d["time"], d["surface"], len(clean)))
        self._db.executemany(
            f"INSERT INTO {table}_docs "
            "(text, orig, session_id, seq, type, time, surface, codepoint_length) VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )

    # ------------------------------------------------------------------ #
    # 检索
    # ------------------------------------------------------------------ #
    def _header_for(self, session_id: str) -> Any:
        """从索引表重建会话头（仅索引所需字段，足够 presentation/filter 使用）。"""
        row = self._db.execute(
            "SELECT version, created_at, cwd, parent_session, seed_length FROM live_sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            row = self._db.execute(
                "SELECT version, created_at, cwd, parent_session, seed_length FROM persisted_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise SessionQueryError(f"会话 {session_id!r} 不存在", "SESSION_QUERY_SESSION_NOT_FOUND")
        return _Header(
            id=session_id, version=row[0], created_at=row[1], cwd=row[2],
            parent_session=row[3], seed_length=row[4] or 0,
        )

    def _query_event_docs(self, session_id: str, expr: str) -> list[dict]:
        """取单会话的 FTS 命中（live 优先，否则 persisted）。"""
        live = self._db.execute(
            "SELECT session_id, seq, type, time, surface, orig "
            "FROM live_docs WHERE session_id=? AND live_docs MATCH ?",
            (session_id, expr),
        ).fetchall()
        rows = live if live else self._db.execute(
            "SELECT session_id, seq, type, time, surface, orig "
            "FROM persisted_docs WHERE session_id=? AND persisted_docs MATCH ?",
            (session_id, expr),
        ).fetchall()
        return [_row_to_doc(r) for r in rows]

    def _query_session_docs(self, expr: str) -> list[dict]:
        """跨语料库取所有 FTS 命中（live 优先覆盖 persisted）。"""
        live = self._db.execute(
            "SELECT d.session_id, d.seq, d.type, d.time, d.surface, d.orig "
            "FROM live_docs d JOIN live_sessions s ON s.id=d.session_id WHERE live_docs MATCH ?",
            (expr,),
        ).fetchall()
        persisted = self._db.execute(
            "SELECT d.session_id, d.seq, d.type, d.time, d.surface, d.orig "
            "FROM persisted_docs d JOIN persisted_sessions s ON s.id=d.session_id "
            "WHERE persisted_docs MATCH ? "
            "AND NOT EXISTS (SELECT 1 FROM live_sessions ls WHERE ls.id=d.session_id)",
            (expr,),
        ).fetchall()
        return [_row_to_doc(r) for r in live + persisted]

    def _availability(self, session_id: str) -> tuple[bool, bool]:
        live = self._db.execute("SELECT 1 FROM live_sessions WHERE id=?", (session_id,)).fetchone() is not None
        persisted = self._db.execute("SELECT 1 FROM persisted_sessions WHERE id=?", (session_id,)).fetchone() is not None
        return live, persisted

    def _event_hit(self, row: dict, terms: list[str]) -> dict:
        orig = row["orig"]
        marked, count = _mark_text(orig, terms, HL_START, HL_END)
        return {
            "sessionId": row["sessionId"], "seq": row["seq"], "type": row["type"],
            "time": row["time"], "surface": row["surface"],
            "snippet": make_snippet(marked, self.config["snippetChars"]),
            "matchCount": count,
        }

    def _paginate(self, hits: list, scope: str, fingerprint: str, page_size: int,
                  cursor: Optional[str]) -> dict:
        offset = self._decode_cursor(cursor, scope, fingerprint) if cursor is not None else 0
        page = hits[offset:offset + page_size]
        next_cursor = self._encode_cursor(scope, fingerprint, offset + len(page)) \
            if offset + len(page) < len(hits) else None
        return {"hits": page, "cursor": next_cursor}

    def _encode_cursor(self, scope: str, fingerprint: str, offset: int) -> str:
        payload = {
            "version": 1, "instance": self._instance, "scope": scope,
            "fingerprint": fingerprint, "generation": str(self._global_generation), "offset": offset,
        }
        return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")

    def _decode_cursor(self, cursor: str, scope: str, fingerprint: str) -> int:
        try:
            payload = json.loads(base64.b64decode(cursor).decode("utf-8"))
        except Exception as exc:
            raise SessionQueryError("session-search 游标无效", "SESSION_QUERY_INVALID_CURSOR") from exc
        if (payload.get("version") != 1 or payload.get("instance") != self._instance
                or payload.get("scope") != scope or payload.get("fingerprint") != fingerprint):
            raise SessionQueryError("session-search 游标不属于本次规范化请求", "SESSION_QUERY_INVALID_CURSOR")
        if str(payload.get("generation")) != str(self._global_generation):
            raise SessionQueryError(
                "会话语料在翻页期间发生变化，请重试完整搜索", "SESSION_QUERY_STALE_CURSOR")
        offset = payload.get("offset")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise SessionQueryError("session-search 游标无效", "SESSION_QUERY_INVALID_CURSOR")
        return offset

    # ------------------------------------------------------------------ #
    # 公开检索 API（override 父类内存实现）
    # ------------------------------------------------------------------ #
    def search_events(self, request: dict, exec: Any = None) -> dict:
        self._assert_search_enabled()
        self._ensure_ready()
        assert self._db is not None
        self._reconcile()

        session_id = request["sessionId"]
        query = _normalize_query(request.get("query", ""))
        filters = materialize_session_event_result_filters(request.get("filters", []))
        page_size = self._limit(request.get("limit"))
        cursor = request.get("cursor")
        fingerprint = _request_fingerprint("events", query, filters, page_size)

        terms = extract_query_terms(query)
        expr = fts_query(query)
        if not expr.strip():
            # 查询无可索引词（如纯标点）：直接返回空命中
            try:
                header = self._header_for(session_id)
            except SessionQueryError:
                header = None
            return {"session": header, "hits": [], "cursor": None}

        rows = self._query_event_docs(session_id, expr)
        hits = []
        for r in rows:
            doc = {"sessionId": r["sessionId"], "seq": r["seq"], "type": r["type"],
                   "time": r["time"], "surface": r["surface"], "text": r["orig"]}
            if filters and not filter_session_event_documents([doc], filters):
                continue
            hits.append(self._event_hit(r, terms))
        # 排名：匹配次数降序、时间降序、seq 降序
        hits.sort(key=lambda h: (-h["matchCount"], -h["time"], -h["seq"]))
        page = self._paginate(hits, "events", fingerprint, page_size, cursor)
        try:
            header = self._header_for(session_id)
        except SessionQueryError:
            header = None
        return {"session": header, **page}

    def search_sessions(self, request: dict, exec: Any = None) -> dict:
        self._assert_search_enabled()
        self._ensure_ready()
        assert self._db is not None
        self._reconcile()

        query = _normalize_query(request.get("query", ""))
        session_filters = materialize_session_result_filters(request.get("sessionFilters", []))
        event_filters = materialize_session_event_result_filters(request.get("eventFilters", []))
        page_size = self._limit(request.get("limit"))
        cursor = request.get("cursor")
        fingerprint = _request_fingerprint("sessions", query, [session_filters, event_filters], page_size)

        terms = extract_query_terms(query)
        expr = fts_query(query)
        if not expr.strip():
            return {"hits": [], "cursor": None}

        rows = self._query_session_docs(expr)
        by_session: dict[str, list[dict]] = {}
        for r in rows:
            by_session.setdefault(r["sessionId"], []).append(r)

        hits = []
        for sid, docs in by_session.items():
            header = self._header_for(sid)
            matched = []
            for r in docs:
                doc = {"sessionId": r["sessionId"], "seq": r["seq"], "type": r["type"],
                       "time": r["time"], "surface": r["surface"], "text": r["orig"]}
                if event_filters and not filter_session_event_documents([doc], event_filters):
                    continue
                matched.append(self._event_hit(r, terms))
            if not matched:
                continue
            matched.sort(key=lambda h: (-h["matchCount"], -h["time"], -h["seq"]))
            live, persisted = self._availability(sid)
            record = {"header": header, "live": live, "persisted": persisted}
            if session_filters and not filter_session_results([record], session_filters):
                continue
            best = matched[0]
            hits.append({
                "session": header, "hitCount": len(matched),
                "strongest": {
                    "sessionId": sid, "seq": best["seq"], "type": best["type"],
                    "time": best["time"], "surface": best["surface"], "snippet": best["snippet"],
                },
            })
        hits.sort(key=lambda h: -h["hitCount"])
        page = self._paginate(hits, "sessions", fingerprint, page_size, cursor)
        return page


def _row_to_doc(row: tuple) -> dict:
    """把 SQL 行（session_id, seq, type, time, surface, orig）转为字典。"""
    return {
        "sessionId": row[0], "seq": row[1], "type": row[2], "time": row[3],
        "surface": row[4], "orig": row[5],
    }


class _Header:
    """轻量会话头视图（仅索引/展示所需字段）。"""

    __slots__ = ("id", "version", "created_at", "cwd", "parent_session", "seed_length")

    def __init__(self, id, version, created_at, cwd, parent_session, seed_length):
        self.id = id
        self.version = version
        self.created_at = created_at
        self.cwd = cwd
        self.parent_session = parent_session
        self.seed_length = seed_length


def _normalize_query(value: Any) -> str:
    if not isinstance(value, str):
        raise SessionQueryError("session-search query 必须为字符串", "SESSION_QUERY_INVALID_QUERY")
    q = re.sub(r"\s+", " ", value.strip())
    if not q:
        raise SessionQueryError("session-search query 不能为空", "SESSION_QUERY_INVALID_QUERY")
    if "\0" in q:
        raise SessionQueryError("session-search query 不能包含 NUL", "SESSION_QUERY_INVALID_QUERY")
    return q


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``sessionQuery`` 服务（SQLite 持久化全文索引后端）。

    与内存版 ``session_query`` 互斥装配——二者都 ``provides=["sessionQuery"]``，
    部署按需在 profile 中二选一加载（本后端提供持久化索引）。
    """
    engine = SqliteSessionQueryEngine(ctx, config)
    if engine.config["openAt"] == "startup":
        engine._open_sync()


apply.provides = ["sessionQuery"]  # 声明：本插件提供 sessionQuery 服务

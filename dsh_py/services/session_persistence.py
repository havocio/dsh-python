"""会话持久化后端（对标 dsh 的 ``session-persistence`` + ``session-persistence-jsonl``）。

:class:`SessionPersistence` 是持久化 seam 的抽象（sessions 服务经
``attach_persistence`` 挂载）；:class:`JsonlSessionPersistence` 是默认后端，
每个会话一个 ``{session_id}.jsonl`` 文件：

- 首行是 header 记录（``{"type":"session", ...}``，对标 jsonl 的 HeaderLine）；
- 后续每行是一条事件（``{"type", "seq", "time", "data"}``，data 经
  :func:`dsh_py.services.message.encode_payload` 编码为 JSON 安全结构）；
- ``load`` 容错：只有最后一行残缺（torn 尾）才丢弃，已提交前缀原样保留
  （对标 dsh 的「torn final record is discarded」）；未知版本拒绝。

本实现为同步文件 IO（dsh 的 write-behind 异步批量留待后续），append 即耐久。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.message import decode_payload, encode_payload
from dsh_py.services.session import SessionEvent, SessionHeader

# 会话持久化格式版本（对齐 dsh 的 SESSION_FORMAT_VERSION）
SESSION_FORMAT_VERSION = 0


def _load_zstd():
    """懒解析 zstd 实现（可选依赖：框架本体零依赖，仅持久化后端按需启用）。"""
    try:
        import zstandard as _zstd
        return _zstd
    except Exception:
        return None


_ZSTD = _load_zstd()


def _compress(compression: str, raw: bytes) -> bytes:
    """按压缩方式压缩字节；``none`` 原样返回。"""
    if compression == "zstd":
        if _ZSTD is None:
            raise SessionPersistenceError("启用 zstd 压缩需先安装 zstandard：pip install zstandard")
        return _ZSTD.ZstdCompressor().compress(raw)
    return raw


def _decompress(compression: str, raw: bytes) -> bytes:
    """按压缩方式解压字节；``none`` 原样返回。"""
    if compression == "zstd":
        if _ZSTD is None:
            raise SessionPersistenceError("读取 zstd 压缩数据需 zstandard：pip install zstandard")
        return _ZSTD.ZstdDecompressor().decompress(raw)
    return raw


class SessionPersistenceError(RuntimeError):
    """会话持久化后端异常。"""


class SessionFormatUnsupportedError(SessionPersistenceError):
    """会话文件版本不被支持（fail loud，绝不静默跳过）。"""


class SessionPersistence(Service, ABC):
    """持久化 seam 抽象（对标 dsh 的 ``abstract class SessionPersistence``）。

    实现需提供：``locate / create / append / load / list``。sessions 服务在
    ``attach_persistence`` 后自动把 create/append 落到后端。
    """

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "sessionPersistence")

    @abstractmethod
    def locate(self, meta: SessionHeader) -> Optional[str]:
        """返回该会话的后端工件路径（可能尚未物化），无则 None。"""

    @abstractmethod
    def create(self, meta: SessionHeader) -> None:
        """登记新会话的元数据（可延迟到首次 append 物化）。"""

    @abstractmethod
    def append(self, session_id: str, events: list[SessionEvent]) -> None:
        """耐久性追加一批事件（须满足 seq 连续契约）。"""

    @abstractmethod
    def load(self, session_id: str) -> Optional[dict]:
        """装载会话：返回 ``{"meta": SessionHeader, "events": [SessionEvent]}``，
        会话不存在返回 None。残缺尾部仅丢弃最后一条 torn 记录。"""

    @abstractmethod
    def list(self) -> list[SessionHeader]:
        """列出所有已物化会话的 header（仅读首行，不解析全日志）。"""

    def read_from(self, session_id: str, from_seq: int) -> Optional[dict]:
        """读取 ``seq >= from_seq`` 的事件（冷读阶梯的尾部重放用）。

        返回 ``{"meta": SessionHeader, "events": [SessionEvent]}``（仅尾段）；
        会话不存在返回 None。默认实现基于 :meth:`load` 过滤；后端可在重载中
        优化（如 sqlite 的游标读）。
        """
        inspection = self.load(session_id)
        if inspection is None:
            return None
        return {
            "meta": inspection["meta"],
            "events": [e for e in inspection["events"] if e.seq >= from_seq],
        }

    def checkpoint(self, session_id: str, seq: int, events: list[SessionEvent]) -> None:
        """可选：写一份前缀快照（默认无操作；sqlite 后端实现快速恢复）。

        :param seq: 快照覆盖到的事件序号（闭区间）。
        :param events: 序号 <= seq 的全部事件（前缀）。
        """


def _header_to_line(meta: SessionHeader) -> dict:
    """SessionHeader → header 行对象（对齐 jsonl 的 HeaderLine）。"""
    line = {"type": "session", "version": meta.version,
            "id": meta.id, "createdAt": meta.created_at}
    if meta.cwd is not None:
        line["cwd"] = meta.cwd
    if meta.request is not None:
        line["request"] = meta.request
    return line


def _line_to_header(line: dict) -> SessionHeader:
    """header 行对象 → SessionHeader（版本不支持时 fail loud）。"""
    if line.get("type") != "session":
        raise SessionPersistenceError(f"首行不是 session header：{line}")
    version = line.get("version")
    if version != SESSION_FORMAT_VERSION:
        raise SessionFormatUnsupportedError(
            f"会话 {line.get('id')!r} 版本 {version} 不受支持（当前 {SESSION_FORMAT_VERSION}）")
    return SessionHeader(
        version=version,
        id=line["id"],
        created_at=line.get("createdAt", 0.0),
        cwd=line.get("cwd"),
        request=line.get("request"),
    )


def _event_to_line(event: SessionEvent) -> dict:
    """SessionEvent → 事件行对象（data 编码为 JSON 安全结构）。"""
    return {"type": event.type, "seq": event.seq, "time": event.time,
            "data": encode_payload(event.data)}


def _line_to_event(line: dict) -> SessionEvent:
    """事件行对象 → SessionEvent（data 解码还原消息对象）。"""
    return SessionEvent(
        type=line["type"], seq=line["seq"], time=line.get("time", 0.0),
        data=decode_payload(line.get("data")),
    )


class JsonlSessionPersistence(SessionPersistence):
    """JSONL 后端：每个会话一个 ``{dir}/{session_id}.jsonl``。"""

    def __init__(self, ctx: AppContext, dir_path: str) -> None:
        super().__init__(ctx)
        self.dir = dir_path
        os.makedirs(self.dir, exist_ok=True)

    # -- 路径与物化 ------------------------------------------------------- #
    def locate(self, meta: SessionHeader) -> str:
        return os.path.join(self.dir, f"{meta.id}.jsonl")

    def _artifact(self, session_id: str) -> str:
        return os.path.join(self.dir, f"{session_id}.jsonl")

    # -- 写路径 ------------------------------------------------------------ #
    def create(self, meta: SessionHeader) -> None:
        """登记会话：若文件尚未物化，写 header 首行。"""
        path = self._artifact(meta.id)
        if os.path.exists(path):
            return  # 已物化（恢复/重复 create），不覆盖
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(_header_to_line(meta), ensure_ascii=False) + "\n")

    def append(self, session_id: str, events: list[SessionEvent]) -> None:
        """耐久性追加事件行（每次打开-追加-关闭，保证落盘）。"""
        path = self._artifact(session_id)
        with open(path, "a", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(_event_to_line(event), ensure_ascii=False) + "\n")

    # -- 读路径 ------------------------------------------------------------ #
    def load(self, session_id: str) -> Optional[dict]:
        """装载会话：首行 header + 事件行；torn 尾行丢弃，已提交前缀保留。"""
        path = self._artifact(session_id)
        if not os.path.exists(path):
            return None
        lines = []
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if raw:
                    lines.append(raw)
        if not lines:
            return None
        try:
            meta = _line_to_header(json.loads(lines[0]))
        except json.JSONDecodeError as exc:
            raise SessionPersistenceError(f"会话 {session_id!r} 首行 header 损坏：{exc}") from exc

        events: list[SessionEvent] = []
        for raw in lines[1:]:
            try:
                events.append(_line_to_event(json.loads(raw)))
            except json.JSONDecodeError:
                # 仅丢弃 torn 尾（最后一条残缺记录）
                if raw != lines[-1]:
                    raise
        return {"meta": meta, "events": events}

    def list(self) -> list[SessionHeader]:
        """扫目录，读取每个物化会话的首行 header。"""
        headers: list[SessionHeader] = []
        for name in sorted(os.listdir(self.dir)):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(self.dir, name)
            try:
                with open(path, encoding="utf-8") as f:
                    first = f.readline().strip()
                if first:
                    headers.append(_line_to_header(json.loads(first)))
            except (OSError, json.JSONDecodeError, SessionPersistenceError):
                continue  # 单个工件损坏不影响列举其余
        return headers


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：挂载 JSONL 会话持久化后端。

    配置：``{"dir": "..."}``（缺省 ``.dsh/sessions``）。
    挂载后 ``ctx.sessions.create()`` 自动登记、``append`` 自动落盘，
    ``ctx.sessions.resume(id)`` 可恢复历史。
    """
    config = config or {}
    dir_path = config.get("dir") or os.path.join(".dsh", "sessions")
    backend = JsonlSessionPersistence(ctx, dir_path)
    ctx.sessions.attach_persistence(backend)


apply.provides = ["sessionPersistence"]  # 声明：本插件提供持久化服务


# --------------------------------------------------------------------------- #
# SQLite 后端（对标 dsh 的 session-persistence-sqlite）
# --------------------------------------------------------------------------- #
class SqliteSessionPersistence(SessionPersistence):
    """SQLite 后端：单库文件存 ``sessions`` / ``events`` 表；每次 ``append`` 在单
    事务内提交，原子性即天然耐久（崩溃不丢已提交前缀）。可选 zstd 压缩
    （``compression='zstd'``，需安装 ``zstandard``）。支持 ``checkpoint`` 快照：
    ``load`` 优先从最近 checkpoint 续接前缀 + 重放尾部，提供快速恢复路径。
    """

    def __init__(self, ctx: AppContext, db_path: str, compression: str = "none") -> None:
        super().__init__(ctx)
        if compression not in ("none", "zstd"):
            raise SessionPersistenceError(f"不支持的压缩方式：{compression!r}")
        self.db_path = db_path
        self.compression = compression
        db_dir = os.path.dirname(db_path) or "."
        os.makedirs(db_dir, exist_ok=True)
        # check_same_thread=False：持久化调用发生在事件循环线程，统一加锁串行化
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "id TEXT PRIMARY KEY, version INTEGER NOT NULL, created_at REAL NOT NULL, "
                "cwd TEXT, request TEXT, compression TEXT NOT NULL DEFAULT 'none')")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "session_id TEXT NOT NULL, seq INTEGER NOT NULL, type TEXT NOT NULL, "
                "time REAL NOT NULL, data BLOB NOT NULL, PRIMARY KEY (session_id, seq))")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS checkpoints ("
                "session_id TEXT NOT NULL, seq INTEGER NOT NULL, payload BLOB NOT NULL, "
                "created_at REAL NOT NULL, PRIMARY KEY (session_id, seq))")

    # -- 路径与物化 ------------------------------------------------------- #
    def locate(self, meta: SessionHeader) -> str:
        return self.db_path

    # -- 写路径 ------------------------------------------------------------ #
    def create(self, meta: SessionHeader) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions "
                "(id, version, created_at, cwd, request, compression) VALUES (?,?,?,?,?,?)",
                (meta.id, meta.version, meta.created_at, meta.cwd,
                 json.dumps(meta.request) if meta.request is not None else None,
                 self.compression))

    def append(self, session_id: str, events: list[SessionEvent]) -> None:
        rows = [(session_id, e.seq, e.type, e.time,
                 _compress(self.compression,
                           json.dumps(encode_payload(e.data), ensure_ascii=False).encode("utf-8")))
                for e in events]
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO events (session_id, seq, type, time, data) "
                "VALUES (?,?,?,?,?)", rows)

    def checkpoint(self, session_id: str, seq: int, events: list[SessionEvent]) -> None:
        # 覆盖式写最新前缀快照（seq 即快照覆盖到的事件序号）
        payload = json.dumps([_event_to_line(e) for e in events], ensure_ascii=False).encode("utf-8")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO checkpoints (session_id, seq, payload, created_at) "
                "VALUES (?,?,?,?)", (session_id, seq, payload, time.time()))

    def load_checkpoint(self, session_id: str):
        """返回 ``(seq, [SessionEvent])`` 或 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT seq, payload FROM checkpoints WHERE session_id=? "
                "ORDER BY seq DESC LIMIT 1", (session_id,)).fetchone()
        if row is None:
            return None
        seq, payload = row
        raw = json.loads(payload.decode("utf-8"))
        return seq, [_line_to_event(e) for e in raw]

    # -- 读路径 ------------------------------------------------------------ #
    def _read_events(self, session_id: str, after_seq: int) -> list[SessionEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, type, time, data FROM events "
                "WHERE session_id=? AND seq>? ORDER BY seq ASC",
                (session_id, after_seq)).fetchall()
        events: list[SessionEvent] = []
        for seq, etype, etime, raw in rows:
            try:
                data = decode_payload(
                    json.loads(_decompress(self.compression, raw).decode("utf-8")))
            except Exception:
                # 仅丢弃 torn 尾（理论不会发生；防御性，保证已提交前缀可用）
                break
            events.append(SessionEvent(type=etype, seq=seq, time=etime, data=data))
        return events

    def load(self, session_id: str) -> Optional[dict]:
        with self._lock:
            srow = self._conn.execute(
                "SELECT id, version, created_at, cwd, request, compression "
                "FROM sessions WHERE id=?", (session_id,)).fetchone()
        if srow is None:
            return None
        _, version, created_at, cwd, request_raw, _compression = srow
        if version != SESSION_FORMAT_VERSION:
            raise SessionFormatUnsupportedError(
                f"会话 {session_id!r} 版本 {version} 不受支持（当前 {SESSION_FORMAT_VERSION}）")
        meta = SessionHeader(
            version=version, id=session_id, created_at=created_at, cwd=cwd,
            request=json.loads(request_raw) if request_raw is not None else None)
        ck = self.load_checkpoint(session_id)
        if ck is not None:
            ck_seq, prefix = ck
            events = list(prefix)
            events.extend(self._read_events(session_id, after_seq=ck_seq))
        else:
            events = self._read_events(session_id, after_seq=0)
        return {"meta": meta, "events": events}

    def list(self) -> list[SessionHeader]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, version, created_at, cwd, request FROM sessions ORDER BY id").fetchall()
        headers: list[SessionHeader] = []
        for sid, version, created_at, cwd, request_raw in rows:
            if version != SESSION_FORMAT_VERSION:
                continue
            headers.append(SessionHeader(
                version=version, id=sid, created_at=created_at, cwd=cwd,
                request=json.loads(request_raw) if request_raw is not None else None))
        return headers

    def dispose(self) -> None:
        """关闭数据库连接（由插件经 ctx.effect 在卸载时调用）。"""
        try:
            self._conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# checkpoint 策略（对标 dsh 的 checkpoint-policy）
# --------------------------------------------------------------------------- #
class CheckpointPolicy(Service):
    """checkpoint 策略：按 turn 边界周期写前缀快照，提供快速恢复路径。

    订阅 ``session/event``，对 ``assistant/message`` / ``turn/end`` 计数，
    每 ``every_turns`` 次经持久化后端写一份前缀快照；后端 ``load`` 优先从最近
    checkpoint 续接（正确性仍由完整事件日志保证）。
    """

    def __init__(self, ctx: AppContext, every_turns: int = 5) -> None:
        super().__init__(ctx, "checkpointPolicy")
        if not isinstance(every_turns, int) or every_turns < 1:
            raise SessionPersistenceError(f"every_turns 必须为正整数：{every_turns!r}")
        self.every_turns = every_turns
        self._counts: dict[str, int] = {}
        self._off = ctx.on("session/event", self._on_event)

    def _on_event(self, session: Any, event: SessionEvent) -> None:
        if event.type not in ("assistant/message", "turn/end"):
            return
        sid = session.header.id
        self._counts[sid] = self._counts.get(sid, 0) + 1
        if self._counts[sid] >= self.every_turns:
            self._counts[sid] = 0
            self._write_checkpoint(session)

    def _write_checkpoint(self, session: Any) -> None:
        persistence = getattr(session, "_persistence", None)
        if persistence is None or not hasattr(persistence, "checkpoint"):
            return
        events = list(session.events)
        if not events:
            return
        max_seq = max(e.seq for e in events)
        persistence.checkpoint(session.header.id, max_seq, events)

    def dispose(self) -> None:
        try:
            self._off()
        finally:
            self._counts.clear()


# --------------------------------------------------------------------------- #
# 插件入口
# --------------------------------------------------------------------------- #
def apply_sqlite(ctx: AppContext, config: Any = None) -> None:
    """插件入口：挂载 SQLite 会话持久化后端。

    配置：``{"dir": "...", "compression": "none" | "zstd"}``
    （``dir`` 缺省 ``.dsh/sessions``，库文件为 ``<dir>/sessions.db``）。
    挂载后 ``ctx.sessions.create()`` 自动登记、``append`` 自动落盘，
    ``ctx.sessions.resume(id)`` 可恢复历史（并优先从 checkpoint 续接）。
    """
    config = config or {}
    dir_path = config.get("dir") or os.path.join(".dsh", "sessions")
    compression = config.get("compression", "none")
    db_path = os.path.join(dir_path, "sessions.db")
    backend = SqliteSessionPersistence(ctx, db_path, compression)
    ctx.sessions.attach_persistence(backend)
    ctx.effect(backend.dispose)  # 卸载时关闭连接


apply_sqlite.provides = ["sessionPersistence"]  # 声明：本插件提供持久化服务
apply_sqlite.inject = ["sessions"]             # 必须在 sessions 服务之后加载


def apply_checkpoint(ctx: AppContext, config: Any = None) -> None:
    """插件入口：挂载 checkpoint 策略（周期写前缀快照）。

    配置：``{"everyTurns": 5}``（每多少个 turn 边界写一次快照）。
    """
    config = config or {}
    every_turns = config.get("everyTurns", 5)
    policy = CheckpointPolicy(ctx, every_turns=every_turns)
    ctx.effect(policy.dispose)  # 卸载时退订


apply_checkpoint.provides = ["checkpointPolicy"]  # 声明：本插件提供 checkpoint 策略
apply_checkpoint.inject = ["sessions"]            # 需在 sessions 服务之后加载
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
from abc import ABC, abstractmethod
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.message import decode_payload, encode_payload
from dsh_py.services.session import SessionEvent, SessionHeader

# 会话持久化格式版本（对齐 dsh 的 SESSION_FORMAT_VERSION）
SESSION_FORMAT_VERSION = 0


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
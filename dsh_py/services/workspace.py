"""workspace 领域词汇与实体（dsh ``@deepseek-ai/dsh-workspace`` 的 domain 半边）。

- :class:`WorkspaceId` / :class:`SessionId`：本地 NewType 品牌（沿用
  ``workflow/types`` 的先例；``WorkspaceId(x)`` 本身即品牌化构造器）。
- :func:`realpath_normalize`：工作区身份的唯一路径规范（``os.path.realpath``：
  尾斜杠 / ``..`` / 符号链接全部解析；不存在则抛 FileNotFoundError——即
  ``create`` 的拒绝路径）。
- domain spec：一张 ``workspaces`` 表（path/title/sessionIds/createdAt/updatedAt）
  + 全局单例（initialized / workspaceIds / archivedSessionIds / pendingMutation）。
- :class:`WorkspaceEntity`：唯一 :class:`Workspace` 实现；每次耐久变更后原地换
  快照，所有写经 ``mutate`` 收口（``updatedAt`` 打戳 + 失效候选剔除恰好一次）。

适配（dsh_py 差异，均已注明）：
- ``fs.realpath``/``stat`` → 同步 ``os.path.realpath``/``os.stat``（无 async fs；
  量级小，事件循环上直接调用，与 dsh 的 async fs 差异仅传输层）。
- dsh 表更新以「fn 原样返回 current」触发 no-op 哨兵中止写槽；dsh_py 的
  ``KvTableImpl.update`` 会无条件写 fn 的返回值——本实体改以**哨兵异常**中止
  （fn 抛 ``_UnchangedSentinel``，写槽不落盘、不事件，调用方捕获吞掉）。
"""

from __future__ import annotations

import os
from typing import Any, Callable, NewType, Optional

from dsh_py.core import schema as z
from dsh_py.services.storage_domain import define_domain, domain_table

SessionId = NewType("SessionId", str)
WorkspaceId = NewType("WorkspaceId", str)


def realpath_normalize(path: str) -> str:
    """规范化目录路径（尾斜杠/``..``/符号链接全解析）。不存在抛 FileNotFoundError。"""
    return os.path.realpath(path)


# ---------------------------------------------------------------------------
# 领域声明（spec）
# ---------------------------------------------------------------------------

workspace_record = z.object({
    "path": z.string(),
    "title": z.string(),
    "sessionIds": z.array(z.string()),
    "createdAt": z.string(),
    "updatedAt": z.string(),
}, extra="strip")

workspace_pending_mutation = z.object({
    "operation": z.string(),
    "workspaceId": z.string(),
})

workspace_domain_state = z.object({
    "initialized": z.boolean(),
    "workspaceIds": z.array(z.string()),
    "archivedSessionIds": z.array(z.string()),
    "pendingMutation": workspace_pending_mutation.optional(),
}, extra="strip")

workspace_domain_spec = define_domain({
    "name": "workspace",
    "version": 2,
    "global": {
        "schema": workspace_domain_state,
        "initial": {"initialized": False, "workspaceIds": [], "archivedSessionIds": []},
    },
    "tables": {"workspaces": domain_table(workspace_record)},
})


# ---------------------------------------------------------------------------
# 实体
# ---------------------------------------------------------------------------

class WorkspaceMoveInvalidError(Exception):
    """insertSessionBefore 命中了未入账的会话或锚点（存储故障保持普通错误）。"""


class _UnchangedSentinel(Exception):
    """fn 报告记录无需变更时抛出的链槽中止哨兵；仅 mutate 观察它。"""


class WorkspaceEntityHost:
    """实体经其变更的注册表侧机制（open 表 / 会话路径索引 / 头部读取）。"""

    def table(self) -> Any:
        raise NotImplementedError

    def session_path(self, session_id: str) -> Optional[str]:
        raise NotImplementedError

    async def read_session_header(self, session_id: str) -> Any:
        raise NotImplementedError

    def remember_session_path(self, session_id: str, path: str) -> None:
        raise NotImplementedError


class WorkspaceEntity:
    """唯一 :class:`Workspace` 实现；仅由注册表构造。"""

    def __init__(self, host: WorkspaceEntityHost, id: WorkspaceId, record: dict) -> None:
        self._host = host
        self.id = id
        self._record = record

    # -- 只读投影 -----------------------------------------------------------

    @property
    def path(self) -> str:
        return self._record["path"]

    @property
    def title(self) -> str:
        return self._record["title"]

    @property
    def created_at(self) -> str:
        return self._record["createdAt"]

    @property
    def updated_at(self) -> str:
        return self._record["updatedAt"]

    @property
    def session_ids(self) -> list:
        """头部校验后的会话（规范 cwd == 工作区路径）；同步过滤。"""
        return [
            sid for sid in self._record["sessionIds"]
            if self._host.session_path(sid) == self._record["path"]
        ]

    # -- 变更 ---------------------------------------------------------------

    async def set_title(self, title: str) -> None:
        await self._mutate(lambda record: {**record, "title": title})

    async def attach_session(self, session_id: str) -> None:
        # 已入账的 id 跳过校验：cwd 事实在首次挂接时已检查，两个输入
        # （存储头部 cwd、工作区路径）均不可变；成员资格由写链上的 mutate 裁决。
        if session_id not in self._record["sessionIds"]:
            header = await self._host.read_session_header(session_id)
            cwd = header.cwd
            if cwd is None:
                raise RuntimeError(
                    f"cannot attach session '{session_id}' to workspace '{self._record['path']}': "
                    "its stored header carries no cwd to validate against"
                )
            try:
                canonical = realpath_normalize(cwd)
            except OSError as exc:
                raise RuntimeError(
                    f"cannot attach session '{session_id}' to workspace '{self._record['path']}': "
                    f"its cwd '{cwd}' does not resolve, so it cannot be validated"
                ) from exc
            if not os.path.isdir(canonical):
                raise RuntimeError(
                    f"cannot attach session '{session_id}' to workspace '{self._record['path']}': "
                    f"its cwd '{cwd}' is not a directory"
                )
            if canonical != self._record["path"]:
                raise RuntimeError(
                    f"cannot attach session '{session_id}' to workspace '{self._record['path']}': "
                    f"its cwd resolves to '{canonical}'"
                )
            self._host.remember_session_path(session_id, canonical)
        await self._mutate(
            lambda record: record if session_id in record["sessionIds"]
            else {**record, "sessionIds": [session_id, *record["sessionIds"]]}
        )

    async def insert_session_before(self, session_id: str, before_session_id: Optional[str] = None) -> None:
        await self._mutate(lambda record: _move_session(record, session_id, before_session_id))

    async def detach_session(self, session_id: str) -> None:
        await self._mutate(
            lambda record: {**record, "sessionIds": [s for s in record["sessionIds"] if s != session_id]}
            if session_id in record["sessionIds"] else record
        )

    async def status(self) -> str:
        """现场目录检查（不缓存）：目录当前存在且为目录返回 ``'ok'``，否则
        ``'missing-dir'``（记录本身绝不因目录暂时消失而变更）。"""
        try:
            return "ok" if os.path.isdir(self._record["path"]) else "missing-dir"
        except OSError:
            return "missing-dir"

    # -- 唯一写路径 -----------------------------------------------------------

    async def _mutate(self, fn: Callable[[dict], dict]) -> None:
        """在域写链上跑 ``fn``：打 ``updatedAt`` 戳 + 剔除不再通过
        「id + 规范 cwd」成员资格检查的候选，然后原地换快照。

        ``fn`` 看到的是其链槽位的当前值，因此 attach/detach 幂等对排队写无竞态；
        ``fn`` 返回 current 原样（并无可剔除项）时抛哨兵中止链槽——no-op 既不
        重写介质也不发变更事件。
        """
        try:
            next_record = await self._host.table().update(self.id, self._make_update(fn))
        except _UnchangedSentinel:
            return
        self._record = next_record

    def _make_update(self, fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
        def update(current: dict) -> dict:
            changed = fn(current)
            session_ids = [
                sid for sid in changed["sessionIds"]
                if self._host.session_path(sid) == changed["path"]
            ]
            if changed is current and len(session_ids) == len(current["sessionIds"]):
                raise _UnchangedSentinel
            return {**changed, "sessionIds": session_ids, "updatedAt": _now_iso()}

        return update


def _move_session(record: dict, session_id: str, before_session_id: Optional[str]) -> dict:
    """DOM-insertBefore 式移动：有锚点落在其前，无锚点追加到末尾。"""
    if session_id not in record["sessionIds"]:
        raise WorkspaceMoveInvalidError(
            f"cannot move session '{session_id}' in workspace '{record['path']}': the session is not accounted"
        )
    if before_session_id is not None and before_session_id not in record["sessionIds"]:
        raise WorkspaceMoveInvalidError(
            f"cannot move session '{session_id}' before '{before_session_id}' in workspace "
            f"'{record['path']}': the anchor session is not accounted"
        )
    if before_session_id == session_id:
        return record
    without = [s for s in record["sessionIds"] if s != session_id]
    at = len(without) if before_session_id is None else without.index(before_session_id)
    session_ids = [*without[:at], session_id, *without[at:]]
    if session_ids == record["sessionIds"]:
        return record
    return {**record, "sessionIds": session_ids}


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "SessionId",
    "WorkspaceId",
    "realpath_normalize",
    "workspace_record",
    "workspace_domain_state",
    "workspace_domain_spec",
    "WorkspaceMoveInvalidError",
    "WorkspaceEntityHost",
    "WorkspaceEntity",
]

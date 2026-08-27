"""耐久工作区注册表（``ctx.workspaceRegistry``）。

对齐 dsh 的 ``@deepseek-ai/dsh-workspace``：耐久工作区记录、稳定注册表顺序、
经域数据形式（storageDomain）承载的头部校验会话成员资格。

- 启动：打开 ``workspace`` 域 → 恢复未完成变更标记 → 校验已存状态 → 未初始化
  时做一次性历史引导（按规范 cwd 分组会话头部，最晚活动优先，产出初始顺序）。
- 变更：``create``（canonical 路径已存在即复用，新建者前置到注册表顺序）、
  ``delete``（保留目录与会话日志）、``insert_before``（DOM-insertBefore 式）、
  ``archive_session``（注册表全局归档集，不触碰工作区账目）。
- 实体：``set_title``/``attach_session``/``insert_session_before``/``detach_session``/
  ``status``，成员资格 = 入账 id + 会话头部规范 cwd 等于工作区路径。

适配（dsh_py 差异，均已注明）：
- dsh 的 ``Service.init`` 生命周期 → 显式 ``async start()``：插件 apply 创建注册
  表后以 ``asyncio.create_task`` 调度启动；未就绪前 ``require_*`` 抛错（同 dsh），
  异步入口先 ``await self._when_ready()``。
- ``ctx.sessions.list()`` 在 dsh_py 返回 **id 列表**（非 Session 对象）——索引活
  会话时逐个 ``get(id)`` 取 header。
- 会话/工作区 id 为本地 NewType 品牌（str 子类语义）。
"""

from __future__ import annotations

import asyncio
import datetime
import os
import uuid
from typing import Any, Callable, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.workspace import (
    SessionId,
    WorkspaceEntity,
    WorkspaceEntityHost,
    WorkspaceId,
    realpath_normalize,
    workspace_domain_spec,
    workspace_domain_state,
)


class WorkspaceUnknownSessionError(Exception):
    """archiveSession 命中了既不在线也不在持久化中的会话——仅确定未命中；
    存储故障按自身传播。"""

    def __init__(self, session_id: SessionId) -> None:
        self.session_id = session_id
        super().__init__(
            f"cannot archive session '{session_id}': live sessions and session persistence hold no such session"
        )


class WorkspaceOrderInvalidError(Exception):
    """重排命中了不在耐久注册表顺序中的源或锚点。"""

    def __init__(self, workspace_id: WorkspaceId) -> None:
        self.workspace_id = workspace_id
        super().__init__(f"cannot reorder unknown workspace '{workspace_id}'")


def _same_ids(left: list, right: list) -> bool:
    return len(left) == len(right) and all(a == b for a, b in zip(left, right))


def _compare_headers(left: Any, right: Any) -> int:
    """最新在前；同刻按 id 码点比较（dsh 用 localeCompare，差异已注明）。"""
    delta = right.created_at - left.created_at
    if delta != 0:
        return 1 if delta > 0 else -1
    return 1 if str(left.id) > str(right.id) else (-1 if str(left.id) < str(right.id) else 0)


def _header_sort_key(header: Any) -> tuple:
    """会话头部排序键：最晚 created_at 在前，同刻按 id 码点升序。"""
    return (-header.created_at, str(header.id))


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _basename(path: str) -> str:
    return os.path.basename(path.rstrip("/\\")) or path


def _warn(ctx: AppContext, message: str) -> None:
    """防御性告警：无 logger 服务（裸 ctx）时静默。"""
    try:
        ctx.logger.warn(message)
    except Exception:  # noqa: BLE001
        pass


class _RegistryHost(WorkspaceEntityHost):
    """把实体接到注册表侧机制的轻量适配器（open 表 / 会话路径索引 / 头部读取）。"""

    def __init__(self, registry: "WorkspaceRegistry") -> None:
        self._registry = registry

    def table(self) -> Any:
        return self._registry._require_table()

    def session_path(self, session_id: str) -> Optional[str]:
        return self._registry._session_paths.get(session_id)

    async def read_session_header(self, session_id: str) -> Any:
        return await self._registry._read_session_header(session_id)

    def remember_session_path(self, session_id: str, path: str) -> None:
        self._registry._remember_session_path(session_id, path)


class WorkspaceRegistry(Service):
    """耐久工作区注册表（``ctx.workspaceRegistry``）。"""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "workspaceRegistry")
        self._table: Any = None
        self._global: Any = None
        self._state: Optional[dict] = None
        self._entities: dict = {}
        self._headers: dict = {}
        self._session_paths: dict = {}
        self._invalid_session_paths: dict = {}
        self._operation_tail: Optional[Any] = None
        self._ready: Optional[asyncio.Future] = None
        self._host: WorkspaceEntityHost = _RegistryHost(self)

    # -- 生命周期 -----------------------------------------------------------

    async def start(self) -> None:
        """打开域、按需引导、重建顺序缓存（插件 apply 调度；只跑一次）。"""
        if self._ready is not None:
            await self._ready
            return
        self._ready = asyncio.get_running_loop().create_future()
        try:
            domain = await self.ctx.storageDomain.open(workspace_domain_spec)
            try:
                self.ctx.effect(_sync_dispose(domain.close), "workspace.domainClose")
            except Exception:  # noqa: BLE001 -- 无 fiber 环境时仅手动关闭
                pass
            self._table = domain.table("workspaces")
            self._global = domain.global_
            self._state = self._global.get()

            await self._recover_pending_mutation()
            self._validate_stored_state(self._require_state())
            if not self._state["initialized"]:
                headers = await self.ctx.sessionPersistence.list()
                await self._replace_header_index(headers)
                await self._bootstrap(headers)
            elif self._table.size > 0:
                await self._replace_header_index(await self.ctx.sessionPersistence.list())

            await self._index_live_sessions()
            self._validate_stored_state(self._require_state())
            self._rebuild_entities()
            self._report_filtered_candidates()
            self._ready.set_result(None)
        except Exception as exc:  # noqa: BLE001
            if not self._ready.done():
                self._ready.set_exception(exc)
            raise

    # -- 公共 API -----------------------------------------------------------

    async def create(self, path: str, title: Optional[str] = None) -> WorkspaceEntity:
        """为已存在目录创建或复用工作区。路径经 realpath 规范化；不存在抛原错误、
        非目录拒绝；同一规范路径重复调用返回既有实体（不改标题）。新建者前置。"""
        await self._when_ready()
        canonical = await asyncio.to_thread(realpath_normalize, path)
        # dsh 的 fs.realpath 对缺失路径抛 ENOENT（create 的拒绝路径）；os.path.realpath
        # 从不抛——补存在性检查以对齐「原错误拒绝」语义。
        if not os.path.exists(canonical):
            raise FileNotFoundError(2, "No such file or directory", path)
        if not os.path.isdir(canonical):
            raise RuntimeError(f"cannot create a workspace at '{canonical}': path is not a directory")
        return await self._enqueue_operation(lambda: self._create_canonical(canonical, title))

    def get(self, id: WorkspaceId) -> Optional[WorkspaceEntity]:
        """按 id 查工作区；未知返回 None。"""
        return self._entities.get(id)

    def list(self) -> list:
        """耐久注册表顺序的同步投影（各实体 ``session_ids`` 已被启动/在线索引
        过滤；本方法不做持久化读取）。"""
        return [self._require_entity(id) for id in self._require_state()["workspaceIds"]]

    async def delete(self, id: WorkspaceId) -> bool:
        """删除一条工作区注册（保留目录与会话日志）；未知 id 幂等 no-op。"""
        await self._when_ready()
        return await self._enqueue_operation(lambda: self._delete_known(id))

    async def insert_before(self, id: WorkspaceId, before_id: Optional[WorkspaceId] = None) -> list:
        """DOM-insertBefore 式移动：有锚点落在其前，无锚点追加；返回提交后的完整顺序。"""
        await self._when_ready()

        async def op() -> list:
            state = self._require_state()
            if id not in state["workspaceIds"]:
                raise WorkspaceOrderInvalidError(id)
            if before_id is not None and before_id not in state["workspaceIds"]:
                raise WorkspaceOrderInvalidError(before_id)
            if before_id == id:
                return state["workspaceIds"]
            without = [w for w in state["workspaceIds"] if w != id]
            at = len(without) if before_id is None else without.index(before_id)
            workspace_ids = [*without[:at], id, *without[at:]]
            if _same_ids(workspace_ids, state["workspaceIds"]):
                return state["workspaceIds"]
            await self._set_state({**state, "workspaceIds": workspace_ids})
            return workspace_ids

        return await self._enqueue_operation(op)

    @property
    def archived_session_ids(self) -> list:
        """注册表全局归档集：从每个分组表面隐藏的会话（归档不动工作区账目）。"""
        return self._require_state()["archivedSessionIds"]

    async def archive_session(self, session_id: SessionId) -> None:
        """耐久归档一个会话；会话必须存在（在线或在持久化中）；已归档幂等。"""
        await self._when_ready()

        async def op() -> None:
            state = self._require_state()
            if session_id in state["archivedSessionIds"]:
                return
            if not await self._session_known(session_id):
                raise WorkspaceUnknownSessionError(session_id)
            state = self._require_state()
            await self._set_state({**state, "archivedSessionIds": [*state["archivedSessionIds"], session_id]})

        await self._enqueue_operation(op)

    async def resolve_by_path(self, path: str) -> Optional[WorkspaceEntity]:
        """按规范目录路径解析（不创建不变更）；缺失路径在 realpath 时拒绝，
        已存在但未被拥有的目录返回 None。"""
        await self._when_ready()
        canonical = await asyncio.to_thread(realpath_normalize, path)
        for entity in self._entities.values():
            if entity.path == canonical:
                return entity
        return None

    # -- 内部 ---------------------------------------------------------------

    async def _when_ready(self) -> None:
        if self._ready is None:
            raise RuntimeError("workspace registry is not started yet")
        await self._ready

    async def _session_known(self, id: SessionId) -> bool:
        sessions = self._service_or_none("sessions")
        if sessions is not None and sessions.get(id) is not None:
            return True
        if id in self._headers:
            return True
        await self._index_headers(await self.ctx.sessionPersistence.list())
        return id in self._headers

    async def _create_canonical(self, canonical: str, title: Optional[str]) -> WorkspaceEntity:
        for entity in self._entities.values():
            if entity.path == canonical:
                return entity
        table = self._require_table()
        state = self._require_state()
        workspace_id = WorkspaceId(str(uuid.uuid4()))
        now = _now_iso()
        record = {
            "path": canonical,
            "title": title or _basename(canonical),
            "sessionIds": [],
            "createdAt": now,
            "updatedAt": now,
        }
        entity = WorkspaceEntity(self._host, workspace_id, record)
        self._entities[workspace_id] = entity
        pending = {**state, "pendingMutation": {"operation": "create", "workspaceId": workspace_id}}
        try:
            await self._set_state(pending)
        except Exception:
            self._entities.pop(workspace_id, None)
            raise
        try:
            await table.put(workspace_id, record)
        except Exception as error:
            self._entities.pop(workspace_id, None)
            try:
                await self._set_state(state)
            except Exception as rollback_error:
                raise AggregateError(
                    [error, rollback_error],
                    f"workspace '{workspace_id}' record write and pending-marker rollback both failed",
                ) from error
            raise
        try:
            await self._set_state({
                "initialized": True,
                "workspaceIds": [workspace_id, *state["workspaceIds"]],
                "archivedSessionIds": state["archivedSessionIds"],
            })
        except Exception as error:
            self._entities.pop(workspace_id, None)
            try:
                await table.delete(workspace_id)
            except Exception as rollback_error:
                raise AggregateError(
                    [error, rollback_error],
                    f"workspace '{workspace_id}' order write and record rollback both failed; "
                    "the pending marker remains recoverable",
                ) from error
            try:
                await self._set_state(state)
            except Exception as rollback_error:
                raise AggregateError(
                    [error, rollback_error],
                    f"workspace '{workspace_id}' order write and pending-marker rollback both failed",
                ) from error
            raise
        return entity

    async def _delete_known(self, id: WorkspaceId) -> bool:
        entity = self._entities.get(id)
        if entity is None:
            return False
        state = self._require_state()
        next_state = {
            "initialized": True,
            "workspaceIds": [w for w in state["workspaceIds"] if w != id],
            "archivedSessionIds": state["archivedSessionIds"],
        }
        await self._set_state({**next_state, "pendingMutation": {"operation": "delete", "workspaceId": id}})
        self._entities.pop(id, None)
        try:
            await self._require_table().delete(id)
        except Exception as error:
            self._entities[id] = entity
            try:
                await self._set_state(state)
            except Exception as rollback_error:
                self._entities.pop(id, None)
                raise AggregateError(
                    [error, rollback_error],
                    f"workspace '{id}' record deletion and registry-order rollback both failed",
                ) from error
            raise
        try:
            await self._set_state(next_state)
        except Exception as error:  # noqa: BLE001 -- 删除已提交，标记清理失败只告警
            _warn(self.ctx, f"workspace '{id}' was deleted but its pending marker could not be cleared: {error}")
        return True

    async def _recover_pending_mutation(self) -> None:
        """完成耐久状态显式命名的唯一一次变更；无法解释的顺序/表分歧仍由
        validate 响亮失败——绝不从行的形状猜测操作。"""
        state = self._require_state()
        pending = state.get("pendingMutation")
        if pending is None:
            return
        if pending["workspaceId"] in state["workspaceIds"]:
            raise RuntimeError(
                "workspace domain is inconsistent: pending "
                f"{pending['operation']} workspace '{pending['workspaceId']}' is still present in registry order"
            )
        await self._require_table().delete(pending["workspaceId"])
        await self._set_state({
            "initialized": state["initialized"],
            "workspaceIds": state["workspaceIds"],
            "archivedSessionIds": state["archivedSessionIds"],
        })

    async def _bootstrap(self, headers: list) -> None:
        """一次性历史引导：按规范 cwd 分组会话头部，最晚活动组优先，产出初始顺序。"""
        table = self._require_table()
        state = self._require_state()
        groups_by_path: dict = {}
        for header in headers:
            path = self._session_paths.get(header.id)
            if path is None:
                continue
            groups_by_path.setdefault(path, []).append(header)
        groups = []
        for path, group in groups_by_path.items():
            ordered = sorted(group, key=_header_sort_key)
            groups.append({"path": path, "headers": ordered, "newestAt": ordered[0].created_at})
        groups.sort(key=lambda g: (-g["newestAt"], g["path"]))

        by_path: dict = {}
        accounted: dict = {}
        for record_id, record in table.entries():
            by_path[record["path"]] = record_id
            for session_id in record["sessionIds"]:
                accounted[session_id] = record_id

        for group in groups:
            group_id = by_path.get(group["path"])
            if group_id is None:
                session_ids = [
                    h.id for h in group["headers"]
                    if h.id not in accounted
                ]
                if not session_ids:
                    continue
                group_id = WorkspaceId(str(uuid.uuid4()))
                created = _now_iso_from_ts(group["newestAt"])
                await table.put(group_id, {
                    "path": group["path"],
                    "title": _basename(group["path"]),
                    "sessionIds": session_ids,
                    "createdAt": created,
                    "updatedAt": created,
                })
                by_path[group["path"]] = group_id
                for session_id in session_ids:
                    accounted[session_id] = group_id
                continue

            current = table.get(group_id)
            historical = [
                h.id for h in group["headers"]
                if accounted.get(h.id) in (None, group_id)
            ]
            historical_set = set(historical)
            session_ids = [*historical, *[s for s in current["sessionIds"] if s not in historical_set]]
            if _same_ids(current["sessionIds"], session_ids):
                continue
            await table.update(group_id, lambda record, ids=session_ids: {
                **record, "sessionIds": ids, "updatedAt": _now_iso(),
            })
            for session_id in historical:
                accounted[session_id] = group_id

        group_rank = {g["path"]: g["newestAt"] for g in groups}
        prior_rank = {w: i for i, w in enumerate(state["workspaceIds"])}
        # 排序语义（对齐 dsh）：时间降序 → 既有顺序索引升序 → id 码点升序
        workspace_ids = [
            record_id
            for record_id, record in sorted(
                table.entries(),
                key=lambda kv: _bootstrap_sort_key(kv, group_rank, prior_rank),
            )
        ]
        if not _same_ids(state["workspaceIds"], workspace_ids):
            await self._set_state({
                "initialized": False,
                "workspaceIds": workspace_ids,
                "archivedSessionIds": state["archivedSessionIds"],
            })
        await self._set_state({
            "initialized": True,
            "workspaceIds": workspace_ids,
            "archivedSessionIds": state["archivedSessionIds"],
        })

    def _validate_stored_state(self, state: dict) -> None:
        table = self._require_table()
        order: set = set()
        for wid in state["workspaceIds"]:
            if wid in order:
                raise RuntimeError(f"workspace domain is inconsistent: registry order repeats workspace '{wid}'")
            if table.get(wid) is None:
                raise RuntimeError(f"workspace domain is inconsistent: registry order references missing workspace '{wid}'")
            order.add(wid)
        if state["initialized"] and len(order) != table.size:
            orphan = next((k for k in table.keys() if k not in order), None)
            raise RuntimeError(
                f"workspace domain is inconsistent: workspace '{orphan}' is absent from registry order"
            )
        paths: dict = {}
        accounted: dict = {}
        for record_id, record in table.entries():
            path_holder = paths.get(record["path"])
            if path_holder is not None:
                raise RuntimeError(
                    f"workspace domain is inconsistent: path '{record['path']}' is claimed "
                    f"by both workspace '{path_holder}' and workspace '{record_id}'"
                )
            paths[record["path"]] = record_id
            for session_id in record["sessionIds"]:
                holder = accounted.get(session_id)
                if holder is not None:
                    raise RuntimeError(
                        f"workspace domain is inconsistent: session '{session_id}' is accounted "
                        f"by both workspace '{holder}' and workspace '{record_id}'"
                    )
                accounted[session_id] = record_id

    def _rebuild_entities(self) -> None:
        self._entities.clear()
        for wid in self._require_state()["workspaceIds"]:
            record = self._require_table().get(wid)
            self._entities[wid] = WorkspaceEntity(self._host, wid, record)

    async def _replace_header_index(self, headers: list) -> None:
        self._headers.clear()
        self._session_paths.clear()
        self._invalid_session_paths.clear()
        await self._index_headers(headers)

    async def _index_headers(self, headers: list) -> None:
        for header in headers:
            await self._index_header(header)

    async def _index_header(self, header: Any) -> None:
        self._headers[header.id] = header
        self._session_paths.pop(header.id, None)
        if header.cwd is None:
            self._invalid_session_paths[header.id] = "header has no cwd"
            return
        try:
            path = await asyncio.to_thread(realpath_normalize, header.cwd)
            if not os.path.isdir(path):
                self._invalid_session_paths[header.id] = f"cwd '{header.cwd}' is not a directory"
                return
            self._session_paths[header.id] = path
            self._invalid_session_paths.pop(header.id, None)
        except Exception:  # noqa: BLE001 -- 目录不可解析
            self._invalid_session_paths[header.id] = f"cwd '{header.cwd}' does not resolve"

    async def _index_live_sessions(self) -> None:
        sessions = self._service_or_none("sessions")
        if sessions is None:
            return
        # dsh_py：sessions.list() 返回 id 列表（非 Session 对象）——逐个 get 取 header
        for sid in sessions.list():
            session = sessions.get(sid)
            if session is not None:
                await self._index_header(session.header)

    def _report_filtered_candidates(self) -> None:
        table = self._require_table()
        for entity in self._entities.values():
            record = table.get(entity.id)
            for session_id in record["sessionIds"]:
                path = self._session_paths.get(session_id)
                if path == record["path"]:
                    continue
                reason = (
                    self._invalid_session_paths.get(session_id)
                    or (
                        f"canonical cwd '{path}' differs from workspace path '{record['path']}'"
                        if session_id in self._headers
                        else "session header is missing"
                    )
                )
                _warn(
                    self.ctx,
                    f"workspace '{entity.id}' filtered session '{session_id}' from membership: {reason}",
                )

    async def _read_session_header(self, id: SessionId) -> Any:
        sessions = self._service_or_none("sessions")
        if sessions is not None:
            live = sessions.get(id)
            if live is not None:
                self._headers[id] = live.header
                return live.header
        cached = self._headers.get(id)
        if cached is not None:
            return cached
        headers = await self.ctx.sessionPersistence.list()
        await self._index_headers(headers)
        header = self._headers.get(id)
        if header is None:
            raise RuntimeError(f"cannot validate session '{id}': session persistence holds no such session")
        return header

    def _remember_session_path(self, id: SessionId, path: str) -> None:
        self._session_paths[id] = path
        self._invalid_session_paths.pop(id, None)

    def _require_table(self) -> Any:
        if self._table is None:
            raise RuntimeError("workspace registry is not started yet")
        return self._table

    def _require_state(self) -> dict:
        if self._state is None:
            raise RuntimeError("workspace registry is not started yet")
        return self._state

    def _require_entity(self, id: WorkspaceId) -> WorkspaceEntity:
        entity = self._entities.get(id)
        if entity is None:
            raise RuntimeError(f"workspace registry order references missing workspace '{id}'")
        return entity

    async def _set_state(self, state: dict) -> None:
        await self._global.set(state)
        self._state = state

    def _enqueue_operation(self, operation: Callable[[], Any]) -> Any:
        async def run() -> Any:
            await self._when_ready()
            await self._recover_pending_mutation()
            return await operation()

        if self._operation_tail is None:
            tail = asyncio.get_running_loop().create_future()
            tail.set_result(None)
            self._operation_tail = tail
        result = _chain(self._operation_tail, run)
        self._operation_tail = _settle(result)
        return result

    def _service_or_none(self, name: str) -> Any:
        return getattr(self.ctx, name) if self.ctx.has_service(name) else None


def _chain(tail: Any, run: Callable[[], Any]) -> Any:
    """把 ``run`` 接到操作尾巴之后（dsh 的 promise 链语义）。"""

    async def chained() -> Any:
        try:
            await tail
        except Exception:  # noqa: BLE001 -- 前序失败不阻断后续操作
            pass
        return await run()

    return chained()


def _settle(coro: Any) -> Any:
    """记录尾巴：吞掉结果与异常，只保留「已完成」状态。以 task 承载，
    避免「未 await 的协程」警告（尾巴可能没有下一个操作去 await 它）。"""

    async def settled() -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001
            pass

    return asyncio.ensure_future(settled())


def _sync_dispose(close: Callable[[], Any]) -> Callable[[], None]:
    """把 async ``close`` 包装成 fiber 清理可用的同步 disposer（best-effort）。"""

    def dispose() -> None:
        try:
            import asyncio

            asyncio.get_running_loop().create_task(close())
        except RuntimeError:  # noqa: PERF203 -- 无运行循环时跳过（进程退出期）
            pass

    return dispose


def _now_iso_from_ts(timestamp: float) -> str:
    return datetime.datetime.fromtimestamp(timestamp / 1000, tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _bootstrap_sort_key(kv: Any, group_rank: dict, prior_rank: dict) -> tuple:
    """注册表顺序排序键：``(-最近活动时间, 既有索引, id)``——时间降序、既有顺序
    升序、id 码点升序（对齐 dsh 的 comparator 语义）。"""
    record_id, record = kv
    left_time = group_rank.get(record["path"])
    if left_time is None:
        try:
            left_time = datetime.datetime.fromisoformat(record["createdAt"].replace("Z", "+00:00")).timestamp() * 1000
        except Exception:  # noqa: BLE001
            left_time = 0.0
    rank = prior_rank.get(record_id)
    if rank is None:
        rank = 2**53
    return (-left_time, rank, record_id)


__all__ = [
    "WorkspaceRegistry",
    "WorkspaceUnknownSessionError",
    "WorkspaceOrderInvalidError",
    "apply",
]


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：创建注册表并调度异步启动（``ctx.workspaceRegistry``）。

    需要 ``storageDomain`` + ``sessionPersistence`` + ``sessions`` 服务（inject
    声明）；未挂载时抛错。
    """
    if not ctx.has_service("storageDomain"):
        raise RuntimeError("workspace: the storageDomain service is not mounted (add dsh_py.services.storage + storage_domain)")
    if not ctx.has_service("sessionPersistence"):
        raise RuntimeError(
            "workspace: the sessionPersistence service is not mounted "
            "(add dsh_py.services.session_persistence:apply)"
        )
    registry = WorkspaceRegistry(ctx)
    try:
        asyncio.get_running_loop().create_task(registry.start())
    except RuntimeError:  # noqa: PERF203 -- 无运行循环（同步装配期）时由调用方显式 start
        pass


apply.inject = ["storageDomain", "sessionPersistence", "sessions"]  # 声明：供 loader 拓扑排序
apply.provides = ["workspaceRegistry"]

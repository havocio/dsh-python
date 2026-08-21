"""进程本地任务注册表（jobs/jobs-local，第 3 层）。

``ctx.jobs`` 的内存实现：全部记录存内存，对外只发**新鲜快照**，绝不给出活状态。

- 注册表存活超过生产者与控制器 fiber；所有者/服务销毁时取消活工作并等待合规
  生产者（抛错的 teardown cancel 只把记录 force-fail，报告可能孤儿）；
- 任务 id 生成 ``<kind>-N``；所有者隔离按 session id 栅栏（可预测 id 依赖
  授权而非保密）；
- settle 首胜：一个终态、释放等待者、一轮 contained 完成监听；完成最后宣布。

**与 dsh 的差异（已注明）**：
- dsh 用 ``ScopedLayers`` 按注册作用域分层（全局层 + owner 作用域链）；
  dsh_py 简化为**全局**控制器/监听器/变更观察集合（注册即服务进程全部 owner）；
- dsh 的 ``ensureOwnerCleanup`` 经 owner 上下文 effect 挂每所有者清理；
  dsh_py 跳过 per-owner 清理（服务级 ``dispose_all`` 统一清理；待 agent
  生命周期缝补齐后对齐）；
- dsh 的 ``wait`` 用 scoped deadline 区分超时与调用方取消；dsh_py 用
  ``asyncio.wait`` 三路（settle / 取消 / 超时）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.services.jobs import JobHooks, JobId, JobRegistry, _is_terminal

logger = logging.getLogger("dsh_py.jobs_local")

# 有界等待的超时码（区分等待超时与调用方取消）
TASK_WAIT_TIMEOUT = "TASK_WAIT_TIMEOUT"

# 每个精确 owner 桶的默认最大活动任务数
DEFAULT_MAX_CONCURRENT_JOBS_PER_OWNER = 10

Config = z.object({
    "maxConcurrentJobsPerOwner": z.integer().default(DEFAULT_MAX_CONCURRENT_JOBS_PER_OWNER),
})


class _TrackedJob:
    """注册表的可变记录（绝不对外——见 :meth:`LocalJobRegistry.snapshot`）。"""

    def __init__(self, id: JobId, kind: str, label: str, output_limit_bytes: Optional[int],
                 owner: Any, hooks: JobHooks) -> None:
        self.id = id
        self.kind = kind
        self.label = label
        self.outputLimitBytes = output_limit_bytes
        self.owner = owner
        self.cancel = hooks.cancel
        self.read_output = getattr(hooks, "readOutput", None)
        self.status = "running"
        self.detail: Optional[str] = None
        self.output: Optional[str] = None
        self.startedAt = int(time.time() * 1000)
        self.finishedAt: Optional[int] = None
        self.reported = False
        # 终态记录并通知监听器后 resolve（惰性创建：start 可能无 running loop）
        self.settled: Optional[asyncio.Future] = None
        self.waiters = 0

    def settled_future(self) -> asyncio.Future:
        if self.settled is None:
            self.settled = asyncio.get_event_loop().create_future()
        return self.settled

    def mark_settled(self) -> None:
        future = self.settled_future()
        if not future.done():
            future.set_result(None)


class LocalJobRegistry(JobRegistry):
    """内存 ``jobs`` 注册表（见 seam 契约的拥有/隔离/生命周期语义）。"""

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx, "jobs")
        cfg = config or {}
        self.max_concurrent = int(cfg.get("maxConcurrentJobsPerOwner", DEFAULT_MAX_CONCURRENT_JOBS_PER_OWNER))
        self._store: dict[JobId, _TrackedJob] = {}
        self._counters: dict[str, int] = {}
        self._controllers: set[str] = set()
        self._listeners: list[Callable] = []
        self._changed: list[Callable] = []
        self._listeners_closed = False
        self.ctx.effect(lambda: asyncio.ensure_future(self.dispose_all()), label="jobs.disposeAll")

    # ------------------------------------------------------------------ #
    # 注册与生命周期
    # ------------------------------------------------------------------ #
    def start(self, spec: dict) -> JobId:
        if not self._controllers:
            raise RuntimeError(
                "后台任务不可用：没有任务控制器服务此 agent（在其装配中加载 tool-jobs）",
            )
        kind = spec["kind"]
        label = spec["label"]
        if kind == "":
            raise ValueError("invalid job kind: 应为非空字符串")
        if label == "":
            raise ValueError("invalid job label: 应为非空字符串")
        limit = spec.get("outputLimitBytes")
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0):
            raise ValueError(f"invalid outputLimitBytes: 应为正整数，得到 {limit!r}")
        owner = spec.get("owner")

        active = self._active_count(owner)
        if active >= self.max_concurrent:
            raise RuntimeError(
                f"此 owner 的后台任务上限已达（limit: {self.max_concurrent}）；"
                "用 job_kill 停掉不需要的任务、等它结束，然后重试",
            )

        hooks = spec["run"]()
        count = self._counters.get(kind, 0) + 1
        self._counters[kind] = count
        id = JobId(f"{kind}-{count}")
        job = _TrackedJob(id, kind, label, limit, owner, hooks)
        self._store[id] = job

        outcome_future = asyncio.ensure_future(_as_awaitable(hooks.done))

        def _on_done(done_future: asyncio.Future) -> None:
            if done_future.cancelled():
                return
            error = done_future.exception()
            if error is not None:
                # 容器化生产者契约违反（done reject）：清理与等待者不得悬挂
                logger.warning("jobs: 任务 %s 生产者 done 拒绝（契约违反）：%r", job.id, error)
                self._settle(job, {"status": "failed", "detail": str(error)})
                return
            self._settle(job, done_future.result())

        outcome_future.add_done_callback(_on_done)
        # 注册完成不可失败 → 可见集真实变化
        self._notify_changed(job.owner)
        return id

    def list(self, caller: Any = None) -> list:
        session = getattr(caller, "id", None)
        return [
            self._snapshot(job)
            for job in self._store.values()
            if job.owner is None or job.owner.id == session
        ]

    def get(self, id: JobId, caller: Any = None) -> dict:
        job = self._expect(id)
        self._assert_access(job, caller)
        return self._snapshot(job)

    def read(self, id: JobId, caller: Any = None) -> dict:
        job = self._expect(id)
        self._assert_access(job, caller)
        text = ""
        if job.read_output is not None:
            text = job.read_output()
        elif _is_terminal(job.status):
            text = job.output or ""
        if _is_terminal(job.status):
            job.reported = True
        return {"text": text, "snapshot": self._snapshot(job)}

    def kill(self, id: JobId, caller: Any = None, reason: Optional[str] = None) -> str:
        job = self._expect(id)
        self._assert_access(job, caller)
        if _is_terminal(job.status):
            job.reported = True
            return "already-finished"
        # 先取消：抛错时生命周期与通知状态都保持不变
        job.cancel(reason)
        job.status = "stopping"
        job.reported = True
        self._notify_changed(job.owner)
        return "requested"

    async def wait(self, id: JobId, timeout_ms: float, caller: Any = None, signal: Any = None) -> dict:
        job = self._expect(id)
        self._assert_access(job, caller)
        if not (isinstance(timeout_ms, (int, float)) and timeout_ms > 0):
            raise ValueError(f"invalid wait timeout: 应为正毫秒数，得到 {timeout_ms!r}")
        if not _is_terminal(job.status):
            if signal is not None and getattr(signal, "aborted", False):
                raise RuntimeError("wait aborted")
            job.waiters += 1
            try:
                await self._await_settlement(job, timeout_ms, signal)
            finally:
                job.waiters -= 1
        if _is_terminal(job.status):
            job.reported = True
        return self._snapshot(job)

    async def _await_settlement(self, job: _TrackedJob, timeout_ms: float, signal: Any) -> None:
        """三路等待：settle / 调用方取消 / 超时。"""
        loop = asyncio.get_event_loop()
        signal_future: Optional[asyncio.Future] = None
        remove_listener: Optional[Callable] = None
        if signal is not None and hasattr(signal, "add_listener"):
            signal_future = loop.create_future()

            def _on_abort() -> None:
                if signal_future is not None and not signal_future.done():
                    signal_future.set_result(True)

            remove_listener = signal.add_listener(_on_abort)
        try:
            pending = {job.settled_future()}
            if signal_future is not None:
                pending.add(signal_future)
            done_set, _ = await asyncio.wait(
                pending, timeout=timeout_ms / 1000.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if job.settled_future() in done_set:
                return
            if signal_future is not None and signal_future in done_set:
                raise RuntimeError("wait aborted")
            # 超时：返回当前 snapshot（不取消任务）
        finally:
            if remove_listener is not None:
                remove_listener()

    def onJobDone(self, listener: Callable[[dict, Any], Any]) -> Callable[[], None]:
        self._listeners.append(listener)

        def dispose() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        self.ctx.effect(dispose, label="jobs.onJobDone()")
        return dispose

    def onJobsChanged(self, listener: Callable[[Any], None]) -> Callable[[], None]:
        self._changed.append(listener)

        def dispose() -> None:
            if listener in self._changed:
                self._changed.remove(listener)

        self.ctx.effect(dispose, label="jobs.onJobsChanged()")
        return dispose

    def attachController(self, name: str) -> Callable[[], None]:
        token = f"{name}:{id(self)}"
        self._controllers.add(token)

        def dispose() -> None:
            self._controllers.discard(token)

        self.ctx.effect(dispose, label="jobs.attachController()")
        return dispose

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _active_count(self, owner: Any) -> int:
        return sum(
            1 for job in self._store.values()
            if job.owner is owner and job.status in ("running", "stopping")
        )

    def _expect(self, id: JobId) -> _TrackedJob:
        job = self._store.get(id)
        if job is None:
            raise RuntimeError(f"未知任务 {id}")
        return job

    def _assert_access(self, job: _TrackedJob, caller: Any) -> None:
        if job.owner is not None and job.owner.id != getattr(caller, "id", None):
            raise RuntimeError(f"任务 {job.id} 属于另一个会话")

    def _snapshot(self, job: _TrackedJob) -> dict:
        snapshot = {
            "id": job.id, "kind": job.kind, "label": job.label,
            "status": job.status, "startedAt": job.startedAt, "reported": job.reported,
        }
        if job.outputLimitBytes is not None:
            snapshot["outputLimitBytes"] = job.outputLimitBytes
        if job.owner is not None:
            snapshot["ownerSession"] = job.owner.id
        if job.detail is not None:
            snapshot["detail"] = job.detail
        if job.finishedAt is not None:
            snapshot["finishedAt"] = job.finishedAt
        return snapshot

    def _notify_changed(self, owner: Any) -> None:
        for listener in list(self._changed):
            try:
                listener(owner)
            except Exception as exc:  # noqa: BLE001
                logger.warning("jobs: onJobsChanged 监听器抛错：%r", exc)

    def _settle(self, job: _TrackedJob, outcome: dict) -> None:
        """记录首个终态、释放等待者、宣布完成（首胜：守住 teardown force-fail）。"""
        if _is_terminal(job.status):
            return
        job.status = outcome["status"]
        job.detail = outcome.get("detail")
        job.output = outcome.get("output")
        job.finishedAt = int(time.time() * 1000)
        if job.waiters > 0:
            job.reported = True
        snapshot = self._snapshot(job)
        job.mark_settled()
        self._notify_changed(job.owner)
        if self._listeners_closed:
            return
        for listener in list(self._listeners):
            try:
                returned = listener(snapshot, job.owner)
                if asyncio.iscoroutine(returned) or isinstance(returned, asyncio.Future):
                    future = asyncio.ensure_future(returned)

                    def _observe(done_future: asyncio.Future) -> None:
                        if done_future.cancelled():
                            return
                        error = done_future.exception()
                        if error is not None:
                            logger.warning("jobs: onJobDone 监听器对 %s 拒绝：%r", job.id, error)

                    future.add_done_callback(_observe)
            except Exception as exc:  # noqa: BLE001
                logger.warning("jobs: onJobDone 监听器对 %s 抛错：%r", job.id, exc)

    async def dispose_all(self) -> None:
        """关监听、取消活任务、等待 settle、清空并通知。"""
        self._listeners_closed = True
        all_jobs = list(self._store.values())
        for job in all_jobs:
            if _is_terminal(job.status):
                continue
            # teardown 取消认领终态报告（所有者/服务正被销毁，无读者）
            job.reported = True
            try:
                job.cancel("jobs service disposed")
                job.status = "stopping"
                self._notify_changed(job.owner)
            except Exception as exc:  # noqa: BLE001
                detail = f"cancel threw during teardown; work may be orphaned: {exc}"
                logger.warning("jobs: teardown 中取消 %s 抛错，记录 force-fail：%r", job.id, exc)
                self._settle(job, {"status": "failed", "detail": detail})
        await asyncio.gather(*(job.settled_future() for job in all_jobs), return_exceptions=True)
        self._store.clear()


async def _as_awaitable(value: Any) -> Any:
    """把 hooks.done（awaitable 或已 resolve 值）归一为可等待协程。"""
    if asyncio.iscoroutine(value):
        return await value
    if isinstance(value, asyncio.Future):
        return await value
    return value


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：注册 ``ctx.jobs``（进程本地注册表）。"""
    LocalJobRegistry(ctx, config or {})


apply.Config = Config
apply.name = "jobs-local"

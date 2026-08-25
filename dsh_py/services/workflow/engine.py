"""内联 asyncio workflow 引擎（对标 dsh 的 ``workflow-worker-thread``，进程内折叠版）。

dsh 用 worker_threads 把每个运行放进独立线程：可强杀（``worker.terminate()``）、
有 vm 同步超时、跨线程结构化克隆。既定取舍（用户拍板）是**进程内 asyncio
内联引擎**：把 ``host.ts`` + ``session.ts`` + ``worker.ts`` 折叠为一个
:class:`InlineRun` 协程管理器，``ChildPort`` 直连 ``ctx.subagents``，脚本在
当前事件循环内 ``exec`` 执行。因此：

- **失去**同步死循环的强杀与 ``syncTimeoutMs``（内联无法中断同步 spin）；
  取消只能靠 hook 检查点在下一个 await 处生效；停泊脚本由 grace 强制终止
  （结果按 ``cancelled`` 解决并取消 drive 任务，任务本体仍是事件循环的悬挂
  协程——已文档化接受）。
- 事件合成、agent-end 恰好一次配对、取消/释放状态机与子静默等待均 1:1
  对齐 dsh 的 host 语义（见各方法 docstring）。
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.core.signal import CancelSignal

from . import WorkflowEngine, WorkflowError
from .meta import validate_meta
from .port import ChildHandle, ChildPort, ChildResult, ChildStartRequest, WorkerLimits
from .realm import render_thrown
from .runtime import WorkflowExecution
from .types import (
    SessionId,
    WorkflowAgentEndInfo,
    WorkflowAgentInfo,
    WorkflowMeta,
    WorkflowResult,
    WorkflowResultInfo,
    WorkflowRunId,
    WorkflowRunInfo,
    WorkflowStartRequest,
    WorkflowRun,
)

#: 仍带着 Claude Code 风格 meta 头的 body（meta 在这里作为数据乘坐请求字段）。
_META_STATEMENT = re.compile(r"^\s*export\s+const\s+meta\b")


def assert_body_parses(body: str, name: str) -> None:
    """用与运行时**完全相同**的包装器对 body 做解析检查，使 ``start()`` 保持
    seam 的同步 ``SCRIPT_PARSE`` 抛错（运行时自己的编译发生在运行期）。开头带
    ``export const meta`` 的 body 给出指向性消息而非裸 SyntaxError——这是模型
    最可能的作者错误。
    """
    if _META_STATEMENT.search(body):
        raise WorkflowError(
            "workflow meta rides the `meta` request field, not the script: remove the "
            "`export const meta = {...}` statement from the body",
            "SCRIPT_PARSE",
        )
    import textwrap

    wrapped = "async def __workflow__():\n" + textwrap.indent(body, "    ")
    try:
        compile(wrapped, f"workflow:{name}", "exec")
    except SyntaxError as error:
        raise WorkflowError(f"workflow script does not parse: {error}", "SCRIPT_PARSE", cause=error) from error


def resolve_subagent_provider(ctx: AppContext, configured: str, override: str | None) -> str:
    """解析一次运行的 provider 路由（发布工作前校验）。"""
    provider = override if override is not None else configured
    if len(provider) == 0 or provider != provider.strip():
        raise WorkflowError("workflow subagentProvider must be a non-empty normalized string", "INVALID_ARGUMENT")
    if ctx.subagents.get_provider(provider) is None:
        raise WorkflowError(f'no subagent provider registered for "{provider}"', "AGENT_START")
    return provider


def resolve_max_total_agents(requested: int | None, ceiling: int) -> int:
    """把一次运行的总子代理上限解析到引擎部署天花板内。"""
    if requested is None:
        return ceiling
    if not isinstance(requested, int) or requested < 1:
        raise WorkflowError("workflow maxTotalAgents must be a positive safe integer", "INVALID_ARGUMENT")
    if requested > ceiling:
        raise WorkflowError(f"workflow maxTotalAgents {requested} exceeds the engine ceiling {ceiling}", "INVALID_ARGUMENT")
    return requested


def resolve_config(config: Any) -> dict:
    """解析引擎配置（全可选，带默认值）。"""
    config = config or {}
    provider = config.get("provider", "spawn")
    max_concurrent = int(config.get("maxConcurrentAgents", 0) or 0)
    max_total = int(config.get("maxTotalAgents", 1000) or 1000)
    max_items = int(config.get("maxItemsPerCall", 4096) or 4096)
    sync_timeout = int(config.get("syncTimeoutMs", 5000) or 5000)
    dispose_grace = int(config.get("disposeGraceMs", 5000) or 5000)
    if len(provider) == 0 or provider != provider.strip():
        raise ValueError("workflow provider must be a non-empty normalized string")
    if max_concurrent < 0:
        raise ValueError("workflow maxConcurrentAgents must be non-negative")
    if max_total < 1:
        raise ValueError("workflow maxTotalAgents must be a positive integer")
    if max_items < 1:
        raise ValueError("workflow maxItemsPerCall must be a positive integer")
    if sync_timeout < 1:
        raise ValueError("workflow syncTimeoutMs must be a positive integer")
    if dispose_grace < 1:
        raise ValueError("workflow disposeGraceMs must be a positive integer")
    return {
        "provider": provider,
        "maxConcurrentAgents": max_concurrent,
        "maxTotalAgents": max_total,
        "maxItemsPerCall": max_items,
        "syncTimeoutMs": sync_timeout,
        "disposeGraceMs": dispose_grace,
    }


class _InlineChildHandle(ChildHandle):
    """运行时消费的子句柄：id + 投影后的 :class:`ChildResult` + 共享释放事务。"""

    def __init__(self, run: "InlineRun", sub_run: Any) -> None:
        self.id: str = str(sub_run.id)
        self._run = run
        self._sub_run = sub_run

    @property
    def result(self):
        async def _project() -> ChildResult:
            result = await self._sub_run.result
            return ChildResult(output=result.output, structured=result.structured, stopReason=result.stopReason)

        return _project()

    async def dispose(self) -> None:
        await self._run.dispose_child(self._sub_run)


class _InlineChildPort(ChildPort):
    """运行管理器拥有的子启动端口——直连 ``ctx.subagents``（无 RPC）。"""

    def __init__(self, run: "InlineRun") -> None:
        self._run = run

    async def start_agent(self, request: ChildStartRequest) -> ChildHandle:
        run = self._run
        failure = run.child_admission_failure()
        if failure is not None:
            # 终态边界后拒绝：子绝不能在已中止/已终止的运行上启动。
            raise WorkflowError(failure["rendered"], "AGENT_START")
        run.host_started += 1
        task = asyncio.create_task(run.start_child(request))
        run.pending_starts.add(task)
        try:
            return await task
        finally:
            run.pending_starts.discard(task)
            run.notify_quiescence()


class InlineRun(WorkflowRun):
    """一次活的内联运行——seam 的 ``WorkflowRun``，由 ``start()`` 直接返回。

    拥有执行、子注册表与结果终止；``result`` 永不拒绝。``meta`` 是可信的
    同进程数据，作为不可变借用被句柄与生命周期事件携带。
    """

    def __init__(
        self,
        ctx: AppContext,
        engine: "InlineWorkflowEngine",
        id: WorkflowRunId,
        meta: WorkflowMeta,
        parent: Any,
        execution: WorkflowExecution,
        subagents: Any,
        provider: str,
        dispose_grace_ms: int,
        signal: Any,
    ) -> None:
        self._ctx = ctx
        self._engine = engine
        self.id = id
        self.meta = meta
        self._parent = parent
        self._execution = execution
        self._subagents = subagents
        self._provider = provider
        self._dispose_grace_ms = dispose_grace_ms
        self._info = WorkflowRunInfo(id, meta)
        loop = asyncio.get_running_loop()
        self._result_future: asyncio.Future = loop.create_future()
        self._settled = False
        self._terminal_claimed = False
        self._cancel_reason: str | None = None
        self._grace_timer: Any = None
        self.host_started = 0
        #: 已发布但未结束的 agent 按 seq 台账——宿主保证的配对记账（endAgent 门）。
        self._live_agents: dict[int, WorkflowAgentInfo] = {}
        self._quiescence_waiters: list[Any] = []
        #: 每个子一次释放事务的备忘录（key=子会话 id）。
        self._child_disposals: dict[str, Any] = {}
        #: 已注册（未静默）的子运行。
        self._children: dict[str, Any] = {}
        #: 尚未兑现/拒绝的 provider 启动任务。
        self.pending_starts: set = set()
        #: 每个子共享的取消信号（对标 host 的 AbortController）。
        self._controller = CancelSignal()
        self._input_signal = signal
        self._input_remove: Any = None
        self._disposed: Any = None
        # 注意：execution 与 drive 任务由 start() 在装配完成后接线（__init__ 只
        # 存引用）；输入信号的取消监听也经 _wire_signal() 延迟接线，避免在
        # execution 尚为 None 时 cancel() 解引用崩溃。
        self._drive_task: Any = None

    def _wire_signal(self, signal: Any) -> None:
        """接线调用方的取消信号（execution 就绪后调用）。"""
        if signal is None:
            return
        if signal.aborted:
            self.cancel("workflow start signal already aborted")
        else:
            self._input_remove = signal.add_listener(lambda: self.cancel("workflow signal aborted"))

    # ------------------------------------------------------------------ #
    # seam 表面
    # ------------------------------------------------------------------ #

    @property
    def result(self) -> Any:
        """以运行结果解析；永不拒绝。"""
        return self._result_future

    def cancel(self, reason: str | None = None) -> None:
        """取消运行：执行侧 hooks 开始抛错、共享给每个子的信号中止、grace 定时
        器武装——`disposeGraceMs` 后仍未终止的运行被强制按 ``cancelled`` 终止
        并取消 drive 任务。幂等；首个原因获胜。
        """
        if self._settled or self._terminal_claimed or self._cancel_reason is not None:
            return
        self._cancel_reason = reason if reason is not None else "workflow cancelled"
        self._execution.cancel(self._cancel_reason)
        self.abort_children(self._cancel_reason)
        loop = asyncio.get_event_loop()
        self._grace_timer = loop.call_later(self._dispose_grace_ms / 1000.0, self._force_settle)

    def _force_settle(self) -> None:
        """grace 到期：脚本停泊不归——强制终止（内联模式取消 drive 任务）。"""
        if self._terminal_claimed or self._settled:
            return
        self._terminal_claimed = True
        self.end_stranded_agents()
        self.settle_result(self.cancelled_result(self.host_started))
        if not self._drive_task.done():
            self._drive_task.cancel()

    async def dispose(self) -> None:
        """取消 + 有界终止 + 子清理。立即宿主驱动每个已注册子的释放（内联模式
        没有 wedged worker 可依赖），等待（至多 grace）结果与子静默，然后取消
        悬挂的 drive 任务。幂等；每条路径都安全。
        """
        if self._disposed is not None:
            return await self._disposed

        async def _tx() -> None:
            self._detach_input_signal()
            self.cancel("workflow disposed")
            # cancel() 在终态终止后故意变 no-op，但释放仍拥有每个已注册子：
            # 独立收割，已终止的 workflow 不能先等子静默再启动幸存子的释放。
            self.reap_children("workflow disposed")
            await self._bounded_quiescence()
            self.reap_children("workflow disposed")
            if not self._drive_task.done():
                self._drive_task.cancel()

        tx = asyncio.create_task(_tx())
        self._disposed = tx
        try:
            await tx
        except Exception:  # noqa: BLE001 - 释放绝不抛错
            pass

    # ------------------------------------------------------------------ #
    # 驱动
    # ------------------------------------------------------------------ #

    async def _drive(self) -> None:
        result = await self._execution.drive()
        self.on_result(result)

    def on_result(self, result: WorkflowResult) -> None:
        """首胜决定：没有进行中的外部取消时，这个结果获胜。先于终止清理收割
        散落的子；若取消已请求而结果不是 cancelled（取消正越过边界的竞态），
        报告 cancelled。
        """
        if self._terminal_claimed:
            return
        cancellation_was_requested = self._cancel_reason is not None
        # 在终止清理调用 provider 释放前声明；Result 获胜后，后续取消不能改写它。
        self._terminal_claimed = True
        self.reap_children("workflow settled")
        if not cancellation_was_requested:
            self.settle_result(result)
            return
        if result.stopReason != "cancelled":
            self.settle_result(self.cancelled_result(result.agentsStarted))
            return
        self.settle_result(result)

    # ------------------------------------------------------------------ #
    # 观察者（事件合成 + agent-end 恰好一次配对）
    # ------------------------------------------------------------------ #

    def observer_phase(self, title: str) -> None:
        if self._cancel_reason is None:
            self._engine.emit_workflow_event("workflow/phase", self._info, title)

    def observer_log(self, message: str) -> None:
        if self._cancel_reason is None:
            self._engine.emit_workflow_event("workflow/log", self._info, message)

    def observer_agent_start(self, agent: WorkflowAgentInfo) -> None:
        self._live_agents[agent.seq] = agent
        self._engine.emit_workflow_event("workflow/agent-start", self._info, agent)

    def observer_agent_end(self, agent: WorkflowAgentEndInfo) -> None:
        # 取消不抑制：被取消的子报告其配对的 agent-end（outcome='cancelled'）。
        # 台账保证每次 agent-start 恰好一次 agent-end。
        if self._live_agents.pop(agent.seq, None) is None:
            return
        self._engine.emit_workflow_event("workflow/agent-end", self._info, agent)

    def end_stranded_agents(self) -> None:
        """为每个已开始但未配对的 agent 合成缺失的 agent-end（outcome
        'cancelled'）——在 worker 不再能发言处调用（grace 强制终止）。
        """
        for info in list(self._live_agents.values()):
            self.observer_agent_end(
                WorkflowAgentEndInfo(seq=info.seq, label=info.label, childId=info.childId, phase=info.phase, outcome="cancelled")
            )

    # ------------------------------------------------------------------ #
    # 子生命周期
    # ------------------------------------------------------------------ #

    def child_admission_failure(self) -> dict | None:
        """就绪的 provider 结果为何不再被准入（对齐 host 的
        ``childAdmissionFailure``）。"""
        if self._cancel_reason is not None:
            return {"reason": self._cancel_reason, "rendered": f"workflow run cancelled: {self._cancel_reason}"}
        if self._terminal_claimed:
            return {"reason": "workflow settled", "rendered": "workflow run already settled"}
        return None

    async def start_child(self, request: ChildStartRequest) -> ChildHandle:
        """等待一次 provider 拥有的启动事务，只在准入时发布。"""
        try:
            sub_run = await self._subagents.start(
                self._provider,
                {
                    "prompt": [{"type": "text", "text": request.prompt}],
                    "parent": self._parent,
                    "signal": self._controller,
                    **({"outputSchema": request.schema} if request.schema is not None else {}),
                    **(
                        {
                            "agentOptions": {
                                **({"provider": request.provider} if request.provider is not None else {}),
                                **({"model": request.model} if request.model is not None else {}),
                            }
                        }
                        if request.provider is not None or request.model is not None
                        else {}
                    ),
                },
            )
        except Exception as error:  # noqa: BLE001 - 启动失败
            failure = self.child_admission_failure()
            if failure is not None:
                raise WorkflowError(failure["rendered"], "AGENT_START") from error
            raise WorkflowError(f"agent() could not start a child: {render_thrown(error)}", "AGENT_START", cause=error) from error
        failure = self.child_admission_failure()
        if failure is not None:
            try:
                await sub_run.dispose()
            except Exception:  # noqa: BLE001 - 被拒子的释放尽力而为
                pass
            raise WorkflowError(failure["rendered"], "AGENT_START")
        self._children[str(sub_run.id)] = sub_run
        return _InlineChildHandle(self, sub_run)

    def dispose_child(self, sub_run: Any) -> Any:
        """启动（或加入）一个已注册子的释放；注册表条目在它终止时离开。按子
        会话 id 备忘录化：worker 的释放 RPC、dispose() 宿主驱动与收割都能落在
        同一个子上——子的 ``dispose()`` 只跑一次，每个调用者都等那一次终止。
        拒绝被包含（记录日志），子仍离开注册表。
        """
        key = str(sub_run.id)
        existing = self._child_disposals.get(key)
        if existing is not None:
            return existing

        async def _tx() -> None:
            try:
                await sub_run.dispose()
            except Exception as error:  # noqa: BLE001 - 释放失败不破坏静默
                self._ctx.logger.warn(f"workflow: child dispose failed: {render_thrown(error)}")
            self.finish_child(key)

        task = asyncio.create_task(_tx())
        self._child_disposals[key] = task
        return task

    def finish_child(self, key: str) -> None:
        self._children.pop(key, None)
        self._child_disposals.pop(key, None)
        self.notify_quiescence()

    def reap_children(self, reason: str) -> None:
        """中止 + 释放每个已注册子（终止/最终清理）；释放被包含、不等待。"""
        self.abort_children(self._cancel_reason if self._cancel_reason is not None else reason)
        for sub_run in list(self._children.values()):
            self.dispose_child(sub_run)

    def abort_children(self, reason: str) -> None:
        if not self._controller.aborted:
            self._controller.abort(reason)

    def notify_quiescence(self) -> None:
        if self._children or self.pending_starts:
            return
        for waiter in self._quiescence_waiters:
            if not waiter.done():
                waiter.set_result(None)
        self._quiescence_waiters.clear()

    async def child_quiescence(self) -> None:
        if not self._children and not self.pending_starts:
            return
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._quiescence_waiters.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter in self._quiescence_waiters:
                self._quiescence_waiters.remove(waiter)
            raise

    async def _bounded_quiescence(self) -> None:
        """等待（至多 grace）结果与子静默。"""
        async def wait_all() -> None:
            await asyncio.shield(self._result_future)
            await self.child_quiescence()

        result_task = asyncio.ensure_future(wait_all())
        grace_task = asyncio.ensure_future(asyncio.sleep(self._dispose_grace_ms / 1000.0))
        done, pending = await asyncio.wait({result_task, grace_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()

    # ------------------------------------------------------------------ #
    # 终止
    # ------------------------------------------------------------------ #

    def cancelled_result(self, agents_started: int) -> WorkflowResult:
        reason = self._cancel_reason if self._cancel_reason is not None else "workflow cancelled"
        return WorkflowResult(
            value=None,
            stopReason="cancelled",
            error=f"workflow run cancelled: {reason}",
            agentsStarted=agents_started,
        )

    def _detach_input_signal(self) -> None:
        if self._input_remove is not None:
            try:
                self._input_remove()
            except Exception:  # noqa: BLE001
                pass
            self._input_remove = None

    def settle_result(self, result: WorkflowResult) -> None:
        """首胜终止：解除输入信号、撤销 grace 定时器、解析结果未来。"""
        if self._settled:
            return
        self._terminal_claimed = True
        self._settled = True
        self._detach_input_signal()
        if self._grace_timer is not None:
            self._grace_timer.cancel()
            self._grace_timer = None
        if not self._result_future.done():
            self._result_future.set_result(result)


class InlineWorkflowEngine(WorkflowEngine):
    """内联引擎服务。``start()`` 前置校验（meta + 宿主侧 body 解析）并返回
    ``result`` 永不拒绝的 :class:`InlineRun`；``workflow/*`` 事件按 seam 契约
    环绕运行发布。
    """

    def __init__(self, ctx: AppContext, config: Any = None) -> None:
        super().__init__(ctx)
        self._config = resolve_config(config)

    def start(self, request: WorkflowStartRequest) -> WorkflowRun:
        # 容忍 dict 调用（工具插件传 dict；dataclass 亦可）——归一化为 seam 类型。
        if isinstance(request, dict):
            request = WorkflowStartRequest(**request)
        meta = validate_meta(request.meta)
        assert_body_parses(request.script, meta.name)
        subagent_provider = resolve_subagent_provider(self.ctx, self._config["provider"], request.subagentProvider)
        max_total = resolve_max_total_agents(request.maxTotalAgents, self._config["maxTotalAgents"])
        run_id = WorkflowRunId(uuid.uuid4().hex)
        info = WorkflowRunInfo(run_id, meta)
        max_concurrent = self._config["maxConcurrentAgents"]
        if max_concurrent == 0:
            max_concurrent = min(16, max(1, (os.cpu_count() or 4) - 2))
        limits = WorkerLimits(
            max_concurrent_agents=max_concurrent,
            max_total_agents=max_total,
            max_items_per_call=self._config["maxItemsPerCall"],
            sync_timeout_ms=self._config["syncTimeoutMs"],
        )
        # 在服务调用仍被 start() 持有者追踪时捕获依赖：已返回的运行在引擎 HMR
        # 卸载移走 ctx.workflowEngine 后仍能启动子（对齐 dsh 的持有者生命周期）。
        subagents = self.ctx.subagents

        run = InlineRun(
            ctx=self.ctx,
            engine=self,
            id=run_id,
            meta=meta,
            parent=request.parent,
            execution=None,  # 下方装配（需要 observer/children 绑定）
            subagents=subagents,
            provider=subagent_provider,
            dispose_grace_ms=self._config["disposeGraceMs"],
            signal=request.signal,
        )
        # 执行：观察者绑定到 run 的事件合成，子端口绑定到 run 的子生命周期。
        execution = WorkflowExecution(
            meta,
            request.script,
            request.args if request.args is not None else None,
            limits,
            _RunObserver(run),
            _InlineChildPort(run),
        )
        run._execution = execution
        run._wire_signal(request.signal)
        run._drive_task = asyncio.create_task(run._drive(), name=f"workflow:{run_id}")

        self.emit_workflow_event("workflow/start", info)
        # workflow/end 在（永不拒绝的）结果终止时触发，只带结果数据——值留在
        # 运行持有者手里。
        asyncio.create_task(self._emit_end(run, info))
        return run

    async def _emit_end(self, run: InlineRun, info: WorkflowRunInfo) -> None:
        settled = await run.result
        self.emit_workflow_event(
            "workflow/end",
            info,
            WorkflowResultInfo(
                stopReason=settled.stopReason,
                agentsStarted=settled.agentsStarted,
                error=settled.error,
            ),
        )


class _RunObserver:
    """把执行观察者绑定到 :class:`InlineRun` 的事件合成（薄适配层）。"""

    def __init__(self, run: InlineRun) -> None:
        self._run = run

    def phase(self, title: str) -> None:
        self._run.observer_phase(title)

    def log(self, message: str) -> None:
        self._run.observer_log(message)

    def agent_start(self, info: WorkflowAgentInfo) -> None:
        self._run.observer_agent_start(info)

    def agent_end(self, info: WorkflowAgentEndInfo) -> None:
        self._run.observer_agent_end(info)


# --------------------------------------------------------------------------- #
# 插件入口
# --------------------------------------------------------------------------- #


def apply(ctx: AppContext, config: Any = None) -> None:
    """注册 ``ctx.workflowEngine``（内联引擎）。

    配置：见 :func:`resolve_config`（provider / maxConcurrentAgents /
    maxTotalAgents / maxItemsPerCall / syncTimeoutMs / disposeGraceMs）。
    """
    InlineWorkflowEngine(ctx, config)


apply.name = "workflow-engine"
apply.inject = ["subagents"]
apply.provides = ["workflowEngine"]

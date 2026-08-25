"""单次运行的脚本侧 hooks、子 RPC、并发/上限、取消与结果序列化。

对标 dsh 的 ``workflow-worker-thread/src/runtime.ts``（``WorkflowExecution``）。
差异（内联引擎的既定取舍）：

- 脚本语言从 JS 重新定向为 **Python**：dsh 用 ``vm.Script`` 编译
  ``(async () => { body })()``；dsh_py 用 ``compile`` + ``exec`` 把 body 包装
  成 ``async def __workflow__(): ...`` 后在注入 async hooks 的命名空间执行。
- 子启动是直连 ``ctx.subagents`` 的 async 调用（无线程 RPC）。
- 失去 ``vm`` 的同步超时（``syncTimeoutMs`` 保留为配置占位，内联无法中断
  同步死循环）与 worker 强杀（被信任前提接受——见 realm 模块信任模型）。
- 脚本环境不是安全边界：Python 运行时拥有完整内建（模型写的就是受信任的
  协调代码，dsh 的 worker 也只是包含而非安全边界）。

致命 workflow 错误——糟糕的 hook 参数、不支持的 schema/选项、上限、启动失败、
取消——经由组合子传播。只有子失败与普通阶段错误变成逐项 ``null``。
"""

from __future__ import annotations

import asyncio
import textwrap
from typing import Any, Callable, Optional, Protocol

from . import WorkflowError, is_fatal_workflow_error
from .port import ChildHandle, ChildPort, ChildResult, ChildStartRequest, WorkerLimits
from .realm import MaterializeError, materialize_from_realm, render_thrown
from .schema import JsonSchemaError, assert_object_json_schema
from .types import SessionId, WorkflowAgentEndInfo, WorkflowAgentInfo, WorkflowMeta, WorkflowResult

#: 脚本可传给 ``agent()`` 的选项；其余响亮拒绝。
SUPPORTED_AGENT_OPTIONS = frozenset({"label", "phase", "schema", "provider", "model"})
#: 延迟到 Claude Code 的选项，在拒绝消息里显式点名。
DEFERRED_AGENT_OPTIONS = frozenset({"effort", "isolation", "agentType"})


class ExecutionObserver(Protocol):
    """执行通过它报告进度（内联引擎直接回灌 ``workflow/*`` 事件）。"""

    def phase(self, title: str) -> None: ...

    def log(self, message: str) -> None: ...

    def agent_start(self, info: WorkflowAgentInfo) -> None: ...

    def agent_end(self, info: WorkflowAgentEndInfo) -> None: ...


def _output_text(blocks: list[Any]) -> str:
    """把子的最终输出块摊平为文本（非 schema 的 ``agent()`` 结果）。

    dsh_py 的 content 块可能是 dict（``{"type":"text","text":...}``）或
    TextBlock 对象（``.text`` 属性）——两者都接受。
    """
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        elif hasattr(block, "text"):
            parts.append(str(block.text))
    return "".join(parts)


def _default_label(prompt: str) -> str:
    """脚本没传 label 时，从 prompt 推导一个短展示标签。"""
    newline = prompt.find("\n")
    line = prompt if newline == -1 else prompt[:newline]
    return line if len(line) <= 48 else f"{line[:47]}…"


async def _maybe_await(value: Any) -> Any:
    """await 可等待值；普通值原样返回（对齐 JS 的 ``await <unknown>`` 自动展开）。"""
    if inspect_isawaitable(value):
        return await value
    return value


def inspect_isawaitable(value: Any) -> bool:
    return hasattr(value, "__await__")


class WorkflowExecution:
    """worker 内的一次活脚本执行。由运行管理器每次构造；``drive()`` 恰好调用
    一次且**永不拒绝**——每个失败都变成非 ``completed`` 的
    :class:`WorkflowResult`。宿主（运行管理器）拥有取消与任何被丢弃子工作的
    清理。
    """

    def __init__(
        self,
        meta: WorkflowMeta,
        body: str,
        args: Any,
        limits: WorkerLimits,
        observer: ExecutionObserver,
        children: ChildPort,
    ) -> None:
        self._meta = meta
        self._limits = limits
        self._observer = observer
        self._children = children
        self._started = 0
        self._active_slots = 0
        self._slot_waiters: list[asyncio.Future] = []
        self._cancel_reason: str | None = None
        self._cancel_error: WorkflowError | None = None
        self._current_phase: str | None = None

        # 先编译：body 语法错误必须在任何环境状态存在前从构造器抛出。宿主
        # start() 已做过同包装预解析，此处防御性再编译（对齐 dsh 的会话路径）。
        filename = f"workflow:{meta.name}"
        wrapped = "async def __workflow__():\n" + textwrap.indent(body, "    ")
        try:
            self._compiled = compile(wrapped, filename, "exec")
        except SyntaxError as error:
            raise WorkflowError(f"workflow script does not parse: {error}", "SCRIPT_PARSE", cause=error) from error

        self._namespace: dict[str, Any] = {
            "agent": lambda prompt, opts=None: self._contain(self._agent(prompt, opts)),
            "parallel": lambda thunks: self._contain(self._parallel(thunks)),
            "pipeline": lambda items, *stages: self._contain(self._pipeline(items, stages)),
            "phase": lambda title: self._phase(title),
            "log": lambda message: self._log(message),
            "args": args,
        }
        exec(self._compiled, self._namespace)

    # ------------------------------------------------------------------ #
    # 取消
    # ------------------------------------------------------------------ #

    def _is_cancelled(self) -> bool:
        return self._cancel_reason is not None

    def _throw_if_cancelled(self) -> None:
        if self._is_cancelled():
            raise self._cancelled_error()

    def _cancelled_error(self) -> WorkflowError:
        # cancel() 在任何调用者能观察到 is_cancelled() 之前武装 cancel_error；
        # 回退只护类型，不可达。
        if self._cancel_error is not None:
            return self._cancel_error
        return WorkflowError("workflow run cancelled", "CANCELLED")

    def cancel(self, reason: str) -> None:
        """取消运行：等待中的 ``agent()`` 槽位拒绝，每个未来 hook 调用都抛
        ``CANCELLED``——脚本在它的下一个 await 处死亡。永不终止的脚本（停泊在
        非 hook 拥有的 promise 上）是宿主的问题：其 grace 定时器强制终止运行。
        幂等；首个原因获胜。
        """
        if self._cancel_reason is not None:
            return
        self._cancel_reason = reason
        self._cancel_error = WorkflowError(f"workflow run cancelled: {self._cancel_reason}", "CANCELLED")
        for waiter in self._slot_waiters:
            if not waiter.done():
                waiter.set_exception(self._cancelled_error())
        self._slot_waiters.clear()

    # ------------------------------------------------------------------ #
    # 驱动
    # ------------------------------------------------------------------ #

    async def drive(self) -> WorkflowResult:
        """把脚本跑到终止。解析——绝不拒绝——为运行的 :class:`WorkflowResult`：
        ``completed`` 时是物化后的返回值，``error`` 时是失败信息，脚本死于取消
        时为 ``cancelled``。本方法只挑选结果；运行管理器负责发布它。
        """
        try:
            # 在 body 真正运行前就取消（已中止的 start 信号）：脚本绝不能执行，
            # 更不能报告 completed。
            if self._is_cancelled():
                raise self._cancelled_error()
            raw = await self._namespace["__workflow__"]()
            # 取消发生在脚本运行期间：一个没碰别的 hook（或一个都没碰）就终止
            # 的脚本也必须报告 cancelled——持有者请求了取消，completed 是谎言。
            if self._is_cancelled():
                raise self._cancelled_error()
            value = None if raw is None else self._materialize_result(raw)
            return WorkflowResult(value=value, stopReason="completed", agentsStarted=self._started)
        except asyncio.CancelledError:
            # 真实外部任务取消（grace 强制终止）：让任务以取消终止，结果未来
            # 由运行管理器兜底。
            raise
        except Exception as error:  # noqa: BLE001 - drive() 的永不拒绝契约
            if self._is_cancelled():
                return WorkflowResult(
                    value=None, stopReason="cancelled",
                    error=self._cancelled_error().message, agentsStarted=self._started,
                )
            return WorkflowResult(
                value=None, stopReason="error",
                error=render_thrown(error), agentsStarted=self._started,
            )

    def _contain(self, coro: Any) -> asyncio.Task:
        """附加一个无操作拒绝消费器而不改变调用方收到的值：若脚本丢弃 promise
        （不 await），取消不会变成未处理拒绝（那会杀死 worker/污染事件循环）；
        若脚本确实 await，它仍观察该拒绝。返回已调度的 Task（对齐 JS 异步函数
        立即开始执行）。
        """
        task = asyncio.ensure_future(coro)
        task.add_done_callback(_consume_dropped)
        return task

    def _materialize_result(self, raw: Any) -> Any:
        """物化脚本返回值；违规变成 RESULT_UNSERIALIZABLE。"""
        try:
            return materialize_from_realm(raw, "workflow result")
        except MaterializeError as error:
            raise WorkflowError(
                f"the workflow's return value is not plain JSON data — {error}. "
                "Return only JSON-serializable objects/arrays/scalars.",
                "RESULT_UNSERIALIZABLE",
                cause=error,
            ) from error

    # ------------------------------------------------------------------ #
    # 并发槽位（FIFO）
    # ------------------------------------------------------------------ #

    async def _acquire_slot(self) -> None:
        if self._active_slots < self._limits.max_concurrent_agents:
            self._active_slots += 1
            return
        waiter: asyncio.Future = asyncio.get_event_loop().create_future()
        self._slot_waiters.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            # 等待者被取消：从队列摘除，避免释放时误激活一个已死等待者。
            if waiter in self._slot_waiters:
                self._slot_waiters.remove(waiter)
            raise

    def _release_slot(self) -> None:
        self._active_slots -= 1
        if self._slot_waiters:
            nxt = self._slot_waiters.pop(0)
            if not nxt.done():
                self._active_slots += 1
                nxt.set_result(None)

    # ------------------------------------------------------------------ #
    # hooks
    # ------------------------------------------------------------------ #

    async def _agent(self, raw_prompt: Any, raw_opts: Any = None) -> Any:
        self._throw_if_cancelled()
        if not isinstance(raw_prompt, str) or len(raw_prompt) == 0:
            raise WorkflowError("agent() requires a non-empty prompt string", "INVALID_ARGUMENT")
        opts = self._read_agent_options(raw_opts)
        if self._started >= self._limits.max_total_agents:
            raise WorkflowError(
                f"this run reached its total agent cap ({self._limits.max_total_agents}) — "
                "a runaway-loop backstop; raise the applicable maxTotalAgents limit if the scale is intentional",
                "AGENT_CAP",
            )
        self._started += 1
        seq = self._started
        label = opts.get("label") or _default_label(raw_prompt)
        phase = opts.get("phase") or self._current_phase

        await self._acquire_slot()
        try:
            # 获取后再复查：await 至少让出一个微任务节拍，取消落在任一窗口都不
            # 该到达宿主（到达也会被拒，但拒绝读起来像启动失败而非取消）。
            self._throw_if_cancelled()
            try:
                run = await self._children.start_agent(
                    ChildStartRequest(
                        prompt=raw_prompt,
                        schema=opts.get("schema"),
                        provider=opts.get("provider"),
                        model=opts.get("model"),
                    )
                )
            except Exception as error:  # noqa: BLE001 - 宿主拒绝启动
                # 宿主在运行取消后拒绝启动——与自身取消状态竞态的拒绝必须读作
                # 取消本身，而非违约。
                if self._is_cancelled():
                    raise self._cancelled_error() from error
                raise WorkflowError(
                    f"agent() could not start a child: {render_thrown(error)}", "AGENT_START", cause=error
                ) from error
            # 启动往返让出事件循环：取消可能落在「宿主已启动子」与「本续体运行」
            # 之间——把新子绞下来，而不是把它留在死脚本背后继续活着。
            if self._is_cancelled():
                await run.dispose()
                raise self._cancelled_error()
            info = WorkflowAgentInfo(seq=seq, label=label, childId=SessionId(run.id), phase=phase)
            self._observer.agent_start(info)
            try:
                try:
                    result = await run.result
                except Exception as error:  # noqa: BLE001 - 基础设施故障
                    # 被拒绝的子结果是宿主转发的**基础设施**故障——与「子失败并
                    # 解析」不同。配对生命周期后按 FATAL 传播：普通抛错会在组合子
                    # 里溶解成逐项 null，损坏的 provider 绝不能读作失败的子。
                    if self._is_cancelled():
                        self._observer.agent_end(WorkflowAgentEndInfo(seq=seq, label=label, childId=info.childId, phase=phase, outcome="cancelled"))
                        raise self._cancelled_error() from error
                    self._observer.agent_end(WorkflowAgentEndInfo(seq=seq, label=label, childId=info.childId, phase=phase, outcome="failed"))
                    raise WorkflowError(
                        f"child agent run failed: {render_thrown(error)}", "AGENT_RESULT", cause=error
                    ) from error
                if result.stopReason == "completed":
                    if opts.get("schema") is not None:
                        # provider 兑现了 outputSchema（启动时能力门控），因此
                        # completed 却无结构化值是子失败。
                        if result.structured is None:
                            self._observer.agent_end(WorkflowAgentEndInfo(seq=seq, label=label, childId=info.childId, phase=phase, outcome="failed"))
                            return None
                        self._observer.agent_end(WorkflowAgentEndInfo(seq=seq, label=label, childId=info.childId, phase=phase, outcome="completed"))
                        return result.structured
                    self._observer.agent_end(WorkflowAgentEndInfo(seq=seq, label=label, childId=info.childId, phase=phase, outcome="completed"))
                    return _output_text(result.output)
                # 被取消的运行杀死脚本；因自身原因失败的子解析 null
                # （脚本按 CC 契约 .filter(Boolean)）。
                if self._is_cancelled():
                    self._observer.agent_end(WorkflowAgentEndInfo(seq=seq, label=label, childId=info.childId, phase=phase, outcome="cancelled"))
                    raise self._cancelled_error()
                self._observer.agent_end(WorkflowAgentEndInfo(seq=seq, label=label, childId=info.childId, phase=phase, outcome="failed"))
                return None
            finally:
                await run.dispose()
        finally:
            self._release_slot()

    def _read_agent_options(self, raw_opts: Any) -> dict:
        """物化并校验 ``agent()`` 的选项袋。"""
        if raw_opts is None:
            return {}
        try:
            opts = materialize_from_realm(raw_opts, "agent() options")
        except MaterializeError as error:
            raise WorkflowError(
                f"agent() options must be plain JSON data — {error}", "INVALID_ARGUMENT", cause=error
            ) from error
        if not isinstance(opts, dict):
            raise WorkflowError("agent() options must be an object", "INVALID_ARGUMENT")
        for key in opts:
            if key in SUPPORTED_AGENT_OPTIONS:
                continue
            if key in DEFERRED_AGENT_OPTIONS:
                raise WorkflowError(
                    f'agent() option "{key}" is deferred and not supported by this engine '
                    "(supported: label, phase, schema, provider, model)",
                    "UNSUPPORTED_OPTION",
                )
            raise WorkflowError(
                f'agent() option "{key}" is not recognized (supported: label, phase, schema, provider, model)',
                "UNSUPPORTED_OPTION",
            )
        for key in ("label", "phase", "provider", "model"):
            value = opts.get(key)
            if value is not None and not isinstance(value, str):
                raise WorkflowError(f'agent() option "{key}" must be a string', "INVALID_ARGUMENT")
        schema = None
        if opts.get("schema") is not None:
            try:
                assert_object_json_schema(opts["schema"])
                schema = opts["schema"]
            except JsonSchemaError as error:
                raise WorkflowError(
                    f"agent() schema is outside the supported subset — {error}",
                    "UNSUPPORTED_SCHEMA",
                    cause=error,
                ) from error
        result: dict = {}
        for key in ("label", "phase", "provider", "model"):
            value = opts.get(key)
            if value is not None:
                result[key] = value
        if schema is not None:
            result["schema"] = schema
        return result

    async def _parallel(self, raw_thunks: Any) -> list:
        """``parallel(thunks)`` hook：每个 thunk 捕获 → ``null``；致命错误传播。"""
        self._throw_if_cancelled()
        if not isinstance(raw_thunks, list):
            raise WorkflowError("parallel() requires an array of zero-argument functions", "INVALID_ARGUMENT")
        self._assert_item_cap(len(raw_thunks), "parallel()")
        thunks: list[Callable[[], Any]] = []
        for index, thunk in enumerate(raw_thunks):
            if not callable(thunk):
                raise WorkflowError(f"parallel() item {index} is not a function", "INVALID_ARGUMENT")
            thunks.append(thunk)

        async def run_one(thunk: Callable[[], Any]) -> Any:
            try:
                return await _maybe_await(thunk())
            except Exception as error:  # noqa: BLE001 - 逐项 null 纪律
                # hook 失败是在脚本环境外构造的 WorkflowError；致命性由本环境的
                # isinstance 识别——脚本构造的对象永远过不了（也不该被误溶解）。
                if is_fatal_workflow_error(error):
                    raise
                return None

        return await asyncio.gather(*(run_one(t) for t in thunks))

    async def _pipeline(self, raw_items: Any, raw_stages: list) -> list:
        """``pipeline(items, ...stages)`` hook：逐项阶段链，**无跨阶段屏障**。"""
        self._throw_if_cancelled()
        if not isinstance(raw_items, list):
            raise WorkflowError("pipeline() requires an items array", "INVALID_ARGUMENT")
        self._assert_item_cap(len(raw_items), "pipeline()")
        if len(raw_stages) == 0:
            raise WorkflowError("pipeline() requires at least one stage function", "INVALID_ARGUMENT")
        stages: list[Callable[[Any, Any, int], Any]] = []
        for index, stage in enumerate(raw_stages):
            if not callable(stage):
                raise WorkflowError(f"pipeline() stage {index} is not a function", "INVALID_ARGUMENT")
            stages.append(stage)

        async def run_one(item: Any, index: int) -> Any:
            value = item
            try:
                for stage in stages:
                    value = await _maybe_await(stage(value, item, index))
                return value
            except Exception as error:  # noqa: BLE001 - 逐项 null 纪律
                # 普通阶段抛错把该条目丢到 null 并跳过其剩余阶段；致命
                # WorkflowError 杀死整个脚本。
                if is_fatal_workflow_error(error):
                    raise
                return None

        return await asyncio.gather(*(run_one(item, index) for index, item in enumerate(raw_items)))

    def _assert_item_cap(self, length: int, hook: str) -> None:
        if length > self._limits.max_items_per_call:
            raise WorkflowError(
                f"{hook} received {length} items — over the per-call cap "
                f"({self._limits.max_items_per_call}); split the work or raise maxItemsPerCall in the engine config",
                "ITEM_CAP",
            )

    def _phase(self, title: Any) -> None:
        """``phase(title)`` hook：为后续 ``agent()`` 设置当前标签并通知观察者。"""
        self._throw_if_cancelled()
        if not isinstance(title, str) or len(title) == 0:
            raise WorkflowError("phase() requires a non-empty title string", "INVALID_ARGUMENT")
        self._current_phase = title
        self._observer.phase(title)

    def _log(self, message: Any) -> None:
        """``log(message)`` hook：向观察者叙述。"""
        self._throw_if_cancelled()
        if not isinstance(message, str):
            raise WorkflowError("log() requires a message string", "INVALID_ARGUMENT")
        self._observer.log(message)


def _consume_dropped(task: asyncio.Task) -> None:
    """消费被丢弃 hook Task 的拒绝（见 ``_contain`` 的契约）。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        # 仅消费——丢弃的 hook promise 不得表面为未处理拒绝。
        pass

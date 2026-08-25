"""workflow 能力 seam（对标 dsh 的 ``@deepseek-ai/dsh-workflow``）。

服务提供者执行编排脚本；只读的生命周期事件从不暴露运行控制权。契约：

- :class:`WorkflowEngine` 是抽象 ``Service``（``ctx.workflowEngine``），
  ``start(request)`` 返回一个 ``result`` 永不拒绝的活运行；
- :class:`WorkflowError` 携带机器可路由的 ``code``（WorkflowErrorCode）与
  ``fatal`` 标志——组合子（``parallel()``/``pipeline()``）对 fatal 错误重抛、
  对普通子失败/阶段错误保留逐项 ``null``；
- 六个 ``workflow/*`` 事件（emit 模式）围绕运行生命周期发布，监听器失败被
  逐例隔离（contained），绝不向发布者传播。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any, Callable, Literal, Optional, Union

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.llm import HarnessError

from .types import (
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

__all__ = [
    "WorkflowEngine",
    "WorkflowError",
    "is_fatal_workflow_error",
    "WorkflowErrorCode",
    "WorkflowEventName",
    "WORKFLOW_EVENT_NAMES",
    # 类型再导出（seam 词汇）
    "WorkflowRunId",
    "WorkflowMeta",
    "WorkflowPhase",
    "WorkflowResult",
    "WorkflowResultInfo",
    "WorkflowRunInfo",
    "WorkflowAgentInfo",
    "WorkflowAgentEndInfo",
    "WorkflowAgentOutcome",
    "WorkflowStopReason",
    "WorkflowRun",
    "WorkflowStartRequest",
]

from .types import WorkflowAgentOutcome, WorkflowPhase, WorkflowStopReason  # noqa: E402


#: ``WorkflowEngine.emit_workflow_event`` 分派的全部 ``workflow/*`` 事件名。
WorkflowEventName = Literal[
    "workflow/start",
    "workflow/phase",
    "workflow/log",
    "workflow/agent-start",
    "workflow/agent-end",
    "workflow/end",
]

WORKFLOW_EVENT_NAMES: tuple[str, ...] = (
    "workflow/start",
    "workflow/phase",
    "workflow/log",
    "workflow/agent-start",
    "workflow/agent-end",
    "workflow/end",
)

#: 机器可路由的致命 workflow 失败：解析/meta/参数/schema 错误、资源上限、
#: 子代理基础设施失败、不可序列化的边界值、取消。普通子失败把其条目解析为
#: ``null``，不属于这些致命码。
WorkflowErrorCode = Literal[
    "SCRIPT_PARSE",
    "META_INVALID",
    "INVALID_ARGUMENT",
    "UNSUPPORTED_OPTION",
    "UNSUPPORTED_SCHEMA",
    "AGENT_CAP",
    "ITEM_CAP",
    "AGENT_START",
    "AGENT_RESULT",
    "RESULT_UNSERIALIZABLE",
    "CANCELLED",
]


class WorkflowError(HarnessError):
    """workflow seam 失败的带类型错误。扩展 :class:`HarnessError`，因此
    ``code`` 是机器可路由的分类。``fatal`` 驱动组合子纪律：``parallel()``/
    ``pipeline()`` 重抛致命错误（打错的选项或触顶的上限必须响亮杀死脚本），
    并把逐项 ``null`` 保留给子运行失败与普通阶段脚本错误。每个
    ``WorkflowErrorCode`` 都是致命的；该标志只是让区分在每处 catch 点显式。

    :ivar fatal: 组合子必须传播此错误而不是把条目置 null。
    """

    def __init__(
        self,
        message: str,
        code: str = "INVALID_ARGUMENT",
        *,
        cause: Any = None,
        fatal: bool = True,
    ) -> None:
        super().__init__(message, code, cause=cause)
        self.fatal = fatal


def is_fatal_workflow_error(error: Any) -> bool:
    """组合子是否必须重抛 ``error`` 而不是把条目映射为 ``null``。

    :param error: 任意抛出的值；致命性是宿主 ``instanceof``（脚本 realm 不可伪造）。
    :returns: 当且仅当 ``error`` 是设置了 ``fatal`` 标志的 :class:`WorkflowError`。
    """
    return isinstance(error, WorkflowError) and error.fatal


def _render_listener_error(error: Any) -> str:
    """渲染任何抛出的值而不违反监听器隔离（``String(error)``，或固定标签）。"""
    try:
        return str(error)
    except Exception:  # noqa: BLE001 - str 强制转换本身也可能抛
        return "[unrenderable thrown value]"


class WorkflowEngine(Service, ABC):
    """workflow Service 定义契约。非法请求在发布前抛错；活运行归持有者所有，
    ``result`` 永不拒绝，取消与释放有界，释放等待子清理在该界内。生命周期
    监听器失败被隔离，``workflow/end`` 在结果终止时恰好触发一次。
    """

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "workflowEngine")

    @abstractmethod
    def start(self, request: WorkflowStartRequest) -> WorkflowRun:
        """解析并执行一条 workflow 脚本。

        :param request: 脚本、其 ``args``、父 Agent、可选取消信号。
        :returns: 活运行；其 ``result`` 在脚本终止时解析。
        """

    def emit_workflow_event(self, name: str, *args: Any) -> None:
        """发布一个生命周期事件，同时隔离并记录每个监听器失败。

        逐例遍历监听器（对标 dsh 的 ``dispatch('emit', [...])``）：

        - :class:`InvariantError`（workflow invariant 的 fail 报告器）**响亮
          传播**并中止分发——对标 dsh 中 invariant 走 ``internal/dispatch``
          拦截路径、不参与观察者包含的语义；
        - 其余同步抛错被隔离并记录，协程型监听器被调度执行且拒绝被记录。
        """
        from dsh_py.services.invariants import InvariantError

        for listener in self.ctx.events._listeners(name):  # noqa: SLF001 - 需要逐例
            try:
                result = listener(*args)
            except InvariantError:
                raise
            except Exception as error:  # noqa: BLE001 - 监听器隔离
                self.ctx.logger.warn(
                    f"workflow: {name} listener threw: {_render_listener_error(error)}"
                )
                continue
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                task = asyncio.ensure_future(result)

                def _consume(done: asyncio.Future, _name: str = name) -> None:
                    if done.cancelled():
                        return
                    exc = done.exception()
                    if exc is not None:
                        self.ctx.logger.warn(
                            f"workflow: {_name} listener rejected: {_render_listener_error(exc)}"
                        )

                task.add_done_callback(_consume)


# 默认导出（对齐 dsh 的 ``export default WorkflowEngine``）
default = WorkflowEngine

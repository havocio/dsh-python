"""引擎内部端口与限制词汇（对标 dsh 的 worker-thread ``types.ts``）。

内联引擎里没有线程边界：``ChildPort`` 是运行管理器直连 ``ctx.subagents``
的桥（进程内 async 直调，非 RPC）。协议消息联合在 dsh 里因结构化克隆存在；
内联模式无需传输层，但保留这些类型以便与 dsh 语义逐一对应。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Optional, Protocol


@dataclass(frozen=True)
class WorkerLimits:
    """一次运行由引擎侧强制执行的限制。宿主只保留它才能操作的旋钮
    （provider、disposeGraceMs）。

    :ivar max_concurrent_agents: 并发 ``agent()`` 上限（已自动解析；>=1）。
    :ivar max_total_agents: 每次运行 ``agent()`` 调用总数（失控循环兜底）。
    :ivar max_items_per_call: 一次 ``parallel()``/``pipeline()`` 接受的条目数。
    :ivar sync_timeout_ms: 脚本初始同步片的超时（dsh 在 vm 内执行；**dsh_py
        内联引擎无法中断同步死循环，保留该字段仅为配置对齐，实际不生效**）。
    """

    max_concurrent_agents: int
    max_total_agents: int
    max_items_per_call: int
    sync_timeout_ms: int


@dataclass(frozen=True)
class ChildStartRequest:
    """一次 ``agent()`` 调用请求宿主启动的子代理（选项已在脚本侧校验）。

    :ivar prompt: 子提示文本。
    :ivar schema: 结构化输出 schema（调用传了才带，已做子集校验）。
    :ivar provider: 逐子 provider 覆盖（调用传了才带）。
    :ivar model: 逐子 model 覆盖（调用传了才带）。
    """

    prompt: str
    schema: Any = None
    provider: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class ChildResult:
    """子代理结果的 JSON 投影（内联模式直传）。``stopReason`` 的 seam 联合可
    扩展，运行时只分支 ``completed``。

    :ivar output: 子的最终 assistant 输出块（list[dict]，text/reasoning 等）。
    :ivar structured: 结构化值（请求带 schema 且 provider 兑现时才存在）。
    :ivar stopReason: 子运行为何结束（运行时只分支 ``'completed'``）。
    """

    output: list[dict]
    structured: Any = None
    stopReason: str = "completed"


class ChildHandle(Protocol):
    """一个已启动子代理的脚本侧句柄——subagent seam 运行句柄的 RPC 镜像，
    缩减为运行时消费的形状。

    :ivar id: 子代理 id（由 subagent seam 铸造）。
    :ivar result: 以子的终态 :class:`ChildResult` 解析；仅当宿主报告基础设施
        故障时拒绝——子因自身原因失败时以非 ``completed`` 的 stop reason 解析。
    """

    id: str
    result: Awaitable[ChildResult]

    async def dispose(self) -> None:
        """请宿主释放该子代理；在宿主确认后解析。"""


class ChildPort(Protocol):
    """运行时启动子代理的端口——让执行核心对线程边界保持无知的 seam。内联
    模式直接桥到 ``ctx.subagents``。

    :param request: 提示与已校验选项。
    :returns: 已发布的子句柄；同步启动或 provider 异步启动失败时拒绝。
    """

    async def start_agent(self, request: ChildStartRequest) -> ChildHandle:
        ...

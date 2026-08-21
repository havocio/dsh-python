"""后台任务注册表 seam（jobs/jobs，第 3 层）。

``ctx.jobs``：后台任务（长时间运行的工具）的注册表与生命周期约定——任务 id、
会话作用域访问、生命周期状态、完成监听、所有者清理；生产者保留其执行资源。

- id 由注册表生成 ``<kind>-N``（可预测，依赖所有者授权而非保密）；
- 拥有任务（owner）的访问按所有者 session id 栅栏；无主任务对任何调用方开放；
- settle 首胜：一个终态记录、释放等待者、一轮 contained 监听器通知，即使
  生产者迟到也要守住；完成通知最后发出（报告者可能同步开一轮模型 turn）；
- :meth:`JobRegistry.start` 在没有附着任务控制器服务该 owner 时拒绝工作；
  所有者/服务销毁时取消并等待合规生产者。

进程本地实现见 :mod:`dsh_py.services.jobs_local`（dsh 的 ``dsh-jobs-local``）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Optional

from dsh_py.core.service import Service

# 任务生命周期：running → 可选 stopping → 恰一个终态
JOB_STATUSES = ("running", "stopping", "completed", "killed", "failed")


class JobId(str):
    """标识一个后台任务（品牌字符串；注册表生成 ``<kind>-N``）。"""

    def __new__(cls, value: str) -> "JobId":
        return str.__new__(cls, value)


# 生产者通过 JobHooks.done 提供的终态结果
class JobOutcome(dict):
    pass


# 任务类型（判别值作为 id 前缀命名空间）
JOB_KINDS = ("bash", "subagent")


def job_outcome(status: str, detail: Optional[str] = None, output: Optional[str] = None) -> dict:
    """构造一个终态结果（status: completed/killed/failed）。"""
    result: dict = {"status": status}
    if detail is not None:
        result["detail"] = detail
    if output is not None:
        result["output"] = output
    return result


class JobHooks:
    """运行期控制与观察生产者工作的钩子（由 ``run`` 同步返回）。"""

    def cancel(self, reason: Optional[str] = None) -> None:
        """请求终止：必须同步、幂等，且最终 settle ``done``。"""

    done: Awaitable[dict] = None  # 生产者释放资源后 resolve（不得 reject）

    def readOutput(self) -> str:  # 消费自上次调用以来产出的输出
        return ""


def _is_terminal(status: str) -> bool:
    return status in ("completed", "killed", "failed")


class JobRegistry(Service, ABC):
    """抽象后台任务注册表（``ctx.jobs``；子类化实现并以插件加载）。"""

    def __init__(self, ctx: Any, name: str = "jobs") -> None:
        # dsh 在构造时 fail loud 防直接实例化抽象 seam
        if type(self) is JobRegistry:
            raise TypeError(
                "dsh_py.services.jobs 是抽象任务注册表 seam；请加载实现（如 dsh_py.services.jobs_local）",
            )
        super().__init__(ctx, name)

    @abstractmethod
    def start(self, spec: dict) -> JobId: ...

    @abstractmethod
    def list(self, caller: Any = None) -> list: ...

    @abstractmethod
    def get(self, id: JobId, caller: Any = None) -> dict: ...

    @abstractmethod
    def read(self, id: JobId, caller: Any = None) -> dict: ...

    @abstractmethod
    def kill(self, id: JobId, caller: Any = None, reason: Optional[str] = None) -> str: ...

    @abstractmethod
    async def wait(self, id: JobId, timeout_ms: float, caller: Any = None, signal: Any = None) -> dict: ...

    @abstractmethod
    def onJobDone(self, listener: Callable[[dict, Any], Any]) -> Callable[[], None]: ...

    @abstractmethod
    def onJobsChanged(self, listener: Callable[[Any], None]) -> Callable[[], None]: ...

    @abstractmethod
    def attachController(self, name: str) -> Callable[[], None]: ...

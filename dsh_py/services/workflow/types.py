"""workflow seam 词汇表（对标 dsh 的 ``@deepseek-ai/dsh-workflow/types``）。

引擎消费/产出的请求、运行、结果类型，以及 ``workflow/*`` 事件载荷中的字段。
纯类型 + id 品牌工厂；本模块不依赖宿主（Cordis/Agent），保证浏览器端可用
（dsh 的约定：耐久词汇留在 ``types``，宿主句柄留在 ``runtime-types``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Literal, NewType, Protocol, Union

# dsh 从 ``@deepseek-ai/dsh-session/types`` 导入 SessionId；dsh_py 的 session
# 模块尚无该品牌，这里按 seam 词汇本地定义（子代理会话 id 的品牌类型）。
SessionId = NewType("SessionId", str)

# --------------------------------------------------------------------------- #
# 品牌类型
# --------------------------------------------------------------------------- #

#: 标识一次 workflow 运行（引擎铸 UUID；测试可传 fixture）。
WorkflowRunId = NewType("WorkflowRunId", str)


# --------------------------------------------------------------------------- #
# 耐久词汇（纯数据）
# --------------------------------------------------------------------------- #

#: 运行为何终止（引擎拥有的闭合联合）：completed / cancelled / error。
WorkflowStopReason = Literal["completed", "cancelled", "error"]


@dataclass(frozen=True)
class WorkflowPhase:
    """脚本 ``meta.phases`` 中声明的一个阶段（进度词汇——分组 agent，不施加执行结构）。

    :ivar title: 阶段标题；``phase()`` 调用按精确字符串匹配它。
    :ivar detail: 可选的阶段一句话说明。
    :ivar provider: 该阶段预期使用的 provider 覆盖（仅信息性）。
    :ivar model: 该阶段预期使用的 model 覆盖（仅信息性）。
    """

    title: str
    detail: str | None = None
    provider: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class WorkflowMeta:
    """脚本身份块（与脚本体一同作为纯 JSON 数据提供，引擎运行前校验）。

    :ivar name: 短 kebab-case workflow 名（展示 + 持久化键）。
    :ivar description: 一句话描述。
    :ivar whenToUse: 可选的适用场景说明。
    :ivar phases: 可选的阶段声明，由 ``phase()`` 调用匹配。
    """

    name: str
    description: str
    whenToUse: str | None = None
    phases: tuple[WorkflowPhase, ...] | None = None

    def to_dict(self) -> dict:
        """序列化为纯 JSON 数据（事件/持久化用）。"""
        out: dict = {"name": self.name, "description": self.description}
        if self.whenToUse is not None:
            out["whenToUse"] = self.whenToUse
        if self.phases is not None:
            out["phases"] = [
                {
                    "title": p.title,
                    **({"detail": p.detail} if p.detail is not None else {}),
                    **({"provider": p.provider} if p.provider is not None else {}),
                    **({"model": p.model} if p.model is not None else {}),
                }
                for p in self.phases
            ]
        return out


@dataclass(frozen=True)
class WorkflowResult:
    """一次活运行的解决结果。``value`` 是脚本被物化后的返回值（宿主 JSON 数据；
    脚本返回 ``undefined`` 时为 ``None``）——仅对 ``completed`` 有意义。
    非 ``completed`` 原因在 ``error`` 携带失败信息。

    :ivar value: 脚本返回值（宿主 JSON 数据；无返回值时为 ``None``）。
    :ivar stopReason: 运行如何终止。
    :ivar error: 失败信息（仅当 stopReason 非 ``completed`` 时存在）。
    :ivar agentsStarted: 运行整个生命周期接受的 ``agent()`` 调用数。
    """

    value: Any = None
    stopReason: str = "completed"
    error: str | None = None
    agentsStarted: int = 0


@dataclass(frozen=True)
class WorkflowRunInfo:
    """一次运行的识别细节：作为借用的不可变数据被每个 ``workflow/*`` 事件携带，
    绝不是活运行本身。

    :ivar id: 运行 id。
    :ivar meta: 已校验的 meta 块。
    """

    id: WorkflowRunId
    meta: WorkflowMeta


@dataclass(frozen=True)
class WorkflowAgentInfo:
    """一次 ``agent()`` 调用在运行内的身份（``workflow/agent-start`` 载荷）。

    :ivar seq: 本次调用在运行内的 1 基序号。
    :ivar label: 展示标签（``label`` 选项，或 prompt 片段）。
    :ivar phase: 该 agent 所属阶段（``phase`` 选项，或当前 ``phase()`` 标题）。
    :ivar childId: 子代理在 subagent seam 上的 id。
    """

    seq: int
    label: str
    childId: SessionId
    phase: str | None = None


#: 一次 ``agent()`` 调用如何结束：干净结果 / 子失败（脚本看到 null）/ 运行取消。
WorkflowAgentOutcome = Literal["completed", "failed", "cancelled"]


@dataclass(frozen=True)
class WorkflowAgentEndInfo(WorkflowAgentInfo):
    """一次 ``agent()`` 调用的结束（``workflow/agent-end`` 载荷）。

    :ivar outcome: 调用如何结束。
    """

    outcome: str = "completed"


@dataclass(frozen=True)
class WorkflowResultInfo:
    """已终止运行的结果事件数据（``workflow/end`` 载荷）：``WorkflowResult``
    减去 ``value``——观察者绝不能拿到调用方结果值的可变别名；需要值的消费者
    持有运行并 ``await result``。

    :ivar stopReason: 运行如何终止。
    :ivar error: 失败信息（仅当 stopReason 非 ``completed`` 时存在）。
    :ivar agentsStarted: 运行接受的 ``agent()`` 调用数。
    """

    stopReason: str
    agentsStarted: int
    error: str | None = None


# --------------------------------------------------------------------------- #
# 宿主句柄（runtime types）
# --------------------------------------------------------------------------- #


@dataclass
class WorkflowStartRequest:
    """调用方请求开始一次 workflow 运行。``meta``/``args`` 按 seam 契约是纯
    JSON 数据。``parent`` 必填：脚本 spawn 的每个 ``agent()`` 都归属到该活
    Agent。

    :ivar script: 纯 Python 脚本体（允许顶层 ``await``，以 ``return <json>`` 结尾）。
    :ivar meta: workflow 身份块（由引擎做形状校验的纯 JSON 数据）。
    :ivar args: 以 ``args`` 全局逐字暴露给脚本的输入。
    :ivar subagentProvider: 本次运行的子代理 provider 覆盖（引擎级）。
    :ivar maxTotalAgents: 本次运行的子代理总数上限。
    :ivar parent: 运行以其名义执行的 Agent（每个子的父）。
    :ivar signal: 中止时取消运行（CancelSignal）。
    """

    script: str
    meta: Any
    args: Any = None
    subagentProvider: str | None = None
    maxTotalAgents: int | None = None
    parent: Any = None
    signal: Any = None


class WorkflowRun(Protocol):
    """持有者拥有的活 workflow。``result`` 永不拒绝；消费者可取消并必须调用
    幂等的 ``dispose()`` 等待脚本与子代理静默。

    :ivar id: 运行 id。
    :ivar meta: 在脚本体运行前即可用的已校验 meta 块。
    :ivar result: 运行结果（永不拒绝的可等待对象）。
    """

    id: WorkflowRunId
    meta: WorkflowMeta
    result: Awaitable[WorkflowResult]

    def cancel(self, reason: str | None = None) -> None:
        """取消运行及其子代理。"""

    async def dispose(self) -> None:
        """必要时取消并等待有界静默与清理。"""

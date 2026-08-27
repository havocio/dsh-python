"""批准能力 seam 的服务定义：请求、取消、审计、按会话策略（对齐 dsh-user-approval）。

缺失 answerer 时 fail-closed；批准只作用于被请求的那一个动作。策略折叠是纯
函数（replay 即状态，无需 catch-up）：``approval/policy`` 事件是会话覆盖的
唯一耐久表示，最后一次切换即生效策略；``approval/asked`` ↔
``approval/decided`` 是每次询问的审计对（log-only，非表面事件）。

差异（相对 dsh）：dsh 的 ``approval/request`` 瀑布流用 ``scopeTarget`` 做
作用域过滤分发；dsh_py 无 scope 事件载波，改为普通 ``ctx.waterfall``，监听器
按 ``req.agent`` 属性自行过滤（语义等价，见 docstring）。dsh 的
``agent.inject(createUserMessage(...))`` 在 dsh_py 为
``agent.session.append("user/message", ...)``。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Literal, NewType, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.message import MessageSource

# --------------------------------------------------------------------------- #
# 词汇
# --------------------------------------------------------------------------- #

#: 配对一次 ``approval/asked`` 审计事件与其 ``approval/decided``。
ApprovalRequestId = NewType("ApprovalRequestId", str)


def ApprovalRequestIdFactory() -> ApprovalRequestId:
    """铸造一个新的请求 id（服务每次 request 一枚）。"""
    return ApprovalRequestId(uuid.uuid4().hex)


#: 已闭合的批准结果：一次性放行 / 显式拒绝 / 撤回 / answerer 不可用。
ApprovalOutcome = Literal["allowed-once", "rejected", "cancelled", "unavailable"]

#: 每个 :data:`ApprovalOutcome`，用于运行时归一化 answerer 返回值。
OUTCOMES: tuple[ApprovalOutcome, ...] = ("allowed-once", "rejected", "cancelled", "unavailable")

#: 会话批准策略：``'ask'`` 交给组合 answerer（无 answerer 时 fail-closed）；
#: ``'never'`` 确定性拒绝（CI / 无人值守）。
ApprovalPolicy = Literal["ask", "never"]

#: 每个 :data:`ApprovalPolicy`，用于选项广告与非法策略串的运行时校验。
APPROVAL_POLICIES: tuple[ApprovalPolicy, ...] = ("ask", "never")

#: ``'never'`` 策略的模型侧陈述。
NEVER_SENTENCE = (
    "Approval prompts are disabled in this session: actions that require approval are "
    "rejected automatically — do not request sandbox escalation (do not set `sandbox_permissions`)."
)

#: ``'ask'`` 策略的模型侧陈述（可能仍 fail-closed）。
ASK_SENTENCE = (
    "Approval policy: ask. Operations that require approval may ask through the configured "
    "answerers; without an available answerer, the request fails closed."
)


def effectiveApprovalPolicy(events: list) -> Optional[ApprovalPolicy]:
    """会话批准策略覆盖：日志中最后一次 ``approval/policy`` 事件，无则 None。

    纯折叠——resume 无需 catch-up 机制，因为重放日志即状态。
    :param events: 按日志顺序的会话事件（其他类型跳过）。
    :returns: 最后一次切换事件的策略，无切换时返回 None。
    """
    for event in reversed(events):
        if event.type == "approval/policy":
            return event.data.get("policy")
    return None


def _hasOpenTurn(events: list) -> bool:
    """日志当前是否位于打开的回合内（``turn/start`` 尚未被 ``turn/end`` 关闭）。

    审计对必须被回合包裹：回合是耐久日志的提交/重放边界，回合间裸事件与
    崩溃尾不可区分，重载时被静默丢弃。
    """
    for event in reversed(events):
        if event.type == "turn/start":
            return True
        if event.type == "turn/end":
            return False
    return False


def setApprovalPolicy(session: Any, policy: ApprovalPolicy) -> None:
    """追加会话策略覆盖的唯一耐久表示。非法值在日志变更前抛错。

    :param session: 覆盖所属的会话。
    :param policy: 生效直到下次切换的策略。
    :raises TypeError: policy 不是 ``ask``/``never``。
    """
    if policy not in APPROVAL_POLICIES:
        raise TypeError('approval policy must be one of "ask" or "never"')
    session.append("approval/policy", {"policy": policy})


@dataclass
class ApprovalRequest:
    """只读的同进程权限问题。

    :param agent: 代表其发问的 agent——路由问题（UI answerer 只为它拥有的
        agent 作答）并接收其会话日志上的审计事件。
    :param toolName: 问题涉及的工俱名（展示与审计）。
    :param callId: 正在被决定的精确工具调用（asker 有时）——让 UI 把提示
        挂到它已流式的工具调用上。
    :param reason: asker 对人类可读的 WHY 解释。
    :param signal: 取消即撤回问题：请求立即按 ``'cancelled'`` 结算，迟到的
        answer 被丢弃。
    """

    agent: Any  # Agent
    toolName: str
    callId: Optional[str] = None
    reason: Optional[str] = None
    signal: Any = None  # CancelSignal


@dataclass
class Config:
    """插件配置：全部可选（缺省由 schema 默认）。"""

    #: 无 ``approval/policy`` 覆盖的会话的部署默认策略。``'ask'`` 交给组合
    #: answerer（无则 fail-closed）；``'never'`` 不提示直接拒绝。
    policy: ApprovalPolicy = "ask"


class ApprovalService(Service):
    """批准服务（``ctx.approval``）：先应用会话策略再交给 answerer，并把每次
    询问的 ask/outcome 审计对落到请求会话。确定性的策略变化通过
    runtime-context 快照与切换通知暴露给模型。
    """

    def __init__(self, ctx: AppContext, config: Optional[Config] = None) -> None:
        super().__init__(ctx, "approval")
        self.config = config or Config()

        # 完整当前值在保留历史之后传送，所以切换策略不重写稳定 system-prompt
        # 缓存前缀。dsh_py 的 CORE_PROFILE 不含 systemPrompt，做可选守卫。
        if ctx.has_service("systemPrompt"):
            from dsh_py.services.system_prompt import PromptContext

            def _text(context: dict) -> str:
                agent = context.get("agent")
                # 裸 assemble()（测试、诊断）没有会话可陈述。
                if agent is None:
                    return ""
                policy = self.effectivePolicy(agent.session)
                return NEVER_SENTENCE if policy == "never" else ASK_SENTENCE

            ctx.systemPrompt.context(PromptContext(
                name="approval:policy",
                order=115,
                text=_text,
            ))

    # ------------------------------------------------------------------ #
    # 策略
    # ------------------------------------------------------------------ #
    def effectivePolicy(self, session: Any) -> ApprovalPolicy:
        """会话当前生效策略：自身覆盖 ?? 配置默认 ?? 'ask'。"""
        return self.overrideOf(session) or self.config.policy or "ask"

    def overrideOf(self, session: Any) -> Optional[ApprovalPolicy]:
        """只读会话覆盖（不应用配置默认）。"""
        return effectiveApprovalPolicy(list(session.events))

    def setPolicy(self, agent: Any, policy: ApprovalPolicy) -> None:
        """切换一个存活 agent 的策略，并把转变排入其下一次模型步。

        会话初始化用 :func:`setApprovalPolicy` 直接做（无先前可见策略可改）。
        """
        previous = self.effectivePolicy(agent.session)
        if previous == policy:
            return
        setApprovalPolicy(agent.session, policy)
        from dsh_py.services.message import TextBlock, create_user_message

        message = create_user_message(
            [TextBlock(f'The approval policy changed from "{previous}" to "{policy}" (changed by the user).')],
            source=MessageSource(kind="plugin", plugin="user-approval"),
        )
        agent.session.append("user/message", message)

    # ------------------------------------------------------------------ #
    # 请求
    # ------------------------------------------------------------------ #
    async def request(self, req: ApprovalRequest) -> ApprovalOutcome:
        """让组合 answerer 决定一次只读同进程请求。

        前置条件：回合打开（审计对必须被耐久日志的提交/重放边界包裹；空闲
        询问在追加任何东西前拒绝）。answerer 阶段总产出结果：已取消信号 →
        ``'cancelled'``；缺失或抛错 answerer → ``'unavailable'``（fail-closed）；
        越界返回值归一化为 ``'unavailable'``。审计对任一追加失败仍拒绝——
        返回未记录的决策违反配对。

        :raises RuntimeError: 无打开回合时。
        """
        session = req.agent.session
        if not _hasOpenTurn(list(session.events)):
            raise RuntimeError(
                "approval.request() outside an open turn: the approval/asked + approval/decided "
                "audit pair must be turn-enclosed (a bare event between turns is crash-tail "
                "garbage on reload). Ask from inside the turn that needs the decision."
            )
        request_id = ApprovalRequestIdFactory()
        asked: dict = {"id": request_id, "toolName": req.toolName}
        if req.callId is not None:
            asked["callId"] = req.callId
        if req.reason is not None:
            asked["reason"] = req.reason
        session.append("approval/asked", asked)
        outcome = await self._decide(req, session)
        session.append("approval/decided", {"id": request_id, "outcome": outcome})
        return outcome

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    async def _decide(self, req: ApprovalRequest, session: Any) -> ApprovalOutcome:
        """分发瀑布流（contained）并与请求信号竞速。"""
        signal = req.signal
        if signal is not None and signal.aborted:
            return "cancelled"
        # 'never' 在这里决定：注册在服务挂载之后、带 prepend 的监听器会排在
        # 任何 gate 监听器之前，所以只有服务自身的请求路径能守住
        # 「'never' 无论注册顺序如何都确定性拒绝」的承诺。
        if self.effectivePolicy(session) == "never":
            return "rejected"

        async def _inner() -> ApprovalOutcome:
            return "unavailable"

        async def _answer() -> ApprovalOutcome:
            try:
                outcome = await self.ctx.waterfall(
                    "approval/request", req, inner=_inner,
                )
                return outcome if outcome in OUTCOMES else "unavailable"
            except Exception:  # noqa: BLE001 - 抛错 answerer 使问题闭合失败
                return "unavailable"

        answer_task = asyncio.create_task(_answer())
        if signal is None:
            return await answer_task
        # 竞速：取消优先 → 'cancelled'；迟到的 answer 被丢弃（已结算 Promise
        # 上的 resolve 是 no-op）。
        abort_waiter: "asyncio.Future[None]" = asyncio.get_running_loop().create_future()

        def _on_abort() -> None:
            if not abort_waiter.done():
                abort_waiter.set_result(None)

        remove = signal.add_listener(_on_abort)
        try:
            done, _pending = await asyncio.wait(
                {answer_task, abort_waiter}, return_when=asyncio.FIRST_COMPLETED,
            )
            if answer_task in done:
                return answer_task.result()
            answer_task.cancel()
            try:
                await answer_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            return "cancelled"
        finally:
            remove()


__all__ = [
    "ApprovalRequestId",
    "ApprovalRequestIdFactory",
    "ApprovalOutcome",
    "OUTCOMES",
    "ApprovalPolicy",
    "APPROVAL_POLICIES",
    "NEVER_SENTENCE",
    "ASK_SENTENCE",
    "effectiveApprovalPolicy",
    "setApprovalPolicy",
    "ApprovalRequest",
    "Config",
    "ApprovalService",
    "apply",
]


def apply(ctx: AppContext, config: Any = None) -> None:
    """装配：提供 ``ctx.approval`` 服务。"""
    normalized = config if isinstance(config, Config) else Config(**(config or {}))
    ctx.provide("approval", ApprovalService(ctx, normalized))

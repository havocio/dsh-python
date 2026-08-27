"""用户提问能力 seam（``ctx.userQuestions``）：暂停 agent 工具调用直到人类
回答问题（对齐 dsh-user-questions）。模型侧工具在 ``tool-ask-user`` 插件；
UI 包提供唯一活跃 provider。

差异（相对 dsh）：dsh 用 ``ctx.get('agents')``；dsh_py 无 ``ctx.get``，
改用 ``ctx.has_service('agents')`` + 属性访问。``HarnessError`` 从
``dsh_py.services.llm`` 导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.llm import HarnessError

# --------------------------------------------------------------------------- #
# Wire-safe 类型
# --------------------------------------------------------------------------- #


@dataclass
class AskUserQuestionOption:
    """提供给用户的一个可选项。"""

    label: str
    description: Optional[str] = None


@dataclass
class AskUserQuestionIntent:
    """调用方声明的展示意图：这个问题「就是」某种决策，认识该标签的 UI
    可以按此展示，而不是泛化选项列表。只改变展示，不改变协议（回答编码
    相同）。"""

    kind: str  # 'plan-review'
    approve: str  # 批准该计划的选项标签；其余选项都拒绝它


@dataclass
class AskUserQuestionItem:
    """一次提问中的一个问题。"""

    id: str  # 稳定调用方 id，在回答中原样回显
    question: str  # 要展示的问题
    detail: Optional[str] = None  # 随问题渲染的支撑细节（不进选项标签）
    header: Optional[str] = None  # 可选短标题/分组标签
    options: Optional[list[AskUserQuestionOption]] = None  # UI 可渲染为菜单的选项
    multiSelect: bool = False  # 是否允许多选（默认单选）
    intent: Optional[AskUserQuestionIntent] = None  # 可选展示意图


@dataclass
class AskUserQuestionAnswerItem:
    """对一个问题的回答。"""

    id: str
    selected: list[str] = field(default_factory=list)
    custom: Optional[str] = None  # 可选自由文本「其他」回答


@dataclass
class AskUserQuestionAnswer:
    """人类的回答。"""

    answers: list[AskUserQuestionAnswerItem] = field(default_factory=list)


@dataclass
class AskUserQuestionRequest:
    """一次人类回答请求。"""

    questions: list[AskUserQuestionItem]
    agent: Any = None  # 精确的存活调用 agent（来自 agent 工具调用时）
    signal: Any = None  # 所属工具/步的取消信号


class UserQuestionError(HarnessError):
    """user-questions 失败的稳定错误分类。"""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message, code)
        self.name = "UserQuestionError"


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #


class UserQuestionProvider:
    """UI 侧 provider：收集答案。"""

    async def ask(self, request: AskUserQuestionRequest) -> AskUserQuestionAnswer:  # pragma: no cover
        raise NotImplementedError


class UserQuestionService(Service):
    """``ctx.userQuestions``：一个活跃 UI provider 加一个 ``ask()`` API。"""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "userQuestions")
        self._provider: Optional[UserQuestionProvider] = None

    def registerProvider(self, provider: UserQuestionProvider) -> Callable[[], None]:
        """注册 UI provider。一个上下文只能有一个活跃 provider。

        :returns: 注销函数（随调用 fiber 卸载亦可）。
        :raises UserQuestionError: code ``DUPLICATE_PROVIDER``（已注册）。
        """
        if self._provider is not None:
            raise UserQuestionError("a user-questions provider is already registered", "DUPLICATE_PROVIDER")
        self._provider = provider

        def dispose() -> None:
            if self._provider is provider:
                self._provider = None

        return self.ctx.fiber.effect(dispose, "userInteraction.registerProvider()")

    async def ask(self, request: AskUserQuestionRequest) -> AskUserQuestionAnswer:
        """问活跃 UI provider 并等待人类回答。

        提供 agent 时，人类交互只对精确的存活运行时根有效：运行时所有权
        （而非耐久会话谱系）决定边界——被拥有的子 agent 没有人类 answerer，
        会永远阻塞；而带谱系、以新运行时根恢复的会话可以正常询问。

        :raises UserQuestionError: ``ASK_ABORTED`` / ``EMPTY_QUESTIONS`` /
            ``CALLER_NOT_LIVE`` / ``DELEGATED_CALLER`` / ``BAD_INTENT`` /
            ``NO_PROVIDER``。
        """
        if request.signal is not None and request.signal.aborted:
            raise UserQuestionError(
                "ask_user_question was aborted before the user answered", "ASK_ABORTED")
        if len(request.questions) == 0:
            raise UserQuestionError(
                "ask_user_question requires at least one question", "EMPTY_QUESTIONS")
        agent = request.agent
        if agent is not None:
            agents = self.ctx.agents if self.ctx.has_service("agents") else None
            if agents is None or agents.get(agent.id) is not agent:
                raise UserQuestionError(
                    "human interaction requires the exact live calling agent when an agent is supplied",
                    "CALLER_NOT_LIVE")
            if agent not in agents.roots():
                raise UserQuestionError(
                    "human interaction is unavailable while the calling agent is owned by another "
                    "live agent; include the unresolved question or decision in the child agent's "
                    "final result",
                    "DELEGATED_CALLER")
        # 展示意图断言类型无法表达的两件事：命名批准标签必须是本问题自己的
        # 选项之一，且 plan-review 必须携带被审阅的计划。在 asker 处拦截，
        # 而不是在各自 UI。
        for question in request.questions:
            intent = question.intent
            if intent is None:
                continue
            labels = [option.label for option in (question.options or [])]
            if intent.approve not in labels:
                raise UserQuestionError(
                    f"question {question.id} declares intent {intent.kind} whose approve label "
                    f"{intent.approve!r} names none of its options",
                    "BAD_INTENT")
            if question.detail is None:
                raise UserQuestionError(
                    f"question {question.id} declares intent {intent.kind} without the detail it reviews",
                    "BAD_INTENT")
        if self._provider is None:
            raise UserQuestionError("no user-questions provider is registered", "NO_PROVIDER")
        return await self._provider.ask(request)


__all__ = [
    "AskUserQuestionOption",
    "AskUserQuestionIntent",
    "AskUserQuestionItem",
    "AskUserQuestionAnswerItem",
    "AskUserQuestionAnswer",
    "AskUserQuestionRequest",
    "UserQuestionError",
    "UserQuestionProvider",
    "UserQuestionService",
    "apply",
]


def apply(ctx: AppContext, config: Any = None) -> None:
    """装配：提供 ``ctx.userQuestions`` 服务。"""
    ctx.provide("userQuestions", UserQuestionService(ctx))

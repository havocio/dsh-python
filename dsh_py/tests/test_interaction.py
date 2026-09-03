"""interaction 家族契约单测（纯逻辑 / mock，不依赖网络或外部 provider）。

覆盖 dsh ``packages/interaction`` 下五个子包的纯逻辑可测面：

- ``commands``     → ``parseCommand`` / ``_normalizeDefinition`` / ``_normalizeResult`` / ``_renderThrown`` / ``_abortError``
- ``user-approval``→ ``ApprovalRequestIdFactory`` / ``effectiveApprovalPolicy`` / ``_hasOpenTurn`` / ``ApprovalRequest``
- ``user-questions``→ ``UserQuestionError`` / ``UserQuestionService.ask`` 校验 / ``registerProvider`` 重复检测
- ``permission-presets``→ ``effectivePermissionPreset`` / ``applyKnobEvent`` / ``foldKnobs`` / ``_DEFAULT_PRESETS``
- ``tool-ask-user``→ ``apply`` 失败快路径 / schema 校验

运行范式与仓库其他 ``test_*.py`` 一致：底部 ``if __name__ == "__main__"`` 自跑全部 ``test_*``。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

from dsh_py.services.commands import (
    CommandDefinition,
    CommandInputDescriptor,
    CommandResult,
    ParsedCommand,
    _abortError,
    _normalizeDefinition,
    _normalizeResult,
    _renderThrown,
    _RegisteredCommand,
    parseCommand,
)
from dsh_py.services.user_approval import (
    APPROVAL_POLICIES,
    ApprovalRequest,
    ApprovalRequestIdFactory,
    _hasOpenTurn,
    effectiveApprovalPolicy,
)
from dsh_py.services.user_questions import (
    UserQuestionError,
    UserQuestionService,
    AskUserQuestionIntent,
    AskUserQuestionItem,
    AskUserQuestionOption,
    AskUserQuestionRequest,
)
from dsh_py.services.llm import HarnessError
from dsh_py.services.permission_presets import (
    EMPTY_KNOBS,
    KnobState,
    _DEFAULT_PRESETS,
    applyKnobEvent,
    effectivePermissionPreset,
    foldKnobs,
)
from dsh_py.plugins.tool_ask_user import (
    ASK_USER_QUESTION_SCHEMA,
    apply as apply_tool_ask_user,
)


# --------------------------------------------------------------------------- #
# 最小 mock 基础设施
# --------------------------------------------------------------------------- #
class _Ev:
    """伪造会话事件：``event.type`` + ``event.data``（与 dsh_py 事件契约一致）。"""

    def __init__(self, type: str, data: Optional[dict] = None) -> None:
        self.type = type
        self.data = data if data is not None else {}


class _Sig:
    """伪造取消信号：``.aborted`` + ``.reason``。"""

    def __init__(self, aborted: bool = False, reason: Any = None) -> None:
        self.aborted = aborted
        self.reason = reason


class _Agent:
    def __init__(self, aid: str) -> None:
        self.id = aid


class _FakeFiber:
    def effect(self, fn, label):  # noqa: ANN001
        return fn


class _FakeAgents:
    def __init__(self, agents: Optional[dict] = None, roots: Optional[set] = None) -> None:
        self._map = agents or {}
        self._roots = roots or set()

    def get(self, aid):
        return self._map.get(aid)

    def roots(self):
        return self._roots


class _UserQuestionsCtx:
    """``UserQuestionService`` 需要的 AppContext 最小镜像。"""

    def __init__(self, with_agents: bool = False) -> None:
        self.provided: list = []
        self.fiber = _FakeFiber()
        self._with_agents = with_agents
        self.agents = _FakeAgents()

    def provide(self, name: str, svc: Any) -> None:
        self.provided.append(name)

    def has_service(self, name: str) -> bool:
        if name == "agents":
            return self._with_agents
        return False


class _Tools:
    def __init__(self) -> None:
        self.registered: list = []

    def register(self, name, description, schema, handler):  # noqa: ANN001
        self.registered.append((name, description, schema, handler))


class _ToolCtx:
    """``tool_ask_user.apply`` 需要的 AppContext 最小镜像。"""

    def __init__(self, tools: bool, user_questions: bool) -> None:
        self.provided: list = []
        self._tools = _Tools() if tools else None
        self._has_uq = user_questions

    def provide(self, name: str, svc: Any) -> None:
        self.provided.append(name)

    def has_service(self, name: str) -> bool:
        if name == "tools":
            return self._tools is not None
        if name == "userQuestions":
            return self._has_uq
        return False

    @property
    def tools(self):
        return self._tools


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def test_parse_command() -> None:
    # 合法斜杠命令 → 解析出 name + 尾部原始输入
    pc = parseCommand("/foo")
    assert isinstance(pc, ParsedCommand)
    assert pc.name == "foo"
    assert pc.rawInput == ""

    pc = parseCommand("/foo bar baz")
    assert pc.name == "foo"
    assert pc.rawInput == " bar baz"

    # 非命令、裸斜杠、前导空白、大写名均不匹配
    assert parseCommand("foo") is None
    assert parseCommand("/") is None
    assert parseCommand("  /foo") is None
    assert parseCommand("/Foo") is None
    print("  ✓ parseCommand 解析/拒绝边界正确")


def test_normalize_definition() -> None:
    ok = _normalizeDefinition(CommandDefinition(
        name="ok-cmd1", description="does a thing",
        handler=lambda inv: CommandResult(kind="success")))
    assert isinstance(ok, _RegisteredCommand)
    assert ok.descriptor.name == "ok-cmd1"

    # 非法名（大写）/ 空描述 / 非函数 handler / 空 hint → TypeError
    for bad in (
        CommandDefinition(name="OK", description="d", handler=lambda inv: None),
        CommandDefinition(name="ok", description="   ", handler=lambda inv: None),
        CommandDefinition(name="ok", description="d", handler="not-fn"),  # type: ignore[arg-type]
        CommandDefinition(name="ok", description="d", handler=lambda inv: None,
                         input=CommandInputDescriptor(hint="  ")),
    ):
        try:
            _normalizeDefinition(bad)
            raise AssertionError("应抛 TypeError")
        except TypeError:
            pass
    print("  ✓ _normalizeDefinition 元数据校验正确")


def test_normalize_result() -> None:
    # CommandResult 原样归一
    assert _normalizeResult("c", CommandResult(kind="success")) == CommandResult(kind="success", text=None, sourceEventSeq=None)
    # dict 载荷 → CommandResult
    r = _normalizeResult("c", {"kind": "error", "text": "boom"})
    assert r == CommandResult(kind="error", text="boom")

    # 非法：非 dict/CommandResult、未知 kind、success 文本非 str、error 文本空
    for bad in (
        "not-a-result",
        {"kind": "weird"},
        {"kind": "success", "text": 123},
        {"kind": "error", "text": ""},
    ):
        try:
            _normalizeResult("c", bad)
            raise AssertionError("应抛 TypeError")
        except TypeError:
            pass
    print("  ✓ _normalizeResult 边界校验正确")


def test_render_thrown() -> None:
    assert _renderThrown("plain") == "plain"

    class _Boom:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert _renderThrown(_Boom()) == "<unrenderable thrown value>"
    print("  ✓ _renderThrown 兜底渲染正确")


def test_abort_error() -> None:
    err = RuntimeError("root")
    assert _abortError(_Sig(reason=err)) is err
    assert isinstance(_abortError(_Sig(reason="msg")), RuntimeError)
    assert _abortError(_Sig(reason="msg")).args[0] == "msg"
    aborted = _abortError(_Sig())
    assert isinstance(aborted, RuntimeError) and aborted.args[0] == "command aborted"
    print("  ✓ _abortError 收敛取消原因正确")


# --------------------------------------------------------------------------- #
# user-approval
# --------------------------------------------------------------------------- #
def test_approval_request_id_factory() -> None:
    a = ApprovalRequestIdFactory()
    b = ApprovalRequestIdFactory()
    assert isinstance(a, str) and len(a) == 32  # uuid4 hex
    assert a != b
    print("  ✓ ApprovalRequestIdFactory 唯一 hex id")


def test_effective_approval_policy() -> None:
    # 最后一次切换即生效策略（reversed 取最近）
    events = [_Ev("approval/policy", {"policy": "ask"}),
              _Ev("turn/start"),
              _Ev("approval/policy", {"policy": "never"})]
    assert effectiveApprovalPolicy(events) == "never"
    # 无策略事件 → None
    assert effectiveApprovalPolicy([_Ev("turn/start"), _Ev("turn/end")]) is None
    # 空日志 → None
    assert effectiveApprovalPolicy([]) is None
    assert "ask" in APPROVAL_POLICIES and "never" in APPROVAL_POLICIES
    print("  ✓ effectiveApprovalPolicy 折叠正确")


def test_has_open_turn() -> None:
    assert _hasOpenTurn([]) is False
    assert _hasOpenTurn([_Ev("turn/start")]) is True
    assert _hasOpenTurn([_Ev("turn/start"), _Ev("turn/end")]) is False
    assert _hasOpenTurn([_Ev("turn/end"), _Ev("turn/start")]) is True
    print("  ✓ _hasOpenTurn 回合边界正确")


def test_approval_request_construction() -> None:
    req = ApprovalRequest(agent=_Agent("a1"), toolName="bash", callId="c1", reason="why")
    assert req.agent.id == "a1"
    assert req.toolName == "bash"
    assert req.callId == "c1"
    assert req.reason == "why"
    print("  ✓ ApprovalRequest 字段正确")


# --------------------------------------------------------------------------- #
# user-questions
# --------------------------------------------------------------------------- #
def test_user_question_error() -> None:
    e = UserQuestionError("boom", "CODE_X")
    assert e.code == "CODE_X"
    assert isinstance(e, HarnessError)
    print("  ✓ UserQuestionError 携带稳定 code")


def test_user_questions_register_duplicate() -> None:
    svc = UserQuestionService(_UserQuestionsCtx())
    svc.registerProvider(_StubProvider())
    try:
        svc.registerProvider(_StubProvider())
        raise AssertionError("重复 provider 应抛错")
    except UserQuestionError as e:
        assert e.code == "DUPLICATE_PROVIDER"
    print("  ✓ registerProvider 拒绝重复注册")


async def test_user_questions_ask_validation() -> None:
    svc = UserQuestionService(_UserQuestionsCtx())

    # NO_PROVIDER：未注册 provider
    try:
        await svc.ask(AskUserQuestionRequest(
            questions=[AskUserQuestionItem(id="q1", question="?")]))
        raise AssertionError("无 provider 应抛 NO_PROVIDER")
    except UserQuestionError as e:
        assert e.code == "NO_PROVIDER"

    # EMPTY_QUESTIONS
    try:
        await svc.ask(AskUserQuestionRequest(questions=[]))
        raise AssertionError("空问题应抛 EMPTY_QUESTIONS")
    except UserQuestionError as e:
        assert e.code == "EMPTY_QUESTIONS"

    # ASK_ABORTED
    try:
        await svc.ask(AskUserQuestionRequest(
            questions=[AskUserQuestionItem(id="q1", question="?")],
            signal=_Sig(aborted=True)))
        raise AssertionError("已取消应抛 ASK_ABORTED")
    except UserQuestionError as e:
        assert e.code == "ASK_ABORTED"

    # BAD_INTENT：approve 标签不在选项 / 缺 detail
    svc.registerProvider(_StubProvider())
    for bad_intent in (
        AskUserQuestionIntent(kind="plan-review", approve="yes"),  # 无 options
        AskUserQuestionIntent(kind="plan-review", approve="yes"),  # 有 options 但 approve 不在其中（见下）
    ):
        pass
    # 显式构造两种 BAD_INTENT
    try:
        await svc.ask(AskUserQuestionRequest(questions=[AskUserQuestionItem(
            id="q1", question="?",
            options=[AskUserQuestionOption(label="a")],
            intent=AskUserQuestionIntent(kind="plan-review", approve="missing"))]))
        raise AssertionError("approve 不在选项应抛 BAD_INTENT")
    except UserQuestionError as e:
        assert e.code == "BAD_INTENT"
    try:
        await svc.ask(AskUserQuestionRequest(questions=[AskUserQuestionItem(
            id="q1", question="?",
            options=[AskUserQuestionOption(label="yes")],
            intent=AskUserQuestionIntent(kind="plan-review", approve="yes"))]))  # 缺 detail
        raise AssertionError("plan-review 缺 detail 应抛 BAD_INTENT")
    except UserQuestionError as e:
        assert e.code == "BAD_INTENT"
    print("  ✓ UserQuestionService.ask 校验路径正确")


async def test_user_questions_caller_not_live() -> None:
    # 提供 agents 服务但 agent 不在映射 → CALLER_NOT_LIVE
    ctx = _UserQuestionsCtx(with_agents=True)
    svc = UserQuestionService(ctx)
    svc.registerProvider(_StubProvider())
    agent = _Agent("a1")
    try:
        await svc.ask(AskUserQuestionRequest(
            questions=[AskUserQuestionItem(id="q1", question="?")], agent=agent))
        raise AssertionError("agent 不存在应抛 CALLER_NOT_LIVE")
    except UserQuestionError as e:
        assert e.code == "CALLER_NOT_LIVE"

    # agent 存在但非 roots → DELEGATED_CALLER
    ctx.agents = _FakeAgents(agents={"a1": agent}, roots=set())
    try:
        await svc.ask(AskUserQuestionRequest(
            questions=[AskUserQuestionItem(id="q1", question="?")], agent=agent))
        raise AssertionError("被拥有的 agent 应抛 DELEGATED_CALLER")
    except UserQuestionError as e:
        assert e.code == "DELEGATED_CALLER"
    print("  ✓ caller liveness 校验正确")


class _StubProvider:
    """最小 provider（不真正提问）。"""

    async def ask(self, request):  # noqa: ANN001
        from dsh_py.services.user_questions import AskUserQuestionAnswer
        return AskUserQuestionAnswer(answers=[])


# --------------------------------------------------------------------------- #
# permission-presets
# --------------------------------------------------------------------------- #
def test_permission_preset_fold() -> None:
    # 最后选择的预设覆盖
    assert effectivePermissionPreset([
        _Ev("permission/preset", {"preset": "p1"}),
        _Ev("turn/start"),
        _Ev("permission/preset", {"preset": "p2"}),
    ]) == "p2"
    assert effectivePermissionPreset([_Ev("turn/start")]) is None

    # applyKnobEvent：三类旋钮转移 + 未知事件返回同一引用
    assert applyKnobEvent(EMPTY_KNOBS, _Ev("permission/preset", {"preset": "p"})) \
        == KnobState(preset="p", sandbox=None, approval=None)
    assert applyKnobEvent(EMPTY_KNOBS, _Ev("sandbox/mode", {"mode": "y"})) \
        == KnobState(sandbox="y")
    assert applyKnobEvent(EMPTY_KNOBS, _Ev("approval/policy", {"policy": "ask"})) \
        == KnobState(approval="ask")
    same = KnobState(preset="x")
    assert applyKnobEvent(same, _Ev("other", {})) is same

    # foldKnobs 整日志折叠
    final = foldKnobs([
        _Ev("permission/preset", {"preset": "p"}),
        _Ev("sandbox/mode", {"mode": "m"}),
        _Ev("approval/policy", {"policy": "never"}),
    ])
    assert final == KnobState(preset="p", sandbox="m", approval="never")
    print("  ✓ permission-preset 旋钮折叠正确")


def test_default_presets() -> None:
    assert set(_DEFAULT_PRESETS.keys()) == {"workspace-write", "danger-full-access"}
    assert _DEFAULT_PRESETS["workspace-write"].sandbox == "workspace-write"
    assert _DEFAULT_PRESETS["workspace-write"].approval == "ask"
    assert _DEFAULT_PRESETS["danger-full-access"].sandbox == "danger-full-access"
    assert _DEFAULT_PRESETS["danger-full-access"].approval == "never"
    print("  ✓ 默认预设捆绑正确")


# --------------------------------------------------------------------------- #
# tool-ask-user
# --------------------------------------------------------------------------- #
def test_tool_ask_user_apply_requires_services() -> None:
    # 缺 tools → RuntimeError
    try:
        apply_tool_ask_user(_ToolCtx(tools=False, user_questions=True))
        raise AssertionError("缺 tools 应抛 RuntimeError")
    except RuntimeError as e:
        assert "tools" in str(e)
    # 缺 userQuestions → RuntimeError
    try:
        apply_tool_ask_user(_ToolCtx(tools=True, user_questions=False))
        raise AssertionError("缺 userQuestions 应抛 RuntimeError")
    except RuntimeError as e:
        assert "userQuestions" in str(e)
    print("  ✓ tool-ask-user apply 失败快路径正确")


def test_tool_ask_user_registers_tool() -> None:
    ctx = _ToolCtx(tools=True, user_questions=True)
    apply_tool_ask_user(ctx)
    assert len(ctx.tools.registered) == 1
    name, _desc, _schema, handler = ctx.tools.registered[0]
    assert name == "ask_user_question"

    # schema 必须声明 questions 为必填，且每条问题要求 id + question
    assert ASK_USER_QUESTION_SCHEMA["required"] == ["questions"]
    items = ASK_USER_QUESTION_SCHEMA["properties"]["questions"]["items"]
    assert items["required"] == ["id", "question"]
    assert asyncio.iscoroutinefunction(handler)
    print("  ✓ tool-ask-user 注册 ask_user_question + schema 正确")


# --------------------------------------------------------------------------- #
# 自跑入口
# --------------------------------------------------------------------------- #
def _main() -> None:
    print("== test_interaction ==")
    test_parse_command()
    test_normalize_definition()
    test_normalize_result()
    test_render_thrown()
    test_abort_error()
    test_approval_request_id_factory()
    test_effective_approval_policy()
    test_has_open_turn()
    test_approval_request_construction()
    test_user_question_error()
    test_user_questions_register_duplicate()
    asyncio.run(test_user_questions_ask_validation())
    asyncio.run(test_user_questions_caller_not_live())
    test_permission_preset_fold()
    test_default_presets()
    test_tool_ask_user_apply_requires_services()
    test_tool_ask_user_registers_tool()
    print("== test_interaction: ALL PASS ==")


if __name__ == "__main__":
    _main()

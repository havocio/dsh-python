"""插件拥有的「人类命令」注册表，供交互式 UI 适配器共享（对齐 dsh-commands）。

命令以斜杠开头（``/name``），由 UI 侧把用户键入的一行文本交给
:meth:`CommandRuntime.execute`；解析成功的命令**不发给模型**，直接调注册的
handler，并把整个生命周期（``command/run`` → ``command/done``）以 log-only
事件落到接收命令的 agent 会话（与 ``tool/call``↔``tool/result`` 配对同构）。

分层规则（复用 dsh-scope 的 ScopedLayers）：普通上下文注册的命令是全局的；
通过某个 agent 的 ``agent.ctx`` 命令注入插件注册的定义，对该 agent 遮蔽同名
全局定义。

差异（相对 dsh）：dsh 的 ``CommandRuntime`` 继承 ``TypertRemoteService`` 并把
``list``/``execute`` 标 ``@Remote``；dsh_py 的 typert 是独立注册表 + 元数据装饰
器，本服务继承 ``Service`` 并保留 ``@remote`` 打标（供装配方注册），Wire 形态
不变。``commands/change`` 通知用 ``ctx.emit``（dsh_py 事件分发逐个隔离监听器
异常，非否决）。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, NewType, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.scope import NamedEntries, ScopeKey, ScopedLayers
from dsh_py.services.typert import remote

# --------------------------------------------------------------------------- #
# 词汇
# --------------------------------------------------------------------------- #

#: 配对一次 command 执行的生命周期 id：``command/run`` 与 ``command/done`` 同
#: 名命令共享，供表面（UI）把 admission 响应与流程节点关联。
CommandId = NewType("CommandId", str)

#: 命令名合法形状：小写字母开头，后续小写字母/数字/连字符/下划线。
COMMAND_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")

#: 斜杠命令解析：名字之后紧跟空白或行尾。
COMMAND_LINE = re.compile(r"^\/([a-z][a-z0-9_-]*)(?=$|[\t\n\r ])")

#: 一次命令执行中配对 id 的实例前缀长度（进程重启后 resume 日志不重复）。
_INSTANCE_TOKEN = uuid.uuid4().hex[:8]


@dataclass(frozen=True)
class CommandInputDescriptor:
    """可选非结构化输入的自描述元数据（占位提示）。"""

    hint: str


@dataclass(frozen=True)
class CommandResult:
    """已规整的命令结果：``kind`` 判别 + 可选文本 / 权威来源事件。

    - ``kind == "success"``：``text`` 可选；``sourceEventSeq`` 指向更早的权威
      领域事件（由更丰富的展示承载）。
    - ``kind == "error"``：``text`` 必填（非空）。
    """

    kind: str  # 'success' | 'error'
    text: Optional[str] = None
    sourceEventSeq: Optional[int] = None


@dataclass(frozen=True)
class CommandExecution:
    """一次已结算的命令执行：handler 的规整结果 + 生命周期配对 id。"""

    commandId: CommandId
    result: CommandResult


@dataclass(frozen=True)
class CommandDescriptor:
    """UI 适配器拿到的不可变命令视图（无 handler）。"""

    name: str
    description: str
    input: Optional[CommandInputDescriptor] = None


@dataclass(frozen=True)
class ParsedCommand:
    """语法上合法的斜杠命令（尚未做注册表解析）。"""

    name: str
    rawInput: str


@dataclass
class CommandInvocation:
    """交给已注册命令 handler 的一次调用。"""

    commandId: CommandId
    agent: Any  # Agent
    rawInput: str
    signal: Any  # CancelSignal


@dataclass
class CommandDefinition:
    """命令注册：发现元数据 + 直接 UI handler。"""

    name: str
    description: str
    input: Optional[CommandInputDescriptor] = None
    recordInput: Optional[bool] = None
    handler: Callable[[CommandInvocation], Any] = field(default=lambda inv: CommandResult(kind="success"))  # noqa: E731


@dataclass
class _RegisteredCommand:
    """注册表内部：原始定义 + 冻结描述符。"""

    definition: CommandDefinition
    descriptor: CommandDescriptor


def parseCommand(line: str) -> Optional[ParsedCommand]:
    """解析一条精确的斜杠命令（不做尾部输入归一化）。

    :param line: 完整候选命令行。
    :returns: 解析结果；当该行不是命令时返回 None。
    """
    match = COMMAND_LINE.match(line)
    if match is None:
        return None
    name = match.group(1)
    if not name:
        return None
    return ParsedCommand(name=name, rawInput=line[match.end():])


def _abortError(signal: Any) -> Exception:
    """把任意取消原因收敛为一个稳定的拒绝异常。"""
    reason = getattr(signal, "reason", None)
    if isinstance(reason, Exception):
        return reason
    if isinstance(reason, str):
        return RuntimeError(reason)
    return RuntimeError("command aborted")


def _renderThrown(value: Any) -> str:
    """渲染任意抛出的值，不信任其字符串强转。"""
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return "<unrenderable thrown value>"


async def _withAbort(promise: Any, signal: Any) -> Any:
    """一旦所属 UI 请求取消就停止等待不配合的 handler。

    handler 的迟到结果被丢弃（取消任务），取消先到时抛取消异常。
    """
    if signal.aborted:
        raise _abortError(signal)
    task = asyncio.ensure_future(promise)
    if signal is None:
        return await task
    loop = asyncio.get_running_loop()
    abort_waiter: "asyncio.Future[None]" = loop.create_future()

    def _on_abort() -> None:
        if not abort_waiter.done():
            abort_waiter.set_result(None)

    remove = signal.add_listener(_on_abort)
    try:
        done, _pending = await asyncio.wait({task, abort_waiter}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()  # 成功或异常原样传播
        # 取消赢了：丢弃迟到结果，报告取消
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        raise _abortError(signal)
    finally:
        remove()


def _normalizeDefinition(definition: CommandDefinition) -> _RegisteredCommand:
    """在进入 UI 协议前拒绝非法命令元数据。"""
    if not COMMAND_NAME.match(definition.name):
        raise TypeError(f'command name "{definition.name}" must match {COMMAND_NAME.pattern}')
    if not isinstance(definition.description, str):
        raise TypeError(f'command "{definition.name}" description must be a string')
    if definition.description.strip() == "":
        raise TypeError(f'command "{definition.name}" description must not be empty')
    if not callable(definition.handler):
        raise TypeError(f'command "{definition.name}" handler must be a function')
    input_descriptor: Optional[CommandInputDescriptor] = None
    if definition.input is not None:
        if not isinstance(definition.input.hint, str) or definition.input.hint.strip() == "":
            raise TypeError(f'command "{definition.name}" input hint must be a non-empty string')
        input_descriptor = CommandInputDescriptor(hint=definition.input.hint)
    normalized = CommandDefinition(
        name=definition.name,
        description=definition.description,
        input=input_descriptor,
        recordInput=definition.recordInput,
        handler=definition.handler,
    )
    descriptor = CommandDescriptor(
        name=normalized.name,
        description=normalized.description,
        input=input_descriptor,
    )
    return _RegisteredCommand(definition=normalized, descriptor=descriptor)


def _normalizeResult(command: str, value: Any) -> CommandResult:
    """在注册表边界校验并剥离不可信 handler 返回。"""
    if isinstance(value, CommandResult):
        result = value
    elif isinstance(value, dict):
        result = CommandResult(kind=value.get("kind", ""), text=value.get("text"), sourceEventSeq=value.get("sourceEventSeq"))
    else:
        raise TypeError(f'command "{command}" handler must return a CommandResult')
    if result.kind == "success":
        if result.text is not None and not isinstance(result.text, str):
            raise TypeError(f'command "{command}" success text must be a string when supplied')
        if result.sourceEventSeq is not None and (
            not isinstance(result.sourceEventSeq, int) or result.sourceEventSeq < 0
        ):
            raise TypeError(f'command "{command}" success sourceEventSeq must be a non-negative safe integer when supplied')
        return CommandResult(kind="success", text=result.text, sourceEventSeq=result.sourceEventSeq)
    if result.kind == "error":
        if not isinstance(result.text, str) or result.text.strip() == "":
            raise TypeError(f'command "{command}" error text must be a non-empty string')
        return CommandResult(kind="error", text=result.text)
    raise TypeError(f'command "{command}" returned unknown result kind "{result.kind}"')


def _warn(ctx: AppContext, message: str) -> None:
    """防御性告警：无 logger 服务（裸 ctx）时静默。"""
    try:
        ctx.logger.warn(message)
    except (AttributeError, Exception):  # noqa: BLE001
        pass


class _CommandLayer:
    """一个 global 或作用域层拥有的全部命令注册。"""

    def __init__(self, scope: Optional[ScopeKey]) -> None:
        self.commands = NamedEntries(_duplicate_error(scope))

    def isEmpty(self) -> bool:
        return self.commands.isEmpty()


def _duplicate_error(scope: Optional[ScopeKey]) -> Callable[[str], Exception]:
    def build(name: str) -> Exception:
        if scope is None:
            return RuntimeError(
                f'command "{name}" is already registered (for a per-agent variant, '
                "mount a command-injected plugin under that agent's agent.ctx)"
            )
        return RuntimeError(f'command "{name}" is already registered in this scope')

    return build


class CommandRuntime(Service):
    """人类命令注册表（``ctx.commands``）。

    普通上下文定义全局可见；通过 agent 命令注入子上下文注册的定义对
    该 agent 遮蔽全局。``execute`` 在 handler 前写 ``command/run``、结算后写
    ``command/done``（抛出/取消按 ``kind: 'error'`` 结算）；两者都是直接
    log-only 追加，不打开回合，持久化在常规检查点排空。
    """

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "commands")
        self._layers: ScopedLayers = ScopedLayers(
            lambda scope: _CommandLayer(scope),
            self._notifyChange,
        )
        self._commandSeq = 0

    # ------------------------------------------------------------------ #
    # 注册 / 查询
    # ------------------------------------------------------------------ #
    def register(self, definition: CommandDefinition) -> Callable[[], bool]:
        """注册一个全局或调用 agent 作用域命令；返回精确注销函数。

        :raises TypeError: 元数据非法（名字形状 / 描述空 / handler 非函数）。
        :raises RuntimeError: 同名命令已注册（同层内）。
        """
        registered = _normalizeDefinition(definition)

        def action(layer: _CommandLayer) -> Callable[[], None]:
            return layer.commands.insert(registered.definition.name, registered)

        return self._layers.effect(self.ctx, action, "commands.register()")

    @remote("list")
    def list(self, agent: Any) -> list[CommandDescriptor]:
        """列出某 agent 有效（遮蔽后）的不可变命令描述符，按名字排序。"""
        return sorted(
            (command.descriptor for command in self._view(agent).values()),
            key=lambda descriptor: descriptor.name,
        )

    def find(self, agent: Any, name: str) -> Optional[CommandDefinition]:
        """解析一条有效命令定义：作用域遮蔽或全局。"""
        command = self._view(agent).get(name)
        return command.definition if command is not None else None

    @remote("execute")
    async def execute(
        self,
        agent: Any,
        line: str,
        signal: Any,
    ) -> Optional[CommandExecution]:
        """解析并执行一条已知命令（不发给模型）。

        语法或名字未命中时返回 None（不写任何日志）；``command/run`` 追加
        失败响亮失败；handler 失败路径上 ``command/done`` 追加失败被包含，
        保持 handler 自身错误为报告失败。
        """
        parsed = parseCommand(line)
        if parsed is None:
            return None
        command = self._view(agent).get(parsed.name)
        if command is None:
            return None
        if signal.aborted:
            raise _abortError(signal)
        command_id = self._mintCommandId()
        run_data: dict = {
            "commandId": command_id,
            "name": parsed.name,
            "source": {"kind": "user"},
        }
        if command.definition.recordInput is not False:
            run_data["args"] = parsed.rawInput
        self.appendLifecycle(agent.session, "command/run", run_data)
        invocation = CommandInvocation(commandId=command_id, agent=agent, rawInput=parsed.rawInput, signal=signal)
        try:
            output = command.definition.handler(invocation)
            result = _normalizeResult(
                parsed.name,
                await _withAbort(_maybe_coro(output), signal),
            )
        except Exception as error:  # noqa: BLE001
            try:
                self.appendLifecycle(agent.session, "command/done", {
                    "commandId": command_id,
                    "kind": "error",
                    "text": error.message if isinstance(error, Exception) and hasattr(error, "message") else _renderThrown(error),
                })
            except Exception as append_error:  # noqa: BLE001
                _warn(self.ctx, f'command "{parsed.name}": command/done append failed: {_renderThrown(append_error)}')
            raise
        done_data: dict = {"commandId": command_id, "kind": result.kind}
        if result.text is not None:
            done_data["text"] = result.text
        if result.kind == "success" and result.sourceEventSeq is not None:
            done_data["sourceEventSeq"] = result.sourceEventSeq
        self.appendLifecycle(agent.session, "command/done", done_data)
        return CommandExecution(commandId=command_id, result=result)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _mintCommandId(self) -> CommandId:
        """铸造下一个配对 id（单调；实例前缀保证 resume 日志不重复）。"""
        self._commandSeq += 1
        return CommandId(f"cmd-{_INSTANCE_TOKEN}-{self._commandSeq}")

    def appendLifecycle(self, session: Any, event_type: str, data: dict) -> Any:
        """直接追加一条 log-only 生命周期事件（不打开回合、不强刷）。"""
        return session.append(event_type, data)

    def _view(self, agent: Any) -> dict:
        """全局定义 + 沿 agent 作用域链的精确遮蔽（近层胜出）。"""
        return self._layers.merge(agent, lambda layer: layer.commands)

    def _notifyChange(self) -> None:
        """通知每个注册表观察者；不使 UI 刷新成为承重路径。"""
        self.ctx.emit("commands/change")


async def _maybe_coro(value: Any) -> Any:
    """把同步 handler 返回值规范为可等待对象（协程/值原样透传）。"""
    if asyncio.iscoroutine(value) or hasattr(value, "__await__"):
        return await value
    return value


__all__ = [
    "CommandId",
    "CommandInputDescriptor",
    "CommandResult",
    "CommandExecution",
    "CommandDescriptor",
    "ParsedCommand",
    "CommandInvocation",
    "CommandDefinition",
    "parseCommand",
    "CommandRuntime",
    "apply",
]


def apply(ctx: AppContext, config: Any = None) -> None:
    """装配：提供 ``ctx.commands`` 服务。"""
    ctx.provide("commands", CommandRuntime(ctx))

"""命令注册表（commands seam，对标 dsh 的 ``dsh-commands`` 核心子集）：人类可调用的
斜杠命令（slash command）。

一条命令是 ``name`` 对应一段 handler：``async/def handler(invocation) -> CommandResult``。
命令按名称注册，``invoke`` 构造 :class:`CommandInvocation`（携带目标 agent、取消
信号、发起命令 id 与原始输入）并分派。

- :class:`CommandsService.register` —— 注册命令（同名覆盖）；
- :class:`CommandsService.invoke` —— 按名称执行并返回 :class:`CommandResult`；
- :class:`CommandResult` —— ``kind``（success/error）+ 人类文本 + 可选来源事件 seq。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Union

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service


@dataclass
class CommandInvocation:
    """一次命令调用的上下文。"""

    agent: Any                       # 命令作用的 agent（目标会话与循环）
    signal: Any = None               # 取消信号（命令处理须尊重）
    commandId: Optional[str] = None  # 发起命令的身份（手工命令溯源）
    rawInput: str = ""               # 命令的原始参数文本（无参数命令须校验为空）


@dataclass
class CommandResult:
    """一次命令执行的最终结果。"""

    kind: str                        # 'success' | 'error'
    text: str                        # 面向用户的人类文本
    sourceEventSeq: Optional[int] = None  # 命令落地的关键事件 seq（如摘要事件）


# 命令 handler：同步或异步均可（invoke 统一归一）
CommandHandler = Callable[[CommandInvocation], Union[CommandResult, Awaitable[CommandResult]]]


class CommandsService(Service):
    """``commands`` 服务：命令注册表与执行器（``ctx.commands``）。"""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "commands")
        self._commands: dict[str, dict] = {}

    def register(self, name: str, description: str, handler: CommandHandler) -> None:
        """登记一条命令（同名覆盖；name 非空）。"""
        if not name:
            raise ValueError("命令名不能为空")
        self._commands[name] = {"name": name, "description": description, "handler": handler}

    def has(self, name: str) -> bool:
        return name in self._commands

    def list(self) -> list[dict]:
        """列出全部已注册命令（名称与描述）。"""
        return [{"name": c["name"], "description": c["description"]} for c in self._commands.values()]

    async def invoke(
        self,
        name: str,
        agent: Any,
        signal: Any = None,
        command_id: Optional[str] = None,
        raw_input: str = "",
    ) -> CommandResult:
        """执行一条命令；未注册时返回错误结果（命令失败作为文本回流，不抛）。"""
        entry = self._commands.get(name)
        if entry is None:
            return CommandResult(kind="error", text=f"未知命令: /{name}")
        invocation = CommandInvocation(
            agent=agent, signal=signal, commandId=command_id, rawInput=raw_input,
        )
        try:
            result = entry["handler"](invocation)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, CommandResult):
                raise TypeError("命令 handler 必须返回 CommandResult")
            return result
        except Exception as exc:  # noqa: BLE001 - 命令错误作为文本回流
            return CommandResult(kind="error", text=f"/{name} 执行失败: {exc}")


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``commands`` 服务（命令注册表）。"""
    CommandsService(ctx)


apply.provides = ["commands"]  # 声明：本插件提供 commands 服务

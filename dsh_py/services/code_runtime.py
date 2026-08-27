"""code-runtime 能力 seam（对标 dsh 的 ``@deepseek-ai/dsh-code-runtime``）。

代码执行能力 seam：以「模型写的一段程序 + 主机异步绑定」为输入，捕获其打印与
返回值。运行时对 tools / sessions 一无所知——消费方（如 Code Mode）拥有这些关注点。

本文件只定义契约（词汇表、请求/结果、失败分类、绑定命名空间校验）与抽象服务
``CodeRuntime``；具体后端在 ``code_runtime_local.py``（进程级本地后端）。

遵循约定：源码中文注释/docstring；依赖策略——好用即用，本期未引入新第三方依赖。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service

# --------------------------------------------------------------------------- #
# 词汇表（契约常量）
# --------------------------------------------------------------------------- #

#: 每个后端都拒绝的绑定全局名——因为某些后端在程序命名空间里拥有该槽位：
#: ``console``（worker 的日志捕获）、``__dsh_main__`` / ``__builtins__`` /
#: ``__name__`` / ``__debug__``（Python 后端的引导包装与种子模块全局）。
#: 共用一个集合而非各自拒绝自身槽位，才能保证「在一个后端有效的命名空间列表在
#: 所有后端都有效」的可移植承诺。
RESERVED_BINDING_GLOBALS: frozenset[str] = frozenset([
    "console",
    "__dsh_main__", "__builtins__", "__name__", "__debug__",
])

#: 每个后端都拒绝的错误类成员名（``CodeBindingErrorClass.memberNameProperty``）。
ROOT_RESERVED_ERROR_MEMBERS: frozenset[str] = frozenset([
    "name", "message", "stack",
    "args", "with_traceback", "add_note",
])

#: dunder 形式（``__x__``，中间非空）：Python 的对象协议槽位，所有后端作为错误成员拒绝。
DUNDER_MEMBER = re.compile(r"^__.+__$")

#: 所有可移植目标语言（ECMAScript ∪ Python）的保留字；作为
#: ``CodeBindingNamespace.global`` / 错误类名被所有后端拒绝。即便只有 TypeScript
#: worker 有已发布后端，Python 仍是可移植目标之一。
PORTABLE_RESERVED_WORDS: frozenset[str] = frozenset([
    # ECMAScript 保留字 + strict 模式保留名
    "await", "break", "case", "catch", "class", "const", "continue", "debugger", "default",
    "delete", "do", "else", "enum", "export", "extends", "false", "finally", "for", "function",
    "if", "import", "in", "instanceof", "new", "null", "return", "super", "switch", "this",
    "throw", "true", "try", "typeof", "var", "void", "while", "with", "yield", "let", "static",
    "implements", "interface", "package", "private", "protected", "public", "arguments", "eval",
    # Python 3.x 关键字与软关键字
    "False", "None", "True", "and", "as", "assert", "async", "def", "del", "elif", "except",
    "from", "global", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "match", "type", "_",
])

#: 标识符规则：``[A-Za-z_][A-Za-z0-9_]*``。
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# --------------------------------------------------------------------------- #
# 类型词汇
# --------------------------------------------------------------------------- #

#: 可无损跨序列化边界传输的 JSON 值。
CodeJsonValue = Any

#: 主机侧暴露给程序的一个异步可调用绑定。
CodeBindingFunction = Callable[[Any], Any]  # (args) -> awaitable[CodeJsonValue]

#: 程序可见的类型化拒绝（按命名空间）。运行时在 ``name`` 下注入真实错误构造器；
#: 成员调用失败成为其实例，并通过 ``memberNameProperty`` 暴露确切成员名。
@dataclass(frozen=True)
class CodeBindingErrorClass:
    #: 构造器全局名与 ``Error.name``；与 ``CodeBindingNamespace.global`` 同一可移植标识符规则。
    name: str
    #: 成员名的非空自有属性。可移植排除集 = ``RESERVED_ERROR_MEMBERS`` ∪ dunder 形式。
    memberNameProperty: str


#: 一组绑定函数，运行时作为程序中一个全局对象暴露（如 ``tools``）。
@dataclass(frozen=True)
class CodeBindingNamespace:
    #: 程序可见的全局标识符，必须满足可移植标识符规则且不是保留全局。
    global_name: str
    #: 可调用成员，键为程序调用的确切名字。
    functions: dict[str, CodeBindingFunction]
    #: 可选的、程序可见的类型化拒绝契约。
    errorClass: CodeBindingErrorClass | None = None


#: 一次运行请求：程序源码 + 绑定 + 中止信号。
@dataclass
class CodeRunRequest:
    #: 运行时代码（Python 后端即 Python 源码），作为 async 函数体运行——顶级
    #: ``await`` / ``return`` 可用，完成值成为 ``CodeRunResult.value``。
    program: str
    #: 暴露给程序的绑定命名空间（每个一个全局对象）。
    bindings: list[CodeBindingNamespace]
    #: 中止信号；触发则运行时停止程序并以 ``kind='abort'`` 的失败解析。
    signal: Any | None = None


#: 失败的「为什么」——各类正交，分别报告（详见各 kind 文档）。
@dataclass
class CodeRunFailure:
    #: 失败类别。
    kind: str  # 'exception' | 'timeout' | '  abort' | 'worker-exit' | 'invalid-output' | 'output-limit'
    #: 人类可读细节，适合反馈给模型以自我纠正。
    message: str


#: 一次运行的结果。错误是解析结果的字段，而非 ``run()`` 的拒绝——报告失败程序是
#: 调用方的职责，不属于异常路径。
@dataclass
class CodeRunResult:
    #: 程序完成值（顶级 ``return``），仅在成功跨无损 JSON 边界时存在。
    value: CodeJsonValue | None = None
    #: 程序按序发出的文本（仅在作为外层结果的一部分受约束）。
    logs: list[str] = None  # type: ignore[assignment]
    #: 仅当运行失败时存在。
    error: CodeRunFailure | None = None

    def __post_init__(self) -> None:
        if self.logs is None:
            self.logs = []


# --------------------------------------------------------------------------- #
# 校验（每个后端共用，保证可移植契约）
# --------------------------------------------------------------------------- #

def validate_binding_namespace(ns: CodeBindingNamespace) -> None:
    """校验一个绑定命名空间的可移植合法性；非法则抛 ``ValueError``。"""
    if not IDENTIFIER.match(ns.global_name):
        raise ValueError(f"绑定全局名不是可移植标识符: {ns.global_name!r}")
    if ns.global_name in PORTABLE_RESERVED_WORDS:
        raise ValueError(f"绑定全局名是保留字: {ns.global_name!r}")
    if ns.global_name in RESERVED_BINDING_GLOBALS:
        raise ValueError(f"绑定全局名是后端拥有的保留槽位: {ns.global_name!r}")
    if ns.errorClass is not None:
        ec = ns.errorClass
        if not IDENTIFIER.match(ec.name):
            raise ValueError(f"错误类名不是可移植标识符: {ec.name!r}")
        if ec.name in PORTABLE_RESERVED_WORDS:
            raise ValueError(f"错误类名是保留字: {ec.name!r}")
        if ec.memberNameProperty in ROOT_RESERVED_ERROR_MEMBERS:
            raise ValueError(f"错误成员名在根保留集中: {ec.memberNameProperty!r}")
        if DUNDER_MEMBER.match(ec.memberNameProperty):
            raise ValueError(f"错误成员名是 dunder 形式: {ec.memberNameProperty!r}")
        if ec.memberNameProperty == "":
            raise ValueError("错误成员名不可为空")


def validate_run_request(request: CodeRunRequest) -> None:
    """校验整个运行请求；seam 契约误用在此抛出（而非作为结果字段）。"""
    for ns in request.bindings:
        validate_binding_namespace(ns)


# --------------------------------------------------------------------------- #
# Seam
# --------------------------------------------------------------------------- #

class CodeRuntime(Service):
    """代码执行能力抽象服务（``ctx.codeRuntime``）。

    实现者须：桥接结构化可克隆的绑定、物化每个声明的命名空间拒绝类、把程序当作
    敌意对等方、把各次运行彼此隔离、并在 dispose 时终止并等待在途运行。

    程序、预算、中止、底层失败都解析进 :class:`CodeRunResult`；仅 Service Definition
    契约误用才会令 ``run()`` 拒绝。
    """

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "codeRuntime")

    @property
    @abstractmethod
    def language(self) -> str:
        """运行时代码期望的源码语言（小写标识）。已知值：``'typescript'`` / ``'python'``。"""
        raise NotImplementedError

    @property
    @abstractmethod
    def isolation(self) -> str:
        """执行底层（小写标识）。已知值：``'worker-thread'`` / ``'process'`` / ``'container'``。"""
        raise NotImplementedError

    @abstractmethod
    async def run(self, request: CodeRunRequest) -> CodeRunResult:
        """执行一个程序并捕获其输出。

        :param request: 程序、绑定、中止信号——请求携带运行所需的一切，无隐藏默认值。
        :returns: 运行结果：完成值（可传输时）、按序日志、以及（若有）失败。
        """
        raise NotImplementedError


__all__ = [
    "RESERVED_BINDING_GLOBALS",
    "ROOT_RESERVED_ERROR_MEMBERS",
    "DUNDER_MEMBER",
    "PORTABLE_RESERVED_WORDS",
    "IDENTIFIER",
    "CodeJsonValue",
    "CodeBindingFunction",
    "CodeBindingErrorClass",
    "CodeBindingNamespace",
    "CodeRunRequest",
    "CodeRunFailure",
    "CodeRunResult",
    "validate_binding_namespace",
    "validate_run_request",
    "CodeRuntime",
]

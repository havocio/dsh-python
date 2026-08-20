"""typert 协议层（对标 dsh 的 ``@deepseek-ai/dsh-typert``，protocol + registry 核心）。

声明式远程调用：业务类/方法用装饰器标记为可远程暴露，注册表收集端点，
客户端经代理对象（或 JSON-RPC 网关）调用。dsh 用 TS 编译器（generator）生成
绑定与 schema；本实现用**运行时反射**替代代码生成——装饰器在方法/类上写入
元数据，注册时扫描收集。

- :func:`remote` —— 标记一个方法可远程调用（可指定 wire 方法名）；
- :func:`remote_scope` —— 标记一个类为远程作用域（可指定 wire 作用域名）；
- :class:`TypertRegistry` —— ``ctx.typertRegistry``：register（扫描 + 校验）、
  ``invoke``（分派，包装为 :class:`RemoteResult`）、``list``、``client_for``
  （动态代理对象）；
- :class:`RemoteResult` / :class:`RemoteFailure` / :class:`InvocationDescriptor`
  —— 调用结果 / 失败 / 描述契约（对齐 dsh-typert-protocol）。

invoke 支持两种接收者：``direct``（注册表内对象的方法）与 ``context``（从
上下文的命名服务解析接收者，wire 名映射到服务）。
"""

from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service

# --------------------------------------------------------------------------- #
# 装饰器
# --------------------------------------------------------------------------- #
_REMOTE_ATTR = "_typert_remote"
_WIRE_ATTR = "_typert_wire"
_SCOPE_ATTR = "_typert_scope"


def remote(method: Optional[str] = None) -> Callable:
    """标记一个方法可远程调用。

    :param method: wire 方法名（缺省用原方法名；``@remote("别名")`` 指定）。
    """
    wire = method

    def decorator(fn: Callable) -> Callable:
        setattr(fn, _REMOTE_ATTR, True)
        setattr(fn, _WIRE_ATTR, wire or fn.__name__)
        return fn

    return decorator


def remote_scope(name: Optional[str] = None) -> Callable:
    """标记一个类为远程作用域。

    :param name: wire 作用域名（缺省用类名；``@remote_scope("别名")`` 指定）。
    """

    def decorator(cls: type) -> type:
        setattr(cls, _SCOPE_ATTR, name or cls.__name__)
        return cls

    return decorator


def _remote_scope_name(cls: type) -> str:
    return getattr(cls, _SCOPE_ATTR, None) or cls.__name__


# --------------------------------------------------------------------------- #
# 契约
# --------------------------------------------------------------------------- #
@dataclass
class RemoteFailure:
    """一次远程调用的稳定失败（code 分类 + 消息）。"""

    code: str
    message: str


@dataclass
class RemoteResult:
    """远程调用结果包装（对齐 dsh 的 ``RemoteResult`` 判别联合）。"""

    ok: bool
    value: Any = None
    error: Optional[RemoteFailure] = None

    @staticmethod
    def success(value: Any) -> "RemoteResult":
        return RemoteResult(ok=True, value=value)

    @staticmethod
    def failure(code: str, message: str) -> "RemoteResult":
        return RemoteResult(ok=False, error=RemoteFailure(code=code, message=message))


@dataclass
class InvocationDescriptor:
    """一次远程调用的完整描述（对齐 dsh 的 ``InvocationDescriptor``）。"""

    id: str                            # 全局稳定身份
    service: str                       # 拥有方法的服务/作用域键
    method: str                        # wire 方法名
    namespace: str = ""                # wire 命名空间（缺省同 service）
    implementation: Optional[str] = None  # 别名：实际调用的成员名
    invocation: dict = field(default_factory=lambda: {"kind": "direct"})  # direct | context
    args: dict = field(default_factory=dict)  # 命名的参数对象


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
class TypertRegistry(Service):
    """``typertRegistry`` 服务：远程端点注册表与调用分派（``ctx.typertRegistry``）。

    注册一个对象时扫描其 ``@remote`` 方法并校验端点名（空名拒绝）；同作用域
    重复注册覆盖。invoke 永不向上抛——预期失败（未注册作用域/方法、接收者
    解析失败、handler 异常）包装为 :class:`RemoteResult` 返回。
    """

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "typertRegistry")
        self._scopes: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    # 注册
    # ------------------------------------------------------------------ #
    def register(self, scope: Optional[str] = None, obj: Any = None) -> Callable[[], None]:
        """注册一个远程对象（扫描 ``@remote`` 方法）；返回精确注销函数。

        :param scope: wire 作用域名；缺省从 ``@remote_scope`` 类装饰器读取。
        """
        resolved_scope = scope or _remote_scope_name(type(obj))
        if not resolved_scope:
            raise ValueError("typert: 远程作用域名不能为空（用 @remote_scope 或传 scope 参数）")
        methods: dict[str, Callable] = {}
        for name in dir(obj):
            attr = getattr(obj, name, None)
            if callable(attr) and getattr(attr, _REMOTE_ATTR, False):
                wire = getattr(attr, _WIRE_ATTR, None) or name
                if not wire:
                    raise ValueError(f"typert: 方法 {name!r} 的 wire 名不能为空")
                methods[wire] = attr
        if not methods:
            raise ValueError(
                f"typert: 对象 {type(obj).__name__!r} 没有 @remote 方法；"
                "请用 @remote 装饰至少一个方法"
            )
        self._scopes[resolved_scope] = {"object": obj, "methods": methods}

        def dispose() -> None:
            self._scopes.pop(resolved_scope, None)

        return dispose

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def has_scope(self, scope: str) -> bool:
        return scope in self._scopes

    def list(self) -> list[dict]:
        """列出全部已注册端点（作用域名 + wire 方法名）。"""
        return [
            {"service": scope, "methods": sorted(entry["methods"].keys())}
            for scope, entry in self._scopes.items()
        ]

    # ------------------------------------------------------------------ #
    # 调用
    # ------------------------------------------------------------------ #
    async def invoke(self, descriptor: InvocationDescriptor) -> RemoteResult:
        """分派一次远程调用；预期失败包装为 RemoteResult（不抛）。

        ``invocation.kind == "direct"`` 调注册对象的方法；
        ``"context"`` 仍要求作用域与方法已注册（端点校验），但接收者从
        上下文的命名服务解析（``wire 名 → ctx.<context>.<method>``）。
        """
        try:
            entry = self._scopes.get(descriptor.service)
            if entry is None:
                return RemoteResult.failure(
                    "SCOPE_NOT_FOUND", f"远程作用域 {descriptor.service!r} 未注册")
            wire = descriptor.method
            fn = entry["methods"].get(wire)
            if fn is None:
                return RemoteResult.failure(
                    "METHOD_NOT_FOUND", f"作用域 {descriptor.service!r} 无方法 {wire!r}")
            if descriptor.invocation.get("kind") == "context":
                # 接收者从上下文的命名服务解析（context → ctx.<service>）
                context = descriptor.invocation.get("context", "")
                receiver = getattr(self.ctx, context, None)
                if receiver is None:
                    return RemoteResult.failure(
                        "CONTEXT_NOT_FOUND", f"上下文中无服务 {context!r}")
                result = getattr(receiver, wire)(**descriptor.args)
            else:
                result = fn(**descriptor.args)
            if inspect.isawaitable(result):
                result = await result
            return RemoteResult.success(result)
        except Exception as exc:  # noqa: BLE001 - 远程调用失败作为结果返回
            return RemoteResult.failure("FAILED", f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ #
    # 客户端代理
    # ------------------------------------------------------------------ #
    def client_for(self, namespace: str) -> Any:
        """返回一个命名空间的客户端代理：``proxy.method(**args)`` → 远程调用。

        调用返回 :class:`RemoteResult`（调用方检查 ``ok``）。方法不存在于
        注册表时，invoke 返回 ``METHOD_NOT_FOUND`` 失败。
        """

        class RemoteProxy:
            def __getattr__(self, name: str) -> Callable:
                if name.startswith("_"):
                    raise AttributeError(name)

                async def caller(**args: Any) -> RemoteResult:
                    descriptor = InvocationDescriptor(
                        id=uuid.uuid4().hex,
                        service=namespace,
                        namespace=namespace,
                        method=name,
                    )
                    descriptor.args = args
                    return await TypertRegistry.invoke(self_registry, descriptor)

                return caller

        self_registry = self

        return RemoteProxy()


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``typertRegistry`` 服务（远程端点注册表）。"""
    TypertRegistry(ctx)


apply.provides = ["typertRegistry"]  # 声明：本插件提供 typertRegistry 服务

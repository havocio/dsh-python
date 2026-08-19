"""应用上下文：绑定事件总线的服务容器 + 插件生命周期 + 作用域树。

``AppContext`` 通过属性访问解析命名服务（如 ``ctx.llm``），把事件方法委托给
内部的 :class:`EventBus`，并负责加载插件。插件是任意 ``apply(ctx, config)``
可调用对象，可用 ``inject`` 声明它依赖的服务名列表，上下文会在运行前校验这些
依赖是否已就绪。

**生命周期（对标 cordis 的 fiber）**：上下文持有一个 :class:`Fiber`，
``ctx.plugin()`` 为每个插件创建一个子 Fiber——插件注册的监听器 / 服务 / effect
都会记录到该 Fiber 上，``dispose()`` 时逆序回收（监听器注销、服务移除）。

**作用域树（对标 cordis 的 extend / isolate / intercept）**：上下文是一个树节点，
``extend()`` 创建子上下文；``isolate(name, label)`` 创建把服务 ``name`` 隔离开的
子上下文（子作用域可提供独立实现，父作用域不受影响）；``intercept(name, config)``
创建对服务 ``name`` 注入配置覆盖的子上下文。服务解析沿「当前 → 父」链向上查找，
遇到隔离边界即停止；事件分派沿「当前 + 祖先链」收集监听器（父作用域监听器可见
子作用域事件）。这正是多会话 / 多作用域工作空间隔离的地基。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from dsh_py.core.events import EventBus
from dsh_py.core.fiber import Fiber
from dsh_py.core.service import Service

# 插件可调用对象类型
PluginFn = Callable[..., Any]


class PluginHandle:
    """``ctx.plugin()`` 返回的可卸载句柄（对标 cordis 的 Fiber）。

    :attr fiber: 该插件的生命周期节点。
    :meth dispose: 卸载插件，逆序回收其注册的全部资源。
    """

    def __init__(self, fiber: Fiber) -> None:
        self.fiber = fiber

    def dispose(self) -> None:
        """卸载该插件（幂等）。"""
        self.fiber.dispose()


class AppContext:
    def __init__(
        self,
        parent: Optional["AppContext"] = None,
        name: str = "root",
    ) -> None:
        # 作用域树
        self._parent = parent
        self._name = name
        # 本作用域注册的服务（键名 -> 实例）
        self._services: dict[str, Any] = {}
        # 隔离表：服务名 -> 作用域标签（原型继承语义：dict 拷贝自父）
        self._isolate: dict[str, Any] = {}
        # 拦截表：服务名 -> 注入的配置覆盖
        self._intercept: dict[str, Any] = {}
        # 事件总线全局共享（对齐 cordis 的单一 EventsService）：
        # 可见性由监听器记录中的归属 scope 决定，而非各自独立的钩子表
        self.events = parent.events if parent is not None else EventBus()

        # 生命周期节点：root 一经创建即视为可用；子上下文挂到父 fiber 之下
        self._fiber = Fiber(parent=parent._fiber if parent else None, name=name)
        self._fiber.start()
        # 「当前激活 fiber」栈：ctx.plugin 加载期间压入子 fiber，
        # 使 ctx.on / ctx.provide / ctx.effect 记录到正确的生命周期节点
        self._fiber_stack: list[Fiber] = [self._fiber]

    # ------------------------------------------------------------------ #
    # 生命周期（Fiber）
    # ------------------------------------------------------------------ #
    @property
    def fiber(self) -> Fiber:
        """当前激活的生命周期节点（插件加载期间为插件 fiber，否则为本上下文 fiber）。"""
        return self._fiber_stack[-1]

    def effect(self, disposer: Callable[[], Any], label: str = "") -> Callable[[], bool]:
        """注册一个资源清理函数，当前 fiber 卸载时逆序执行。"""
        return self.fiber.effect(disposer, label=label)

    def dispose(self) -> None:
        """卸载本上下文：回收本作用域注册的全部资源（监听器 / 服务 / effect）。"""
        self._fiber.dispose()
        self._services.clear()

    # ------------------------------------------------------------------ #
    # 作用域树（对标 cordis 的 extend / isolate / intercept）
    # ------------------------------------------------------------------ #
    def _spawn(self, name: str) -> "AppContext":
        """创建一个以当前上下文为父的子上下文。"""
        return AppContext(parent=self, name=name)

    def extend(self, name: str = "child") -> "AppContext":
        """创建子上下文（继承父的服务空间与隔离/拦截表，父不被修改）。"""
        return self._spawn(name)

    def isolate(self, name: str, label: Any = None, ctx_name: str = "isolated") -> "AppContext":
        """创建把服务 ``name`` 隔离开的子上下文。

        子作用域内对 ``name`` 的读写解析到新标签（继承父的隔离表并覆盖
        ``name``），可提供独立实现而不影响父作用域；相同 ``label`` 的隔离
        调用共享作用域。对标 cordis 的 ``ctx.isolate(name, label)``。
        """
        child = self._spawn(ctx_name)
        child._isolate = dict(self._isolate)
        child._isolate[name] = label if label is not None else object()
        return child

    def intercept(self, name: str, config: Any, ctx_name: str = "intercepted") -> "AppContext":
        """创建对服务 ``name`` 注入配置覆盖的子上下文。

        在子上下文之下加载的服务 ``name`` 会合并该配置（祖先条目在前）。
        对标 cordis 的 ``ctx.intercept(name, config)``。
        """
        child = self._spawn(ctx_name)
        child._intercept = dict(self._intercept)
        child._intercept[name] = config
        return child

    # ------------------------------------------------------------------ #
    # 服务注册与解析
    # ------------------------------------------------------------------ #
    def provide(self, name: str, instance: Any) -> None:
        """以 ``name`` 注册 ``instance`` 到**当前作用域**（重名则静默覆盖）。

        注册会记录到当前 fiber：插件卸载时，若该服务仍是此实例则一并移除。
        """
        self._services[name] = instance

        def _unprovide() -> None:
            if self._services.get(name) is instance:
                del self._services[name]

        self.fiber.effect(_unprovide, label=f"provide({name})")

    def has_service(self, name: str) -> bool:
        """判断某个命名服务在当前作用域树内是否可解析。"""
        try:
            self._resolve_service(name)
            return True
        except AttributeError:
            return False

    def _resolve_service(self, name: str) -> Any:
        """沿「当前 → 父」链解析服务；跨隔离边界（label 变化）即停止。"""
        node: Optional[AppContext] = self
        key = self._isolate.get(name)  # 当前作用域对 name 的标签（继承父的语义）
        while node is not None:
            if name in node._services:
                return node._services[name]
            if node._parent is not None:
                # 父级对该服务的标签与当前不同 → 已跨越隔离边界，停止向上
                if node._parent._isolate.get(name) is not key:
                    break
            node = node._parent
        raise AttributeError(f"上下文没有名为 {name!r} 的服务或属性")

    def __getattr__(self, name: str) -> Any:
        # 仅当常规属性查找失败时才会走到这里
        if name.startswith("_"):
            raise AttributeError(f"上下文没有属性 {name!r}")
        return self._resolve_service(name)

    # ------------------------------------------------------------------ #
    # 事件委托（分派携带本上下文，父作用域监听器可见子作用域事件）
    # ------------------------------------------------------------------ #
    def _register_listener(self, name: str, listener: Any, **opts: Any) -> Callable[[], bool]:
        """注册监听器，归属本上下文，并把注销句柄挂到当前 fiber。"""
        opts.setdefault("scope", self)
        dispose = self.events.on(name, listener, **opts)
        self.fiber.effect(dispose, label=f"on({name})")
        return dispose

    def on(self, name: str, listener: Any = None, **opts: Any) -> Any:
        """注册事件监听器；也可作装饰器使用（``@ctx.on('ev')``）。"""
        if listener is None:
            def decorator(fn: Any) -> Any:
                self._register_listener(name, fn, **opts)
                return fn
            return decorator
        return self._register_listener(name, listener, **opts)

    def once(self, name: str, listener: Any = None, **opts: Any) -> Any:
        """注册一次性事件监听器；也可作装饰器使用。"""
        if listener is None:
            def decorator(fn: Any) -> Any:
                self._register_once(name, fn, **opts)
                return fn
            return decorator
        return self._register_once(name, listener, **opts)

    def _register_once(self, name: str, listener: Any, **opts: Any) -> Callable[[], bool]:
        """注册一次性监听器，并把注销句柄挂到当前 fiber。"""
        opts.setdefault("scope", self)
        dispose = self.events.once(name, listener, **opts)
        self.fiber.effect(dispose, label=f"once({name})")
        return dispose

    def emit(self, name: str, *args: Any) -> None:
        return self.events.emit(name, *args, scope=self)

    async def parallel(self, name: str, *args: Any) -> None:
        return await self.events.parallel(name, *args, scope=self)

    async def serial(self, name: str, *args: Any) -> Any:
        return await self.events.serial(name, *args, scope=self)

    def bail(self, name: str, *args: Any) -> Any:
        return self.events.bail(name, *args, scope=self)

    def waterfall(self, name: str, *args: Any, inner: Optional[Callable[..., Any]] = None) -> Any:
        return self.events.waterfall(name, *args, inner=inner, scope=self)

    # ------------------------------------------------------------------ #
    # 插件加载
    # ------------------------------------------------------------------ #
    def plugin(
        self,
        apply_fn: PluginFn,
        config: Any = None,
        *,
        name: Optional[str] = None,
        inject: Optional[list[str]] = None,
    ) -> PluginHandle:
        """加载一个插件：schema 校验配置 → 校验 inject 依赖 → 调用 ``apply``。

        对标 cordis 的 ``ctx.plugin()``：
        - 若 ``apply_fn`` 带 ``Config``（:class:`dsh_py.core.schema.Schema`），
          先校验并填充默认值，再传给 ``apply``（配置写错启动期即报错）；
        - ``inject`` 未显式传入时，读取 ``apply_fn.inject`` 属性；
        - 为插件创建子 :class:`Fiber`（插件期间注册的资源记录其上）；
        - 返回 :class:`PluginHandle`，可随时 ``dispose()`` 卸载插件并回收资源。
        依赖缺失会在插件主体执行前就抛错。
        """
        # 配置 schema 校验（对标 schemastery）
        schema = getattr(apply_fn, "Config", None)
        if schema is not None:
            config = schema.validate(config)

        deps = list(inject if inject is not None else getattr(apply_fn, "inject", None) or [])
        missing = [dep for dep in deps if not self.has_service(dep)]
        if missing:
            label = name or getattr(apply_fn, "__name__", "plugin")
            raise RuntimeError(
                f"插件 {label!r} 依赖了尚未可用的服务：{missing}"
            )
        fiber = Fiber(
            parent=self._fiber_stack[-1],
            name=name or getattr(apply_fn, "__name__", "plugin"),
        )
        fiber.start()
        self._fiber_stack.append(fiber)
        try:
            apply_fn(self, config)
        finally:
            self._fiber_stack.pop()
        return PluginHandle(fiber)
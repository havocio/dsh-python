"""运行时不变式注册表（runtime-diagnostics/invariants，第 3 层）。

每个工作区包从自己的 ``invariant`` companion 注册检查；普通包入口与诊断
无关。注册表提供全局开关与包名正则选择（allowlist / blocklist）。

- :func:`register` —— 保留包名名额（即使过滤禁用也保留）；选中的安装器在
  子 fiber 中运行（可访问注入的服务），收到绑定包名的 ``fail`` 报告器
  （报告即抛 :class:`InvariantError`）；安装失败释放名额并传播。
- 配置：``enabled``（默认 true）、``package_allowlist`` / ``package_blocklist``
  （区分大小写正则源列表，编译期校验：非空、无周边空白、无重复、正则合法）。

**与 dsh 的差异（已注明）**：dsh 的 ``register`` 返回可 await 的 disposer
（内部 join 子安装完成）；dsh_py 的 :meth:`InvariantRegistry.register` 同步
返回 disposer——安装器若返回协程，会在当前事件循环调度执行（无运行循环时用
``asyncio.run``），其异常经 done 回调记录而不崩进程；同步抛错（含 ``fail``
报告）立即失败并释放名额。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.core.service import Service

logger = logging.getLogger("dsh_py.invariants")

# 一个不变式失败的稳定机器码（对齐 dsh 的 ``code = 'INVARIANT'``）
INVARIANT_CODE = "INVARIANT"


class InvariantError(Exception):
    """包拥有的运行时不变式被违反。"""

    code = INVARIANT_CODE

    def __init__(self, package_name: str, message: str) -> None:
        super().__init__(f'invariant violated by "{package_name}": {message}')
        self.packageName = package_name


# 安装器：在子上下文安装检查；``fail`` 报告绑定包名的不变式违反（抛错）。
# 返回 None 或协程（异步检查结束后 settle）。
InvariantInstaller = Callable[[AppContext, Callable[[str], None]], Optional[Awaitable[None]]]


def _compile_patterns(field: str, values: list[str]) -> list[re.Pattern]:
    """编译并校验一个包名过滤列表（非空/无周边空白/无重复/正则合法）。"""
    seen: set[str] = set()
    compiled: list[re.Pattern] = []
    for value in values:
        if value == "" or value.strip() != value:
            raise ValueError(f"invariants: {field} 条目必须非空且无周边空白")
        if value in seen:
            raise ValueError(f"invariants: {field} 含重复正则 {value!r}")
        seen.add(value)
        try:
            compiled.append(re.compile(value))
        except re.error as exc:
            raise ValueError(f"invariants: {field} 含非法正则 {value!r}") from exc
    return compiled


def _schedule_coroutine(coro: Awaitable[None]) -> None:
    """把安装器协程调度到当前事件循环；无运行循环时 ``asyncio.run``。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return

    task = loop.create_task(coro)

    def _on_done(t: Any) -> None:
        if t.cancelled():
            return
        error = t.exception()
        if error is not None:
            logger.error("invariants: 异步安装器失败：%r", error)

    task.add_done_callback(_on_done)


Config = z.object({
    "enabled": z.boolean().default(True),
    "package_allowlist": z.array(z.string()).default([]),
    "package_blocklist": z.array(z.string()).default([]),
})


class InvariantRegistry(Service):
    """包拥有的运行时不变式注册表（``ctx.invariants``）。"""

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx, "invariants")
        cfg = config or {}
        self._enabled = bool(cfg.get("enabled", True))
        self._allowlist = _compile_patterns("package_allowlist", cfg.get("package_allowlist", []) or [])
        self._blocklist = _compile_patterns("package_blocklist", cfg.get("package_blocklist", []) or [])
        self._registrations: set[str] = set()

    # ------------------------------------------------------------------ #
    # 选择与注册
    # ------------------------------------------------------------------ #
    def _selected(self, package_name: str) -> bool:
        """一个完整包名是否通过配置的过滤器。"""
        if not self._enabled:
            return False
        if self._allowlist and not any(p.search(package_name) for p in self._allowlist):
            return False
        return not any(p.search(package_name) for p in self._blocklist)

    def register(self, package_name: str, installer: InvariantInstaller) -> Callable[[], None]:
        """注册一个包的安装器：保留名额（即使过滤禁用）；选中时安装到子 fiber。

        :param package_name: 拥有该贡献的完整包名（非空、无空白、唯一）。
        :param installer: 子上下文安装器（可携带 ``.inject`` 服务依赖声明）。
        :returns: 作用域化 disposer（卸载子 fiber 并释放名额）。
        """
        if (package_name == "" or package_name.strip() != package_name
                or re.search(r"\s", package_name) is not None):
            raise ValueError("invariants: packageName 必须非空且不含空白")
        if package_name in self._registrations:
            raise ValueError(f'invariants: 包 {package_name!r} 已注册')
        self._registrations.add(package_name)

        def fail(message: str) -> None:
            raise InvariantError(package_name, message)

        if not self._selected(package_name):
            def filtered_disposer() -> None:
                self._registrations.discard(package_name)
            self.ctx.effect(filtered_disposer, label=f"invariants.register({package_name!r})")
            return filtered_disposer

        def install_wrapper(child_ctx: AppContext, _config: Any = None) -> None:
            result = installer(child_ctx, fail)
            if inspect.isawaitable(result):
                _schedule_coroutine(result)

        try:
            handle = self.ctx.plugin(
                install_wrapper,
                inject=list(getattr(installer, "inject", None) or []),
            )
        except Exception:
            self._registrations.discard(package_name)
            raise

        def disposer() -> None:
            self._registrations.discard(package_name)
            handle.dispose()

        self.ctx.effect(disposer, label=f"invariants.register({package_name!r})")
        return disposer

    def list(self) -> list[str]:
        """列出当前已注册（含被过滤的）包名。"""
        return sorted(self._registrations)


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：注册 ``ctx.invariants`` 服务。"""
    InvariantRegistry(ctx, config or {})


apply.Config = Config
apply.name = "invariants"
apply.provides = ["invariants"]

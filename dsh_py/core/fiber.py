"""插件生命周期（Fiber，对标 cordis 的 ``fiber.ts``）。

每个插件实例对应一个 :class:`Fiber`：它承载该插件注册的全部资源（事件监听器、
服务、用户自定义 effect），并在卸载时**逆序回收**。状态机：

    PENDING → ACTIVE → UNLOADING → DISPOSED

- ``PENDING``：已创建未启动，禁止注册资源。
- ``ACTIVE``：运行中，可注册资源（监听器 / 服务 / effect）。
- ``UNLOADING``：正在逆序执行清理函数。
- ``DISPOSED``：清理完成，不可再用（dispose 幂等）。

这是「一切皆插件」的生命周期地基：插件可卸载、资源可自动回收，
后续 Loader 的卸载与热重载、Scope 的级联清理都建立在这之上。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Optional

# 资源清理函数：卸载时被调用，回收该插件注册的资源
Disposer = Callable[[], Any]


class FiberState(str, Enum):
    """Fiber 的生命周期状态。"""
    PENDING = "pending"      # 已创建，尚未启动
    ACTIVE = "active"        # 运行中，可注册资源
    UNLOADING = "unloading"  # 正在执行清理
    DISPOSED = "disposed"    # 清理完成，不可再用


class Fiber:
    """插件 / 作用域的生命周期节点。"""

    def __init__(
        self,
        parent: Optional["Fiber"] = None,
        name: str = "root",
        uid: int = 0,
    ) -> None:
        self.parent = parent
        self.name = name
        self.uid = uid
        self.state = FiberState.PENDING
        # (标签, 清理函数)，按注册顺序保存；卸载时逆序执行
        self._effects: list[tuple[str, Disposer]] = []
        # 子 fiber（父 fiber 卸载时先卸载子级）
        self._children: list["Fiber"] = []

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def assert_active(self) -> None:
        """运行期守卫：非 ``ACTIVE`` 状态下禁止再注册资源。"""
        if self.state != FiberState.ACTIVE:
            raise RuntimeError(
                f"fiber {self.name!r} 当前状态 {self.state.value!r}，不可注册资源"
            )

    def start(self) -> None:
        """进入 ``ACTIVE`` 状态，并挂到父 fiber 的 children。"""
        self.state = FiberState.ACTIVE
        if self.parent is not None:
            self.parent._children.append(self)

    # ------------------------------------------------------------------ #
    # effect（资源注册）
    # ------------------------------------------------------------------ #
    def effect(self, disposer: Disposer, label: str = "") -> Callable[[], bool]:
        """注册一个资源清理函数，卸载时逆序执行；返回可单独取消注册的句柄。

        :param disposer: 无参可调用，卸载时被调用以回收资源。
        :param label: 诊断标签（如 ``on(event)`` / ``provide(name)``）。
        :returns: ``cancel()`` —— 提前取消这条 effect 的注册，成功返回 True。
        """
        self.assert_active()
        entry = (label, disposer)
        self._effects.append(entry)

        def cancel() -> bool:
            if entry in self._effects:
                self._effects.remove(entry)
                return True
            return False

        return cancel

    # ------------------------------------------------------------------ #
    # 卸载
    # ------------------------------------------------------------------ #
    def dispose(self) -> None:
        """卸载：先卸载子 fiber，再逆序执行本 fiber 的 effect，进入 ``DISPOSED``。

        幂等：重复调用 / 已处于 UNLOADING 或 DISPOSED 时直接返回。
        单个清理函数抛错不阻断其余清理（错误被吞掉并继续）。
        """
        if self.state in (FiberState.UNLOADING, FiberState.DISPOSED):
            return
        self.state = FiberState.UNLOADING
        # 子 fiber 先卸载（后注册的资源先回收）
        for child in list(self._children):
            child.dispose()
        self._children.clear()
        for _label, disposer in reversed(self._effects):
            try:
                disposer()
            except Exception:  # noqa: BLE001 - 单个清理失败不阻断整体卸载
                continue
        self._effects.clear()
        self.state = FiberState.DISPOSED
        if self.parent is not None and self in self.parent._children:
            self.parent._children.remove(self)

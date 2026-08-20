"""运行时内省服务（对标 cordis 的 ``ReflectService``）。

提供对当前上下文已注册服务、事件、作用域树的内省能力，便于调试、编排与
可视化。所有方法均为只读快照，不修改任何状态。
"""
from __future__ import annotations

from typing import Any

from dsh_py.core.service import Service


class ReflectService(Service):
    """上下文内省服务（``name='reflect'``）。"""

    def __init__(self, ctx: Any, name: str = "reflect") -> None:
        super().__init__(ctx, name)
        self._ctx = ctx

    def services(self) -> list[str]:
        """列出本作用域直接注册的服务名（已排序）。"""
        return sorted(self._ctx._services.keys())

    def events(self) -> list[str]:
        """列出事件总线上已注册（曾 ``on``）的事件名（已排序）。"""
        return sorted(self._ctx.events._hooks.keys())

    def listeners(self, name: str) -> int:
        """某事件名下的监听器数量。"""
        return len(self._ctx.events._hooks.get(name, []))

    def scopes(self) -> list[str]:
        """收集当前作用域树的所有节点名（自身 → 祖先）。"""
        out: list[str] = []
        node: Any = self._ctx
        while node is not None:
            out.append(node._name)
            node = node._parent
        return out

    def snapshot(self) -> dict[str, Any]:
        """一次性返回服务 / 事件 / 作用域的内省快照。"""
        return {
            "services": self.services(),
            "events": self.events(),
            "scopes": self.scopes(),
        }

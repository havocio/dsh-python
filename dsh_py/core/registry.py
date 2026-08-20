"""组件注册表服务（对标 cordis 的 ``RegistryService``）。

提供跨服务的统一组件目录：按 ``(category, id)`` 登记 agent / tool / adapter
等组件，并广播 ``registry/updated`` 事件，供 :class:`ReflectService` 等内省
与运行时编排。例如各 seam 可把自身能力登记到注册表，形成全局「服务目录」。
"""
from __future__ import annotations

from typing import Any

from dsh_py.core.service import Service


class RegistryService(Service):
    """组件注册表服务（``name='registry'``）。"""

    def __init__(self, ctx: Any, name: str = "registry") -> None:
        super().__init__(ctx, name)
        self._ctx = ctx
        # (category, id) -> component
        self._entries: dict[tuple[str, str], Any] = {}

    def register(self, category: str, id: str, component: Any) -> Any:
        """登记一个组件（覆盖同名同类别），并广播 ``registry/updated``。"""
        self._entries[(category, id)] = component
        try:
            self._ctx.emit("registry/updated", category, id, "register")
        except Exception:
            pass
        return component

    def get(self, category: str, id: str, default: Any = None) -> Any:
        """按类别 + id 取组件；不存在返回 ``default``。"""
        return self._entries.get((category, id), default)

    def list(self, category: str | None = None) -> list[str]:
        """列出类别（``category=None`` 时）；或某类别下的全部 id。"""
        if category is None:
            return sorted({c for (c, _id) in self._entries})
        return sorted([_id for (c, _id) in self._entries if c == category])

    def all(self) -> dict[str, Any]:
        """返回 ``"category:id" -> component`` 的完整目录。"""
        return {f"{c}:{_id}": comp for (c, _id), comp in self._entries.items()}

    def remove(self, category: str, id: str) -> bool:
        """移除一个组件；成功返回 True，不存在返回 False。"""
        key = (category, id)
        if key in self._entries:
            del self._entries[key]
            try:
                self._ctx.emit("registry/updated", category, id, "remove")
            except Exception:
                pass
            return True
        return False

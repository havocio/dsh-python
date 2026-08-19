"""取消信号（对标 dsh 使用的 ``AbortSignal`` 语义的轻量实现）。

Python 无内建 AbortSignal；这里提供语义等价物：
- ``abort(reason)`` 触发取消，携带原因（dsh 的 ``AgentCancelCause`` 等价物）；
- ``throw_if_aborted()`` 在异步代码的检查点抛 :class:`SignalCancelledError`；
- 监听器（callback）在取消时被同步调用（对标 ``addEventListener('abort')``）。

Agent 主循环、LLM 适配器都可持有同一个信号对象，实现 dsh 的三源取消融合
（调用方 cancel / 生命周期卸载 / 工厂 teardown）中的前两种。
"""

from __future__ import annotations

from typing import Any, Callable, Optional


class SignalCancelledError(Exception):
    """操作被取消时在检查点抛出。携带取消原因。"""

    def __init__(self, reason: Any = None) -> None:
        super().__init__(reason)
        self.reason = reason


class CancelSignal:
    """一次可取消操作的信号。"""

    def __init__(self) -> None:
        self._aborted = False
        self._reason: Any = None
        self._listeners: list[Callable[[], None]] = []

    # -- 状态 ---------------------------------------------------------------- #
    @property
    def aborted(self) -> bool:
        """是否已被取消。"""
        return self._aborted

    @property
    def reason(self) -> Any:
        """取消原因（未取消时为 None）。"""
        return self._reason

    # -- 取消 ---------------------------------------------------------------- #
    def abort(self, reason: Any = None) -> None:
        """触发取消（幂等）：记录原因并同步通知监听器。"""
        if self._aborted:
            return
        self._aborted = True
        self._reason = reason
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 - 单个监听器失败不影响取消
                continue

    def throw_if_aborted(self) -> None:
        """检查点：若已取消则抛 :class:`SignalCancelledError`。"""
        if self._aborted:
            raise SignalCancelledError(self._reason)

    # -- 监听 ---------------------------------------------------------------- #
    def add_listener(self, callback: Callable[[], None]) -> Callable[[], bool]:
        """注册取消监听器；返回可取消注册的句柄。"""
        self._listeners.append(callback)

        def remove() -> bool:
            if callback in self._listeners:
                self._listeners.remove(callback)
                return True
            return False

        return remove

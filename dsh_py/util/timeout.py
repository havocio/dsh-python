"""共享超时原语（util/timeout，对标 dsh 的 ``@deepseek-ai/dsh-timeout``）。

截止时间算术、信号融合与超时分类。库只通过取消信号通知；各能力仍拥有
停止自身工作的机制，并把超时原因翻译成面向调用方的公开结果。

- :class:`TimeoutReason` —— 携带能力所属 ``code`` 与已过截止的 ``timeout_ms``；
- :func:`clamp_timeout` —— 校验调用方可选提示、应用后端默认值并封顶；
- :func:`deadline` —— 融合上游取消与可识别超时的 Deadline（信号 + 清理）；
- :func:`idle_watchdog` —— 围绕一次在途 async-iterator 需求的重新武装看门狗；
- :func:`timeout_of` —— 从信号/携带 reason 的对象恢复匹配的超时原因。

Python 映射（已注明）：dsh 用 ``AbortSignal``，dsh_py 用
:class:`dsh_py.core.signal.CancelSignal`（``reason`` 语义一致）；计时器用
事件循环的 ``call_later``（须在运行中的事件循环内创建）。
"""

from __future__ import annotations

import asyncio
import math
from typing import Any, Optional

from dsh_py.core.signal import CancelSignal

# 对齐 dsh 的 MAX_TIMER_DELAY_MS（Node 调度上限；Python 的 call_later 无此
# 限制，保留校验以对齐「超时提示必须合理」的契约）
MAX_TIMER_DELAY_MS = 2_147_483_647


class TimeoutReason(Exception):
    """内部中止原因：携带能力所属 code 与已过截止。"""

    def __init__(self, code: str, timeout_ms: float) -> None:
        super().__init__(f"{code} after {timeout_ms}ms")
        self.code = code
        self.timeout_ms = timeout_ms


def assert_timer_delay(timeout_ms: float, name: str) -> None:
    """校验计时器延迟为正有限数且不超过调度上限。"""
    if not (isinstance(timeout_ms, (int, float)) and math.isfinite(timeout_ms)
            and timeout_ms > 0 and timeout_ms <= MAX_TIMER_DELAY_MS):
        raise ValueError(f"{name} 必须是大于 0 且不超过 {MAX_TIMER_DELAY_MS} 的有限数")


def clamp_timeout(
    requested: Optional[float],
    def_: float,
    max_: float,
    name: str = "timeoutMs",
) -> float:
    """校验调用方可选超时提示，应用后端默认值并封顶。

    :param requested: 调用方可选提示；提供时必须为正有限数（0 不是
        「禁用超时」的公开哨兵）。
    :param def_: 未提供提示时的后端默认值。
    :param max_: 结果封顶的后端上界。
    :returns: ``min(requested ?? def_, max_)``。
    """
    if requested is not None and not (
        isinstance(requested, (int, float)) and math.isfinite(requested) and requested > 0
    ):
        raise ValueError(f"{name} 必须是正有限数")
    return min(requested if requested is not None else def_, max_)


class Deadline:
    """截止信号 + 清除其计时器的清理（dispose 一次性）。"""

    def __init__(self, signal: CancelSignal, dispose: Any) -> None:
        self.signal = signal
        self._dispose = dispose

    def dispose(self) -> None:
        """清除计时器；可安全调用一次。"""
        if self._dispose is not None:
            self._dispose()
            self._dispose = None

    # 上下文管理器：``with deadline(...) as d:`` 退出即清理
    def __enter__(self) -> "Deadline":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.dispose()


def deadline(
    upstream: Optional[CancelSignal],
    timeout_ms: float,
    code: str,
) -> Deadline:
    """融合上游取消与可识别超时；``timeout_ms <= 0`` 是内部「无计时器」哨兵。

    信号只负责通知：调用方必须停止自己的工作。融合用 :meth:`CancelSignal.any`
    ——先取消的源胜出（对齐 ``AbortSignal.any`` 的 reason 采纳语义），
    :func:`timeout_of` 只在超时胜出时读到 :class:`TimeoutReason`。
    """
    if timeout_ms <= 0:
        # 无超时（后台工作）：只转发上游信号；无上游时给一个永不中止的信号
        return Deadline(upstream or CancelSignal(), None)

    assert_timer_delay(timeout_ms, "deadline timeoutMs")
    timer = CancelSignal()
    loop = asyncio.get_running_loop()
    handle = loop.call_later(timeout_ms / 1000.0, lambda: timer.abort(TimeoutReason(code, timeout_ms)))
    signal = CancelSignal.any([upstream, timer]) if upstream is not None else timer
    return Deadline(signal, handle.cancel)


class IdleWatchdog:
    """可重新武装的空闲看门狗：计时器只在 ``next`` 在途时存在。

    消费者思考时间不计入提供方空闲时间。返回的信号在整个调用期内稳定，
    只负责通知；迭代器必须观察它才能终止工作。
    """

    def __init__(
        self,
        upstream: Optional[CancelSignal],
        timeout_ms: float,
        code: str,
    ) -> None:
        assert_timer_delay(timeout_ms, "idleWatchdog timeoutMs")
        self._timeout = CancelSignal()
        self.signal = CancelSignal.any([upstream, self._timeout]) if upstream is not None else self._timeout
        self._timeout_ms = timeout_ms
        self._code = code
        self._handle: Any = None
        self._outstanding = False
        self._disposed = False
        self._loop = asyncio.get_running_loop()

    def _arm(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
        self._handle = self._loop.call_later(
            self._timeout_ms / 1000.0,
            lambda: self._timeout.abort(TimeoutReason(self._code, self._timeout_ms)),
        )

    async def next(self, iterator: Any) -> Any:
        """等待一次迭代器需求，期间武装空闲计时器。"""
        if self._disposed:
            raise RuntimeError("idleWatchdog 已 dispose")
        if self._outstanding:
            raise RuntimeError("idleWatchdog next 已在途")
        self._outstanding = True
        self._arm()
        try:
            return await iterator.__anext__()
        finally:
            if self._handle is not None:
                self._handle.cancel()
                self._handle = None
            self._outstanding = False

    def pulse(self) -> None:
        """传输活动未产生迭代值时重新武装在途需求；否则为 no-op。"""
        if self._disposed or not self._outstanding:
            return
        self._arm()

    def dispose(self) -> None:
        """清除已武装的计时器；可安全调用一次。"""
        if self._disposed:
            return
        self._disposed = True
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def __enter__(self) -> "IdleWatchdog":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.dispose()


def idle_watchdog(
    upstream: Optional[CancelSignal],
    timeout_ms: float,
    code: str,
) -> IdleWatchdog:
    """创建可重新武装的空闲看门狗（工厂；对齐 dsh 导出的 ``idleWatchdog``）。"""
    return IdleWatchdog(upstream, timeout_ms, code)


def timeout_of(x: Any, code: Optional[str] = None) -> Optional[TimeoutReason]:
    """从信号或携带 reason 的对象恢复超时原因。

    :param x: :class:`CancelSignal` 或任何携带 ``reason`` 的对象（如捕获的
        取消错误）。
    :param code: 提供时仅精确匹配该 code 的 :class:`TimeoutReason`。
    :returns: 匹配的超时原因；否则 None（外来 code 走普通取消路径）。
    """
    reason = getattr(x, "reason", None)
    if not isinstance(reason, TimeoutReason):
        return None
    return reason if code is None or reason.code == code else None

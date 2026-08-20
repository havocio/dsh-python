"""事件总线：提供五种分发模式，对标 cordis 的 ``EventsService``。

五种模式：
- ``emit``     —— 发后不理；协程型监听器只被调度，不会被 await。
- ``parallel`` —— 并发 await 所有监听器（异常会被聚合抛出）。
- ``serial``   —— 按顺序 await 监听器，直到某个返回「拦截(真值)」结果为止。
- ``bail``     —— 同步；第一个真值返回胜出（忽略协程型监听器）。
- ``waterfall``—— 围绕最终的 ``next`` 续体把监听器串起来；每个监听器接收
                 ``(*args, next)``，可以调用 ``next()`` 把控制权交给下一个；
                 不调用 ``next()`` 即视为否决后续链路。监听器通过「返回一个
                 新值」来变换结果，这与 dsh 的契约一致（不支持改写参数）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional, Union

# 监听器类型：同步或异步函数皆可
Listener = Callable[..., Union[None, Awaitable[None]]]


def _is_bailed(value: Any) -> bool:
    """判断返回值是否应当中止 bail/serial 分发（空值与 False 视为不中止）。"""
    return value is not None and value is not False


async def _maybe_await(value: Any) -> Any:
    """若是协程则 await，否则原样返回。"""
    if asyncio.iscoroutine(value):
        return await value
    return value


class EventBus:
    def __init__(self) -> None:
        # 事件名 -> 监听器条目列表，每个条目为 {"listener", "global", "scope"}
        # scope 记录监听器注册时的归属上下文；分派时按「祖先链可见性」过滤
        self._hooks: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------ #
    # 注册
    # ------------------------------------------------------------------ #
    def on(
        self,
        name: str,
        listener: Listener,
        *,
        prepend: bool = False,
        global_: bool = False,
        scope: Any = None,
    ) -> Callable[[], bool]:
        """为 ``name`` 注册 ``listener``，返回一个可注销的 disposer。

        :param scope: 监听器归属的上下文。分派时只有「该上下文的祖先链」
            内的监听器可见（父作用域的监听器能收到子作用域事件）。
        """
        hooks = self._hooks.setdefault(name, [])
        entry = {"listener": listener, "global": global_, "scope": scope}
        if prepend:
            hooks.insert(0, entry)
        else:
            hooks.append(entry)

        def dispose() -> bool:
            """从注册表中移除该监听器；成功返回 True，已不存在返回 False。"""
            if entry in hooks:
                hooks.remove(entry)
                return True
            return False

        return dispose

    def once(self, name: str, listener: Listener, **opts: Any) -> Callable[[], bool]:
        """注册一个监听器，首次触发后自动注销。"""
        disposed = False

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            nonlocal disposed
            if not disposed:
                disposed = True
                dispose()
            return listener(*args, **kwargs)

        dispose = self.on(name, wrapper, **opts)
        return dispose

    @staticmethod
    def _visible_in(scope: Any, ancestors: set) -> bool:
        """判断一个监听器归属的 ``scope`` 是否在给定祖先链内可见。"""
        if scope is None:
            return True
        return scope in ancestors

    def _ancestors(self, scope: Any) -> set:
        """收集某上下文自身 + 全部祖先（供分派时做可见性过滤）。"""
        chain = set()
        node = scope
        while node is not None:
            chain.add(node)
            node = getattr(node, "_parent", None)
        return chain

    def _listeners(self, name: str, scope: Any = None) -> list[Listener]:
        """取出该事件名下可见的监听器（按注册顺序）。

        :param scope: 分派上下文；给定后仅保留「注册于其祖先链内」的监听器
            （``global`` 监听器恒可见）。
        """
        if scope is None:
            return [h["listener"] for h in self._hooks.get(name, [])]
        ancestors = self._ancestors(scope)
        return [
            h["listener"]
            for h in self._hooks.get(name, [])
            if h["global"] or self._visible_in(h["scope"], ancestors)
        ]

    # ------------------------------------------------------------------ #
    # 分发
    # ------------------------------------------------------------------ #
    def emit(self, name: str, *args: Any, scope: Any = None) -> None:
        """同步触发监听器；若检测到运行中的事件循环，协程型结果会被调度执行。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        for listener in self._listeners(name, scope):
            result = listener(*args)
            if loop is not None and asyncio.iscoroutine(result):
                asyncio.ensure_future(result)

    def dispatch(self, name: str, *args: Any, scope: Any = None) -> list[Any]:
        """逐个调用监听器，**隔离**单个监听器的异常（contained）。

        与 dsh 的 ``ctx.events.dispatch('emit', args)`` 语义一致：注册表类通知
        （如 ``llm/adapters-updated``）是非否决的——一个监听器失败不得阻断其余
        监听器，也不得使注册提交回滚。返回各监听器的返回值列表。
        """
        results: list[Any] = []
        for listener in self._listeners(name, scope):
            try:
                results.append(listener(*args))
            except Exception:  # noqa: BLE001 - contained：通知失败不影响注册提交
                continue
        return results


    async def parallel(self, name: str, *args: Any, scope: Any = None) -> None:
        """并发 await 所有监听器；任一个失败即抛出聚合异常。"""
        results = await asyncio.gather(
            *[_maybe_await(listener(*args)) for listener in self._listeners(name, scope)],
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise Exception(f"parallel '{name}' 中有 {len(errors)} 个监听器失败") from errors[0]

    async def serial(self, name: str, *args: Any, scope: Any = None) -> Any:
        """按顺序 await 监听器，返回第一个「拦截」值；无则返回 None。"""
        for listener in self._listeners(name, scope):
            result = await _maybe_await(listener(*args))
            if _is_bailed(result):
                return result
        return None

    def bail(self, name: str, *args: Any, scope: Any = None) -> Any:
        """同步 bail：返回第一个真值；无则 None。"""
        for listener in self._listeners(name, scope):
            result = listener(*args)
            if _is_bailed(result):
                return result
        return None

    def waterfall(
        self,
        name: str,
        *args: Any,
        inner: Optional[Callable[..., Any]] = None,
        scope: Any = None,
    ) -> Any:
        """将监听器围绕 ``inner`` 串成瀑布流；每个监听器接收 ``(*args, next)``。

        ``next()`` 会调用下一个监听器（最后落到 ``inner``），并**原样**返回它的
        结果——若下一个监听器是异步生成器则为异步迭代器，是 async 函数则为协程，
        否则为普通值。不调用 ``next()`` 即否决后续链路。调用方自行决定对该结果
        是 ``await``（pre-step 风格）还是 ``async for``（stream 风格）。
        """
        cbs = self._listeners(name, scope)

        def make_next(index: int) -> Callable[[], Any]:
            """构造第 index 个监听器对应的 next 续体。"""
            def nxt() -> Any:
                if index < len(cbs):
                    return cbs[index](*args, make_next(index + 1))
                return inner() if callable(inner) else None

            return nxt

        if cbs:
            return cbs[0](*args, make_next(1))
        return inner() if callable(inner) else None

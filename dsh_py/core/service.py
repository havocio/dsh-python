"""服务基类。

服务是注册在某个 :class:`AppContext` 上的具名能力。子类构造时调用
``super(ctx, name)`` 即会自动把自身注册到上下文，对标 cordis 的 ``Service``
（例如 ``super(ctx, 'llm')``）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dsh_py.core.context import AppContext


class Service:
    def __init__(self, ctx: "AppContext", name: str | None = None) -> None:
        self.ctx = ctx
        # 未显式指定名称时，默认用类名作为服务名
        self.name = name or self.__class__.__name__
        ctx.provide(self.name, self)

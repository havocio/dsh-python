"""工具调用超时强制守卫（guard/timeout-policy，治理类）。

协作式工具调用超时强制器：工具在元数据中声明 ``timeout_ms``（经
``ctx.tools.register(..., timeout_ms=...)``），并承诺尊重执行；本插件为这次执行
**武装一个截止**，将其自身到期映射到模型可见的 ``TOOL_TIMEOUT`` 文本结果，**不与
工具 promise 竞速或遗弃**——我们的计时器优先于嵌套的外部截止（另一个 ``tools/execute``
包装的计时器先触发时，会读作普通上游取消，而不是我们的超时）。

实现说明（对标 dsh 的 ``deadline``/``timeoutOf`` 封装）：dsh 把派生截止挂到
``exec.signal`` 上交给工具的 handler，由工具自身的取消逻辑尊重它；dsh_py 的 handler
当前不接收 ``exec.signal``，故本插件改为「赛跑 + 取消内层」来强制时限——到期即取消
内层协程并返回结构化超时结果。语义与 dsh 一致：模型看到的永远是 ``TOOL_TIMEOUT`` 文本。
"""

from __future__ import annotations

import asyncio

from dsh_py.core.context import AppContext


# 本插件自身拥有的代码：同时作为内部 deadline 分类码与替换结果上的结构化错误码。
# 把 ``timeoutOf`` 限定到它，可避免把另一个外层 deadline（先触发）误读为本插件的超时。
TOOL_TIMEOUT = "TOOL_TIMEOUT"


def tool_timeout_result(timeout_ms: int) -> tuple[str, bool]:
    """本插件计时器胜出时替换的结果：``content`` 是面向模型的消息；``isError`` 为 True。"""
    message = f"tool call timed out after {timeout_ms}ms"
    return f"Error: {message}", True


def apply(ctx: AppContext, config: dict | None = None) -> None:
    """注册超时包装器：解析调用方可见的工具定义，临时武装截止，delegate，再替换结果。"""
    @ctx.on("tools/execute")
    async def on_execute(event, next):
        exec = event["exec"]
        entry = ctx.tools.get(exec["name"], exec.get("agent"))
        timeout_ms = entry.timeout_ms if entry is not None else None
        # 工具未声明预算：无截止，原样 delegate
        if timeout_ms is None:
            return await next()

        own_timer_fired = False
        inner = asyncio.ensure_future(next())
        loop = asyncio.get_event_loop()

        def _fire():
            nonlocal own_timer_fired
            own_timer_fired = True
            inner.cancel()

        handle = loop.call_later(timeout_ms / 1000.0, _fire)
        try:
            result = await inner
        except asyncio.CancelledError:
            # 只有我们的计时器触发才替换为结构化超时；否则（外层取消）向上传播
            if own_timer_fired:
                return tool_timeout_result(timeout_ms)
            raise
        finally:
            handle.cancel()
        # 内层正常完成：若我们的计时器曾触发（限定的代码），内层也已看到取消并
        # 抵达静默——用模型可见的 TOOL_TIMEOUT 替换它返回的任何内容。
        if own_timer_fired:
            return tool_timeout_result(timeout_ms)
        return result


apply.name = "timeout-policy"
apply.inject = ["tools"]

"""``/compact`` 命令（command-compact，对标 dsh 的 ``dsh-command-compact``）：
经后端无关的压缩 seam 提供人类可调用的手工压缩。

无参数；调用 ``ctx.compaction.compactNow(agent, signal, commandId)``，并把六类
预期失败（:class:`ManualCompactionError`）映射为简洁的人类结果文本。成功时报告
被遮蔽的条目数与估算 token 数，并携带摘要事件 seq 供溯源。
"""

from __future__ import annotations

from typing import Any

from dsh_py.core.context import AppContext
from dsh_py.services.commands import CommandInvocation, CommandResult
from dsh_py.services.compaction import ManualCompactionError

USAGE = "用法: /compact（无参数）"

# 六类预期失败的面向用户文案（对话保持不变；尝试记录在会话日志中）
_EXPECTED_FAILURES = {
    "busy": "压缩不可用：进程已有进行中的压缩，或 agent 不空闲。",
    "cancelled": "压缩已取消。",
    "changed": "选中的历史在替换前发生了变化。对话保持不变；尝试已记录在会话日志中。",
    "summary": "压缩无法产出有用的摘要。对话保持不变；尝试已记录在会话日志中。",
    "commit": "压缩未干净完成；部分会话历史可能已变化。重试前请检查当前会话状态。",
    "persistence": "压缩已完成，但会话无法保存。",
}


def _expected_failure(error: ManualCompactionError) -> CommandResult:
    return CommandResult(kind="error", text=_EXPECTED_FAILURES.get(error.code, f"压缩失败: {error.message}"))


async def _execute_compact(ctx: AppContext, invocation: CommandInvocation) -> CommandResult:
    if invocation.rawInput.strip():
        return CommandResult(kind="error", text=USAGE)
    try:
        result = await ctx.compaction.compact_now(
            invocation.agent, invocation.signal, invocation.commandId,
        )
        if result is None:
            return CommandResult(kind="success", text="尚无可以压缩的历史。")
        return CommandResult(
            kind="success",
            text=f"已压缩 {len(result.shadowedSeqs)} 条历史（约 {result.shadowedTokenCount} tokens）。",
            sourceEventSeq=result.summarySeq,
        )
    except ManualCompactionError as error:
        if invocation.signal is not None and getattr(invocation.signal, "aborted", False):
            return CommandResult(kind="error", text=_EXPECTED_FAILURES["cancelled"])
        return _expected_failure(error)


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``/compact`` 命令（经 ``ctx.commands``）。"""
    if not ctx.has_service("commands"):
        raise RuntimeError("commands 注册表未就绪：请先加载 dsh_py.services.commands:apply")
    if not ctx.has_service("compaction"):
        raise RuntimeError("压缩服务未就绪：请先加载压缩后端（如 compaction_basic:apply）")

    def handler(invocation: CommandInvocation) -> Any:
        return _execute_compact(ctx, invocation)

    ctx.commands.register("compact", "压缩较早的会话历史", handler)


apply.provides = ["commandCompact"]
apply.inject = ["commands", "compaction"]

"""记录的人类反馈（feedback/command-feedback，第 3 层）。

- :func:`record_feedback` —— 与触发方式无关地追加一条权威的 ``feedback/record``
  log-only 事件（绝不进入模型上下文或派生历史）；
- ``/feedback`` 命令 —— 面向用户的反馈生产方：校验、记录、应答。应答携带
  会话 id、匿名用户 id 与会话共享策略披露。追加是急切的但不强制 flush，
  所以确认只表明「已记录」，不表明「已落盘」。

命令反馈评价只写入日志；dsh 的 ``session-telemetry-otel`` 会观察
``feedback/record`` 释放待处理的遥测前缀。dsh_py 尚无遥测缝，本模块读不到
telemetry 服务时披露「Session sharing is not configured.」（读经插件上下文，
服务缺席时命令仍可用）。

匿名用户 id 来自 :mod:`dsh_py.services.anonymous_user_id`
（identity/anonymous-user-id 的完整版：UUID v4 持久化到 DSH_HOME 下
``.anonymous-user-id``，进程级 memo + wx 并发 settle + best-effort）。
"""

from __future__ import annotations

from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.services.anonymous_user_id import get_or_create_anonymous_user_id
from dsh_py.services.commands import CommandInvocation, CommandResult

USAGE = "Usage: /feedback <text>"

_DISCLOSURE = {
    "full": "Session sharing is enabled.",
    "feedback-only": "Session sharing is feedback-gated; recording feedback releases the session prefix for sharing.",
    "disabled": "Session sharing is disabled.",
}


def record_feedback(session: Any, text: str) -> None:
    """记录一条人类反馈，与任何 UI 触发方式无关。

    :raises TypeError: 规范化（trim）后文本为空。
    """
    normalized = text.strip()
    if normalized == "":
        raise TypeError("feedback text must not be empty")
    session.append("feedback/record", {"text": normalized})


def _sharing_disclosure(telemetry: Any) -> str:
    """已挂载后端的披露策略；无后端时给「未配置」提示。"""
    if telemetry is None:
        return "Session sharing is not configured."
    return _DISCLOSURE.get(getattr(telemetry, "sharing", ""), _DISCLOSURE["disabled"])


def _execute_feedback_command(invocation: CommandInvocation, ctx: AppContext) -> CommandResult:
    """校验、记录并应答一条反馈；出错时不留下任何 ``feedback/record`` 事件。"""
    if invocation.rawInput.strip() == "":
        return CommandResult(kind="error", text=f"Feedback text is required. {USAGE}")
    record_feedback(invocation.agent.session, invocation.rawInput)
    # 遥测服务缺席时命令仍可用：读经插件上下文，按属性有无降级披露
    telemetry = ctx.sessionTelemetry if hasattr(ctx, "sessionTelemetry") else None
    session = invocation.agent.session
    return CommandResult(kind="success", text=(
        f"Feedback recorded for session {session.header.id}\n"
        f"Anonymous user: {get_or_create_anonymous_user_id()}. "
        f"{_sharing_disclosure(telemetry)}"
    ))


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """为每个装配的命令适配器注册全局 ``/feedback`` 命令。"""
    ctx.commands.register(
        "feedback",
        "record feedback about this session",
        lambda invocation: _execute_feedback_command(invocation, ctx),
    )


apply.name = "command-feedback"
apply.inject = ["commands"]

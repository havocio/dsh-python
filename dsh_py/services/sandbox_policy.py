"""sandbox 会话级策略（对标 dsh 的 ``@deepseek-ai/dsh-sandbox-policy``）。

以会话日志为存储：运行时切换（UI 策略控件或测试场景）记录为会话上的一条
``sandbox/mode`` 事件；``effective = fold(events) ?? 部署默认值``，使覆盖在重启后由重放
恢复，且两个会话互不串扰。事件为 log-only（类似 ``approval/*``）。

每个强制执行家族共享此策略状态，故放在 policy 包而非任一能力 seam。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dsh_py.services.sandbox import ConfinedSandboxMode, SandboxExecutionPolicy, SandboxPolicy

#: 所有沙箱模式，用于选项广告与对不可信模式字符串的运行时校验。
SANDBOX_MODES: tuple[str, ...] = ("read-only", "workspace-write", "danger-full-access")


def effectiveSandboxMode(events: list) -> str | None:
    """会话的沙箱模式覆盖：日志中最后一个 ``sandbox/mode`` 事件，无则返回 ``None``。"""
    for event in reversed(events):
        if getattr(event, "type", None) == "sandbox/mode":
            return event.data.get("mode")
    return None


def setSandboxMode(session: Any, mode: str) -> None:
    """写入路径：向会话追加恰好一条 ``sandbox/mode`` 事件（switch 即事件）。"""
    session.append("sandbox/mode", {"mode": mode})


@dataclass
class SandboxPolicyResolver:
    """解析每次能力调用的完整策略：会话覆盖 ?? 部署默认值，叠加工作区根。"""

    default_mode: str = "read-only"
    workspace_root: str = "."

    def resolve(self, session: Any | None, mode_override: str | None = None) -> SandboxPolicy:
        """解析策略；``mode_override`` > 会话覆盖 > 部署默认值。"""
        mode: str | None = mode_override
        if mode is None and session is not None:
            mode = effectiveSandboxMode(list(session.events))
        if mode is None:
            mode = self.default_mode
        if mode not in SANDBOX_MODES:
            raise ValueError(f"invalid sandbox mode: {mode}")
        session_id = getattr(session, "header", None)
        session_id = getattr(session_id, "id", None) if session_id is not None else None
        return SandboxPolicy(
            mode=mode,  # type: ignore[arg-type]
            workspaceRoot=self.workspace_root,
            sessionId=session_id,
        )

    def as_execution_policy(self, session: Any | None = None, mode_override: str | None = None) -> SandboxExecutionPolicy:
        return self.resolve(session, mode_override).as_execution_policy()


__all__ = [
    "SANDBOX_MODES",
    "effectiveSandboxMode",
    "setSandboxMode",
    "SandboxPolicyResolver",
]

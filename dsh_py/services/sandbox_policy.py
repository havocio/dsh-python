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
from dsh_py.core.context import AppContext

#: 所有沙箱模式，用于选项广告与对不可信模式字符串的运行时校验。
SANDBOX_MODES: tuple[str, ...] = ("read-only", "workspace-write", "danger-full-access")


def owner_of(actor: Any) -> Any:
    """从一次能力调用的 ``actor`` 推导「观测/策略归属」的会话身份。

    对标 dsh 的 ``actor.agent.session``：``actor`` 通常是工具层透传的 Agent
    （``exec['agent']``），其 ``.session`` 即归属会话；若 ``actor`` 自身就是 Agent
    或 Session，也需正确收敛。返回 ``None`` 表示无归属（落到全局/默认策略）。
    """
    if actor is None:
        return None
    agent = getattr(actor, "agent", None)
    if agent is None:
        agent = actor
    return getattr(agent, "session", None)


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


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``sandboxPolicy`` 服务（会话级沙箱策略解析器）。

    供 ``fs-sandbox`` 等强制执行家族在每次能力调用时解析 ``{mode, workspaceRoot,
    sessionId}``；缺省策略由部署配置决定（默认 ``read-only``，fail-closed）。
    """
    config = config or {}
    resolver = SandboxPolicyResolver(
        default_mode=config.get("default_mode", "read-only"),
        workspace_root=config.get("workspace_root", "."),
    )
    ctx.provide("sandboxPolicy", resolver)


apply.provides = ["sandboxPolicy"]  # 声明：本插件提供 sandboxPolicy 服务（供 loader 拓扑排序）


__all__ = [
    "SANDBOX_MODES",
    "effectiveSandboxMode",
    "setSandboxMode",
    "SandboxPolicyResolver",
]

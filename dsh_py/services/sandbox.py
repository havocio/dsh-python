"""sandbox 能力 seam（对标 dsh 的 ``@deepseek-ai/dsh-sandbox``）。

同一世界进程隔离能力 seam：把精确的子进程 argv 包裹在主机路径文件策略之下。
容器、微虚拟机、远程执行应替换外层能力 seam；本服务共享主机内核与文件系统。

本文件定义词汇表、升级阶梯、逃逸标记、拒绝词表，以及抽象服务 ``SandboxProvider``。
本地后端在 ``services/sandbox_local.py``；会话级策略解析在 ``services/sandbox_policy.py``。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service

# --------------------------------------------------------------------------- #
# 词汇表
# --------------------------------------------------------------------------- #

#: 文件效应模式。``read-only`` 仅允许 ``/dev/null`` 等必要 sink；``workspace-write``
#: 另允许工作区与后端定义的临时区；``danger-full-access`` 绕过隔离。
SandboxMode = str  # 'read-only' | 'workspace-write' | 'danger-full-access'
ConfinedSandboxMode = str  # 排除 'danger-full-access'

#: 严格更宽阶梯：键为生效模式，值为可升级到的模式集（在执行时校验，绝不烘焙进 schema）。
WIDER_MODES: dict[str, list[str]] = {
    "read-only": ["workspace-write", "danger-full-access"],
    "workspace-write": ["danger-full-access"],
}

#: 闭包升级目标词汇——任何调用可能升级到的模式（``read-only`` 是底，无人升级到它）。
ESCALATION_TARGETS: list[str] = ["workspace-write", "danger-full-access"]

#: 执行非 ``danger-full-access`` 时，harness 失败时返回的错误码（fail-closed）。
SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"


@dataclass
class SandboxExecutionPolicy:
    """一次能力调用解析出的完整文件效应策略。"""

    mode: str  # SandboxMode
    workspaceRoot: str
    #: 调用会话的不透明身份（branded SessionId）；缺失则回退到逐调用后端状态。
    sessionId: Any | None = None


@dataclass
class SandboxPolicy:
    """一次能力调用携带的文件效应策略（仅受限模式，不含 danger-full-access）。"""

    mode: str  # ConfinedSandboxMode
    workspaceRoot: str
    sessionId: Any | None = None

    def as_execution_policy(self) -> SandboxExecutionPolicy:
        return SandboxExecutionPolicy(mode=self.mode, workspaceRoot=self.workspaceRoot, sessionId=self.sessionId)


@dataclass
class RunnerFailureRule:
    """执行器在命令执行前失败的证据规则。"""

    fatalSignatures: list[str]
    allowedExitCodes: list[int] | None = None
    informationalLines: list[str] | None = None


@dataclass
class ConfinedArgv:
    """``SandboxProvider.confine`` 的结果：要 spawn 的 argv，以及所选后端的执行完整性。"""

    argv: list[str]
    enforcement: str  # 'full' | 'partial'
    #: 本后端对「被拒绝的文件效应」产生的、大小写不敏感的 stderr 子串。
    denialSignatures: list[str]
    runnerFailureRules: list[RunnerFailureRule]


class SandboxUnavailableError(Exception):
    """``confine`` 无法执行请求模式时抛出，携带 ``SANDBOX_UNAVAILABLE``。"""

    def __init__(self, mode: str, detail: str | None = None) -> None:
        super().__init__(
            f'sandbox mode "{mode}" is requested but no sandbox backend is usable on this host; '
            "refusing to run the command unconfined. Install bubblewrap or run a Landlock-enforcing "
            "kernel (Linux), ensure sandbox-exec is usable (macOS), or ensure the ACL restricted-token "
            "runner can start (Windows) — otherwise switch the consumer to danger-full-access."
            + (f" Runner failure: {detail}" if detail is not None else "")
        )
        self.name = "SandboxUnavailableError"
        self.code = SANDBOX_UNAVAILABLE


# --------------------------------------------------------------------------- #
# 升级词汇（escalation）
# --------------------------------------------------------------------------- #

EscalationOutcome = str  # 'allowed-once' | 'rejected' | 'cancelled' | 'unavailable'


class EscalationApprover:
    """升级审批最小形状（结构化于 approval seam）。"""

    async def request(self, req: dict) -> EscalationOutcome:  # pragma: no cover - 由消费方闭包实现
        raise NotImplementedError


@dataclass
class EscalationApproval:
    approver: EscalationApprover | None
    agent: Any | None
    callId: Any
    toolName: str
    signal: Any | None = None


@dataclass
class EscalationRequest:
    requestedMode: str
    justification: str
    effectiveMode: str  # SandboxMode
    subject: str


def validateEscalationArgs(sandboxPermissions: str | None, justification: str | None) -> None:
    """校验升级参数的配对关系（schema 无法表达的约束）。"""
    if sandboxPermissions is not None and justification is None:
        raise ValueError("invalid escalation: sandbox_permissions requires a justification")
    if justification is not None and sandboxPermissions is None:
        raise ValueError("invalid escalation: justification is only valid together with sandbox_permissions")
    if justification is not None and justification.strip() == "":
        raise ValueError("invalid justification: expected a non-empty sentence")


def sandboxDenialMarker(mode: str) -> str:
    """模型可见的拒绝标记（两个强制执行家族共用，便于模型一致识别）。"""
    return f"[sandbox: file access denied under {mode} mode]"


def escalationHintMarker(subject: str) -> str:
    """随拒绝附带的同轮升级提示（站在决策点，避免模型依赖工具描述记忆重试）。"""
    return (
        f"[sandbox: escalation available — retry this exact {subject} once with "
        "sandbox_permissions (the narrowest wider mode that suffices) + justification; "
        "the approval prompt asks the user]"
    )


def approveEscalation(request: EscalationRequest, approval: EscalationApproval) -> str:
    """执行前、任何代码运行前解析一次沙箱升级（有序 fail-closed）。

    返回授予的模式；非严格更宽、缺审批服务、无 agent、拒绝/取消/不可答均抛精确文本。
    """
    mode = request.requestedMode
    effective_mode = request.effectiveMode
    if mode not in (WIDER_MODES.get(effective_mode, [])):
        raise ValueError(
            f'sandbox escalation to "{mode}" is not strictly wider than this call\'s current "{effective_mode}" mode'
        )
    if approval.approver is None:
        raise ValueError(f'sandbox escalation to "{mode}" requires approval, but no approval service is composed')
    if approval.agent is None:
        raise ValueError(f'sandbox escalation to "{mode}" requires approval, but the call has no agent to route it through')
    outcome = approval.approver.request({
        "agent": approval.agent,
        "toolName": approval.toolName,
        "callId": approval.callId,
        "reason": f"escalate sandbox to {mode}: {request.justification}",
        **({"signal": approval.signal} if approval.signal is not None else {}),
    })
    if isinstance(outcome, str):
        oc = outcome
    else:
        # 允许协程/可等待（本同步边界以 async 包装）：交给调用方自行 await。
        raise TypeError("approver.request must return a sync outcome here")
    if oc == "allowed-once":
        return mode
    if oc == "rejected":
        raise ValueError(f'the user rejected escalating this {request.subject} to "{mode}"')
    if oc == "cancelled":
        raise ValueError(f'approval for escalating to "{mode}" was cancelled')
    raise ValueError(f'sandbox escalation to "{mode}" requires approval, but no approval channel is available')


# --------------------------------------------------------------------------- #
# 词根（roots）
# --------------------------------------------------------------------------- #


def canonicalPath(path: str) -> str:
    """解析为规范路径（符号链接展开）；失败则返回原样（保守：缺失的前缀匹配不到）。"""
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def writableRoots(policy: SandboxExecutionPolicy) -> list[str]:
    """受限执行可写入的词根（规范、去重 allow-list）。``read-only`` 返回空。"""
    if policy.mode != "workspace-write":
        return []
    return sorted({canonicalPath(p) for p in [policy.workspaceRoot, "/tmp", os.path.join(os.sep, "tmp")]})


# --------------------------------------------------------------------------- #
# Seam
# --------------------------------------------------------------------------- #


class SandboxProvider(Service):
    """进程沙箱抽象服务；``confine`` 必须返回强制 argv 或在包裹/运行期 fail-closed；
    静默的非受限透传被禁止。"""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "sandbox")

    @abstractmethod
    def confine(self, argv: list[str], policy: SandboxPolicy) -> ConfinedArgv:
        """把 ``argv`` 包裹为在 ``policy`` 下隔离执行的字样；调用方以此替换自己的 argv。"""
        raise NotImplementedError


__all__ = [
    "SandboxMode", "ConfinedSandboxMode", "WIDER_MODES", "ESCALATION_TARGETS",
    "SANDBOX_UNAVAILABLE", "SandboxExecutionPolicy", "SandboxPolicy", "RunnerFailureRule",
    "ConfinedArgv", "SandboxUnavailableError", "EscalationOutcome", "EscalationApprover",
    "EscalationApproval", "EscalationRequest", "validateEscalationArgs", "sandboxDenialMarker",
    "escalationHintMarker", "approveEscalation", "canonicalPath", "writableRoots", "SandboxProvider",
]

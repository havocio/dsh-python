"""sandbox seam 契约单测（对照 dsh 临时冒烟脚本，正式入库）。

覆盖：升级参数配对校验、拒绝/升级提示标记、升级审批有序 fail-closed、路径词根解析、
升级阶梯。均不依赖真实沙箱后端（bubblewrap/landlock/sandbox-exec/ACL）。
"""

from __future__ import annotations

import asyncio
import os

from dsh_py.services.sandbox import (
    EscalationApproval,
    EscalationApprover,
    EscalationRequest,
    SandboxExecutionPolicy,
    SandboxPolicy,
    SandboxUnavailableError,
    WIDER_MODES,
    approveEscalation,
    canonicalPath,
    escalationHintMarker,
    sandboxDenialMarker,
    validateEscalationArgs,
    writableRoots,
)


class _AllowApprover(EscalationApprover):
    """测试用审批器：同步返回固定结果（对齐 approveEscalation 的同步调用约定）。"""

    def __init__(self, outcome: str) -> None:
        self._outcome = outcome

    def request(self, req):  # 同步：approveEscalation 以同步方式调用
        return self._outcome


async def test_validate_escalation_args_pairing() -> None:
    validateEscalationArgs("workspace-write", "need write")  # 两者都有 → OK
    for bad in [("workspace-write", None), (None, "need write"), ("workspace-write", "   ")]:
        try:
            validateEscalationArgs(*bad)
            raise AssertionError(f"应拒绝非法配对: {bad}")
        except ValueError:
            pass
    print("  ✓ 升级参数配对校验正确")


async def test_markers() -> None:
    assert sandboxDenialMarker("read-only") == "[sandbox: file access denied under read-only mode]"
    hint = escalationHintMarker("command")
    assert "sandbox_permissions" in hint and "command" in hint
    print("  ✓ 拒绝/升级提示标记格式正确")


async def test_approve_escalation_fail_closed() -> None:
    base = dict(requestedMode="workspace-write", justification="j",
                effectiveMode="read-only", subject="command")

    def approval(oc: str) -> EscalationApproval:
        return EscalationApproval(approver=_AllowApprover(oc), agent=object(),
                                  callId="c", toolName="tool")

    # 非严格更宽 → 拒绝（请求模式不比生效模式更宽）
    nonwider = dict(base)
    nonwider["requestedMode"] = "read-only"
    try:
        approveEscalation(EscalationRequest(**nonwider), approval("allowed-once"))
        raise AssertionError("应拒绝非更宽升级")
    except ValueError:
        pass
    # 缺审批服务 → 拒绝
    try:
        approveEscalation(EscalationRequest(**base),
                          EscalationApproval(approver=None, agent=object(), callId="c", toolName="t"))
        raise AssertionError("应拒绝缺审批服务")
    except ValueError:
        pass
    # 缺 agent → 拒绝
    try:
        approveEscalation(EscalationRequest(**base),
                          EscalationApproval(approver=_AllowApprover("allowed-once"), agent=None,
                                             callId="c", toolName="t"))
        raise AssertionError("应拒绝缺 agent")
    except ValueError:
        pass
    # 用户拒绝 → 拒绝
    try:
        approveEscalation(EscalationRequest(**base), approval("rejected"))
        raise AssertionError("应拒绝用户拒绝")
    except ValueError:
        pass
    # 允许一次 → 返回授予的模式
    assert approveEscalation(EscalationRequest(**base), approval("allowed-once")) == "workspace-write"
    print("  ✓ 升级审批有序 fail-closed")


async def test_path_roots_and_ladder() -> None:
    assert WIDER_MODES["read-only"] == ["workspace-write", "danger-full-access"]
    assert WIDER_MODES["workspace-write"] == ["danger-full-access"]
    # read-only → 无写入词根
    assert writableRoots(SandboxExecutionPolicy(mode="read-only", workspaceRoot="/ws")) == []
    # workspace-write → 含 workspaceRoot + /tmp（规范化、去重，平台无关比对）
    roots = writableRoots(SandboxExecutionPolicy(mode="workspace-write", workspaceRoot="/ws"))
    assert canonicalPath("/ws") in roots and canonicalPath("/tmp") in roots
    # canonicalPath 返回规范化字符串（不抛）
    assert isinstance(canonicalPath("rel/../x"), str)
    print("  ✓ 路径词根 / 升级阶梯正确")


async def test_unavailable_error() -> None:
    err = SandboxUnavailableError("workspace-write", "runner failed")
    assert err.code == "SANDBOX_UNAVAILABLE"
    assert "workspace-write" in str(err)
    print("  ✓ SandboxUnavailableError 携带 SANDBOX_UNAVAILABLE")


async def main() -> None:
    print("== test_sandbox ==")
    await test_validate_escalation_args_pairing()
    await test_markers()
    await test_approve_escalation_fail_closed()
    await test_path_roots_and_ladder()
    await test_unavailable_error()
    print("OK: sandbox seam 契约单测通过")


if __name__ == "__main__":
    asyncio.run(main())

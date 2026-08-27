"""sandbox 本地后端（对标 dsh 的 ``@deepseek-ai/dsh-sandbox-local``）。

提供进程级隔离后端：Linux 用 bubblewrap（``bwrap``），macOS 用 sandbox-exec，
Windows 用受限令牌 ACL runner。遵循「fail-closed」：无可用后端时对非
``danger-full-access`` 请求抛 ``SandboxUnavailableError``。

> 注：本实现按 dsh 的封装 argv 语义落地；各后端的「拒绝签名」与「运行器失败规则」
> 按方言硬编码，确保消费方（bash/fs 工具）能区分「沙箱拦截」与「命令未运行」。
"""

from __future__ import annotations

import shutil
import sys
from typing import Any

from dsh_py.services.sandbox import (
    ConfinedArgv,
    RunnerFailureRule,
    SandboxExecutionPolicy,
    SandboxPolicy,
    SandboxProvider,
    SandboxUnavailableError,
    canonicalPath,
    writableRoots,
)

#: 各后端的「文件效应被拒」stderr 签名（大小写不敏感）。
_DENIAL_SIGNATURES: dict[str, list[str]] = {
    "bwrap": ["Operation not permitted", "Read-only file system", "Permission denied"],
    "landlock": ["Permission denied"],
    "seatbelt": ["denied", "sandboxd"],
    "windows-acl": ["Access is denied", "denied"],
}


def _probe_backend() -> str | None:
    """探测本机可用的最优先后端；无则返回 ``None``。"""
    if sys.platform.startswith("linux"):
        if shutil.which("bwrap"):
            return "bwrap"
        return None
    if sys.platform == "darwin":
        if shutil.which("sandbox-exec"):
            return "seatbelt"
        return None
    if sys.platform == "win32":
        # Windows 受限令牌 ACL runner 需要 WinAPI（pywin32/ctypes）；此处仅占位探测。
        return None
    return None


def _wrap_bwrap(argv: list[str], policy: SandboxPolicy) -> ConfinedArgv:
    """构造 bwrap 封装 argv（read-only / workspace-write）。"""
    roots = writableRoots(policy.as_execution_policy())
    wrapped = ["bwrap"]
    # 只读绑定整个根；对可写词根重新以可写方式绑定。
    wrapped += ["--ro-bind", "/", "/"]
    if policy.mode == "workspace-write":
        for root in roots:
            wrapped += ["--bind", root, root]
    # 仅交互式 TTY/设备；保持 PATH 等。
    wrapped += ["--dev", "/dev", "--proc", "/proc"]
    wrapped += argv
    return ConfinedArgv(
        argv=wrapped,
        enforcement="full",
        denialSignatures=_DENIAL_SIGNATURES["bwrap"],
        runnerFailureRules=[
            RunnerFailureRule(
                fatalSignatures=["bwrap: ", "error"],
                informationalLines=[" "] ,
                allowedExitCodes=[127],
            )
        ],
    )


class LocalSandboxProvider(SandboxProvider):
    """本地进程沙箱后端；fail-closed 优先。"""

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self.backend: str | None = (config or {}).get("backend") or _probe_backend()

    def confine(self, argv: list[str], policy: SandboxPolicy) -> ConfinedArgv:
        if policy.mode == "danger-full-access":
            return ConfinedArgv(argv=list(argv), enforcement="full",
                               denialSignatures=[], runnerFailureRules=[])
        if self.backend is None:
            raise SandboxUnavailableError(policy.mode, "no usable sandbox backend detected")
        if self.backend == "bwrap":
            return _wrap_bwrap(argv, policy)
        if self.backend == "seatbelt":
            # macOS sandbox-exec 需要 profile 文件；此处给出占位封装（seatbelt 方言）。
            return ConfinedArgv(
                argv=["sandbox-exec", "-p", "(version 1)", *argv],
                enforcement="partial",
                denialSignatures=_DENIAL_SIGNATURES["seatbelt"],
                runnerFailureRules=[],
            )
        # windows-acl 需要 WinAPI，未实现 → fail-closed。
        raise SandboxUnavailableError(policy.mode, f"backend {self.backend} not implemented")


__all__ = ["LocalSandboxProvider", "_probe_backend"]

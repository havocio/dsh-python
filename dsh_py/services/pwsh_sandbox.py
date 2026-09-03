"""沙箱化 PowerShell 执行体（pwsh-sandbox，对标 dsh 的 ``@deepseek-ai/dsh-pwsh-sandbox``）。

注册为 ``ctx.pwshSandbox``：在本地 pwsh 执行器之上，把 pwsh argv 经 ``ctx.sandbox.confine``
包裹为隔离执行。``mode=danger-full-access`` 直接透传（无隔离）；否则按解析策略封堵后运行。
运行中（runner spawn 失败、runner 启动失败）抛 ``SandboxUnavailableError``（fail-closed）。
沙箱事实（mode/enforcement/denial）随结果返回，供工具层渲染。

封堵分类方言（denial 签名、runner 失败规则）与 dsh 的 ``pwsh-sandbox/helpers.ts`` 一致：
拒绝优先于运行器失败（命令未运行）；runner 失败的致命行优先于 denial 签名。

与 dsh 差异（已注明）：
- dsh 的 ``SandboxPwshExecutor`` 注册为 ``ctx.shell``（覆盖 bash）；dsh_py 让其注册为
  并列的 ``ctx.pwshSandbox``，工具层在封堵装配下调用它、否则回退 ``ctx.pwsh``。
- 默认模式取自 ``ctx.sandboxPolicy.default_mode``（未挂载则为 None → 不封堵）。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.services.pwsh_local import (
    DEFAULT_GRACE_MS,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_MS,
    PwshLocalService,
)
from dsh_py.services.sandbox import (
    ConfinedArgv,
    SandboxPolicy,
    SandboxUnavailableError,
)
from dsh_py.services.sandbox_policy import SandboxPolicyResolver

#: 运行器启动失败判定（对标 dsh helpers）：仅 Node ENOENT/EACCES 且 argv[0] 可溯源。
EXECUTABLE_SPAWN_CODES = {"EACCES", "ENOENT"}


def _is_usable_workdir(path: str) -> bool:
    try:
        return os.path.isdir(path) and os.access(path, os.X_OK)
    except OSError:
        return False


def _is_runner_spawn_failure(error: Any, runner_program: Optional[str], workdir: str) -> bool:
    """调用方 cwd 可用前提下，把 Node ENOENT/EACCES 且 argv[0] 可溯源的失败归为运行器启动失败。"""
    if runner_program is None or not _is_usable_workdir(workdir):
        return False
    if not isinstance(error, Exception):
        return False
    code = getattr(error, "code", None) or getattr(error, "errno", None)
    path = getattr(error, "path", None)
    syscall = getattr(error, "syscall", None)
    if not isinstance(code, str) or code not in EXECUTABLE_SPAWN_CODES:
        return False
    if not isinstance(syscall, str):
        return False
    exact_syscall = f"spawn {runner_program}"
    if path is None:
        return syscall == exact_syscall
    if not isinstance(path, str) or path != runner_program:
        return False
    return syscall == "spawn" or syscall == exact_syscall


def _classify_runner_failure(exit_code: Optional[int], stderr: str, rules: Any) -> Optional[str]:
    """按结构化 runner 失败规则分类一次失败运行；返回首条致命行，否则 None。"""
    if exit_code is None or exit_code == 0:
        return None
    lines = (stderr or "").splitlines()
    for rule in (rules or []):
        allowed = getattr(rule, "allowedExitCodes", None)
        if allowed is not None and exit_code not in allowed:
            continue
        informational = {s.lower() for s in (getattr(rule, "informationalLines", None) or [])}
        fatal = [s.lower() for s in (getattr(rule, "fatalSignatures", None) or []) if s.strip()]
        for line in lines:
            lowered = line.lower()
            if lowered in informational:
                continue
            if any(sig in lowered for sig in fatal):
                return line
    return None


def _matches_signature(exit_code: Optional[int], stderr: str, signatures: Any) -> bool:
    """大小写不敏感匹配非零退出的 stderr 拒绝签名。"""
    if exit_code is None or exit_code == 0:
        return False
    lowered = (stderr or "").lower()
    return any(sig.lower() in lowered for sig in (signatures or []))


class SandboxPwshService(PwshLocalService):
    """``pwsh`` 封堵执行体（``ctx.pwshSandbox``）：在本地 pwsh 之上施加沙箱隔离。"""

    def __init__(self, ctx: AppContext, **kwargs: Any) -> None:
        super().__init__(ctx, name="pwshSandbox", **kwargs)
        # 默认沙箱模式取自 ctx.sandboxPolicy（未挂载则为 None → 不封堵）。
        self.sandbox_mode: Optional[str] = None
        if ctx.has_service("sandboxPolicy"):
            resolver = ctx.sandboxPolicy
            self.sandbox_mode = getattr(resolver, "default_mode", None)

    async def execute(  # type: ignore[override]
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout_ms: Optional[int] = None,
        env: Optional[dict] = None,
        dsh_env: Optional[dict] = None,
        policy: Any = None,
    ) -> dict:
        """运行一条命令：danger-full-access 透传；否则按 policy 经 ``ctx.sandbox.confine`` 隔离执行。"""
        if not command.strip():
            raise ValueError("命令不能为空")
        if policy is None:
            policy = self._default_policy()
        mode = policy.mode
        if mode == "danger-full-access":
            result = await super().execute(command, cwd=cwd, timeout_ms=timeout_ms, env=env, dsh_env=dsh_env)
            result["sandbox"] = {"mode": mode, "denied": False, "enforcement": "full"}
            return result
        if not self.ctx.has_service("sandbox"):
            raise SandboxUnavailableError(mode, "no sandbox service composed")
        confiner: Any = self.ctx.sandbox
        try:
            confined: ConfinedArgv = confiner.confine(self.argv(command), policy)
        except SandboxUnavailableError:
            raise
        except Exception as error:  # noqa: BLE001 - 运行器启动失败视为沙箱不可用
            raise SandboxUnavailableError(mode, str(error)) from error
        runner_program = confined.argv[0] if confined.argv else None
        workdir = cwd or self.cwd or (policy.workspaceRoot if policy.workspaceRoot not in (None, ".") else os.getcwd())
        try:
            result = await self._run_argv(
                command, confined.argv, workdir, self._clamp_timeout(timeout_ms),
                self._build_env(env, dsh_env), self.max_output_bytes,
            )
        except SandboxUnavailableError:
            raise
        except Exception as error:  # noqa: BLE001
            if _is_runner_spawn_failure(error, runner_program, workdir):
                raise SandboxUnavailableError(mode, str(error)) from error
            raise
        # runner 失败优先于 denial（命令未运行）。
        runner_failure = _classify_runner_failure(result["exit_code"], result["stderr"], confined.runnerFailureRules)
        if runner_failure is not None:
            raise SandboxUnavailableError(mode, runner_failure)
        denied = _matches_signature(result["exit_code"], result["stderr"], confined.denialSignatures)
        result["sandbox"] = {
            "mode": mode,
            "denied": denied,
            "enforcement": confined.enforcement,
        }
        result["command"] = command
        return result

    def _default_policy(self) -> Any:
        if self.ctx.has_service("sandboxPolicy"):
            return self.ctx.sandboxPolicy.resolve(None)
        return SandboxPolicy(mode="read-only", workspaceRoot=".")


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``pwshSandbox`` 服务（沙箱化 PowerShell 执行体）。"""
    config = config or {}
    SandboxPwshService(
        ctx,
        timeout_ms=config.get("timeoutMs", DEFAULT_TIMEOUT_MS),
        max_output_bytes=config.get("maxOutputBytes", DEFAULT_MAX_OUTPUT_BYTES),
        grace_ms=config.get("graceMs", DEFAULT_GRACE_MS),
        pwsh_path=config.get("pwshPath"),
        cwd=config.get("cwd"),
    )


apply.provides = ["pwshSandbox"]  # 声明：本插件提供 pwshSandbox 服务
apply.inject = ["subprocess", "sandbox", "sandboxPolicy"]  # 声明：封堵装配依赖

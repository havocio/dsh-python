"""沙箱化 bash 执行体（bash-sandbox，对标 dsh 的 ``@deepseek-ai/dsh-bash-sandbox``）。

注册为 ``ctx.shellSandbox``：在本地 bash 执行器（``ctx.shell``）之上，把
``['bash','-c',<command>]`` 经 ``ctx.sandbox.confine`` 包裹为隔离执行。
``mode=danger-full-access`` 直接透传（无隔离）；否则按解析策略封堵后运行。
运行中（runner spawn 失败、runner 启动失败）抛 ``SandboxUnavailableError``（fail-closed）。
沙箱事实（mode/enforcement/denial）随结果返回，供工具层渲染。

封堵分类方言（denial 签名、runner 失败规则）与 dsh 的 ``bash-sandbox/helpers.ts`` 一致：
拒绝优先于运行器失败（命令未运行）；runner 失败的致命行优先于 denial 签名。

与 dsh 差异（已注明）：
- dsh 的 ``SandboxBashExecutor`` 注册为 ``ctx.shell``（覆盖 bash-local）；dsh_py 让其注册为
  并列的 ``ctx.shellSandbox``，工具层在封堵装配下调用它、否则回退 ``ctx.shell``。
- 默认模式取自 ``ctx.sandboxPolicy.default_mode``（未挂载则为 None → 不封堵）。
- 复用 ``ShellService._run_argv`` seam 执行（``ctx.shell`` 与 ``ctx.shellSandbox`` 共享同一
  份 subprocess 生命周期代码，避免分叉）。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.services.sandbox import (
    ConfinedArgv,
    SandboxPolicy,
    SandboxUnavailableError,
)
from dsh_py.services.shell import DSH_ENV_PREFIX, ShellService

#: 运行器启动失败判定（对标 dsh helpers）：仅 ENOENT/EACCES 且 argv[0] 可溯源。
EXECUTABLE_SPAWN_CODES = {"EACCES", "ENOENT"}


def _is_usable_workdir(path: str) -> bool:
    try:
        return os.path.isdir(path) and os.access(path, os.X_OK)
    except OSError:
        return False


def _is_runner_spawn_failure(error: Any, runner_program: Optional[str], workdir: str) -> bool:
    """调用方 cwd 可用前提下，把 ENOENT/EACCES 且 argv[0] 可溯源的失败归为运行器启动失败。"""
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


class SandboxBashService(ShellService):
    """``bash`` 封堵执行体（``ctx.shellSandbox``）：在本地 bash 之上施加沙箱隔离。"""

    def __init__(self, ctx: AppContext, shell: Optional[str] = None, name: str = "shellSandbox") -> None:
        super().__init__(ctx, shell=shell, name=name)
        # 默认沙箱模式取自 ctx.sandboxPolicy（未挂载则为 None → 不封堵）。
        self.sandbox_mode: Optional[str] = None
        if ctx.has_service("sandboxPolicy"):
            self.sandbox_mode = getattr(ctx.sandboxPolicy, "default_mode", None)

    async def execute(  # type: ignore[override]
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout_ms: Optional[int] = None,
        env: Optional[dict] = None,
        policy: Any = None,
    ) -> dict:
        """运行一条命令：danger-full-access 透传；否则按 policy 经 ``ctx.sandbox.confine`` 隔离执行。"""
        if not command.strip():
            raise ValueError("命令不能为空")
        if policy is None:
            policy = self._default_policy()
        mode = policy.mode
        if mode == "danger-full-access":
            result = await super().execute(command, cwd=cwd, timeout_ms=timeout_ms, env=env)
            result["sandbox"] = {"mode": mode, "denied": False, "enforcement": "full"}
            return result
        if not self.ctx.has_service("sandbox"):
            raise SandboxUnavailableError(mode, "no sandbox service composed")
        confiner: Any = self.ctx.sandbox
        argv = self._bash_argv(command)
        try:
            confined: ConfinedArgv = confiner.confine(argv, policy)
        except SandboxUnavailableError:
            raise
        except Exception as error:  # noqa: BLE001 - 运行器启动失败视为沙箱不可用
            raise SandboxUnavailableError(mode, str(error)) from error
        runner_program = confined.argv[0] if confined.argv else None
        workdir = cwd or os.getcwd()
        # 受信任 DSH_* 快照合并（与父类 execute 一致：剥离继承的 DSH_* 再并入）。
        effective_env: Optional[dict] = None
        if env is not None:
            effective_env = {k: v for k, v in os.environ.items() if not k.startswith(DSH_ENV_PREFIX)}
            effective_env.update(env)
        try:
            result = await self._run_argv(command, tuple(confined.argv), workdir, timeout_ms, effective_env)
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

    def _bash_argv(self, command: str) -> list[str]:
        """把用户命令包装成本地 shell 的 argv（与 ``ShellService`` 一致；封堵前交给 confine）。"""
        base = os.path.basename(self._shell).lower()
        if base in ("cmd", "cmd.exe"):
            return [self._shell, "/c", command]
        return [self._shell, "-c", command]

    def _default_policy(self) -> Any:
        if self.ctx.has_service("sandboxPolicy"):
            return self.ctx.sandboxPolicy.resolve(None)
        return SandboxPolicy(mode="read-only", workspaceRoot=".")


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``shellSandbox`` 服务（沙箱化 bash 执行体）。"""
    config = config or {}
    SandboxBashService(ctx, shell=config.get("shell"), name="shellSandbox")


apply.provides = ["shellSandbox"]  # 声明：本插件提供 shellSandbox 服务
apply.inject = ["subprocess", "sandbox", "sandboxPolicy"]  # 声明：封堵装配依赖

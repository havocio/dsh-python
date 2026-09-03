"""PowerShell 本地执行后端（pwsh-local，对标 dsh 的 ``@deepseek-ai/dsh-pwsh-local``）。

注册为 ``ctx.pwsh``（与 ``ctx.shell`` 的 bash 实现并列，不冲突——dsh_py 已把
``ctx.shell`` 占为 bash，故 PowerShell 走独立 ``ctx.pwsh`` 服务，工具层按 dialect
调用，不互相覆盖）。每条命令经 ``ctx.subprocess`` 以
``pwsh -NoLogo -NoProfile -NonInteractive -Command <cmd>`` 运行；命令作为单个
argv 元素传入 ``-Command``（无中间 shell 转义层，故没有 ``bash -c`` 那样的引号域）。
固定 UTF-8 输出 preamble，并注入 ``NO_COLOR``/``PAGER``/``GIT_PAGER`` 覆盖以避免
输出乱码（Windows PowerShell 5.1 默认按 OEM 代码页写控制台，会被 preamble 纠正）。

与 dsh 差异（已注明）：
- dsh 用 ``ctx.shell`` 承载 pwsh（pwsh-local 注册为 ``ctx.shell``，覆盖 bash）；
  dsh_py 已固定 ``ctx.shell``=bash，故 pwsh 注册为并列的 ``ctx.pwsh``，封堵变体
  为 ``ctx.pwshSandbox``（见 ``pwsh_sandbox.py``）。
- 超时/输出上限/后端的语义同 dsh；``maxSpillBytes`` 由底层 ``ctx.subprocess``
  收集器默认值接管，本层不单独管理 spill 文件。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service

#: 固定 UTF-8 输出 preamble：强制 [Console]::OutputEncoding 与 $OutputEncoding 为无 BOM UTF-8。
#: 与 dsh 同源，使 Windows PowerShell 5.1 与 pwsh 7 输出一致。
ENCODING_PREAMBLE = (
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
)

#: 注入子进程的环境覆盖：禁用彩色与分页器（避免 ANSI/分页污染工具输出）。
ENV_OVERRIDES = {
    "NO_COLOR": "1",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
}

#: 终止升级宽限（毫秒）：SIGTERM → grace → SIGKILL。
DEFAULT_GRACE_MS = 3000
#: 默认前台超时（毫秒）。
DEFAULT_TIMEOUT_MS = 120_000
#: 单次调用超时上限（毫秒）：超过此值的 timeoutMs 被钳制到此处。
DEFAULT_MAX_TIMEOUT_MS = 600_000
#: 每流内存收集上限（字节）。
DEFAULT_MAX_OUTPUT_BYTES = 64_000


def candidate_pwsh_paths(env: Optional[dict] = None) -> list[str]:
    """Windows 上探测已知 PowerShell 路径（含 PATH 中各 ``pwsh.exe``），按解析顺序排列。

    内容与 dsh 的 ``candidatePwshPaths`` 一致：PowerShell 7 安装目录 → PATH 各条目
    → Windows PowerShell 5.1。显式参数化（env）使其成为纯函数，便于单测。
    """
    environ = env if env is not None else dict(os.environ)
    program_files = environ.get("ProgramFiles") or "C:\\Program Files"
    system_root = environ.get("SystemRoot") or environ.get("WINDIR") or "C:\\Windows"
    candidates: list[str] = [
        os.path.join(program_files, "PowerShell", "7", "pwsh.exe"),
    ]
    separator = ";" if (environ.get("OS") or "").upper().startswith("WINDOWS") or os.name == "nt" else os.pathsep
    for entry in (environ.get("PATH") or "").split(separator):
        trimmed = entry.strip().strip('"')
        if trimmed:
            candidates.append(os.path.join(trimmed, "pwsh.exe"))
    candidates.append(os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"))
    return candidates


def resolve_pwsh_path(
    configured: Optional[str] = None,
    env: Optional[dict] = None,
    platform: Optional[str] = None,
) -> str:
    """解析 pwsh 可执行文件路径：配置值 > 已知 Windows 路径（存在文件）> PATH(pwsh) > 'pwsh'。

    :param configured: 显式配置的可执行路径（优先，信任原样）。
    :param env: 探测用的环境字典（默认 ``os.environ``）。
    :param platform: 强制平台（默认 ``os.name`` 推导；测试可注入 ``'nt'``）。
    :returns: 可执行文件路径或裸 ``pwsh``（交 PATH 解析）。
    """
    if configured and configured.strip():
        return configured
    is_windows = (platform or os.name) == "nt"
    if is_windows:
        for candidate in candidate_pwsh_paths(env):
            if os.path.isfile(candidate):
                return candidate
    # 非 Windows：与 dsh 一致，直接返回裸 ``pwsh``，由 OS 经 PATH 在 spawn 时解析
    # （不在此处猜 PATH——Windows 的已知路径已在上一步穷举）。
    return "pwsh"


class PwshLocalService(Service):
    """``pwsh`` 本地执行后端（``ctx.pwsh``）。

    命令默认、超时、环境、cwd 由调用方指定；本实现为零依赖本地后端：优先经
    ``ctx.subprocess`` seam 执行（树级终止、有界收集、超时 kill），seam 未挂载时
    回退 ``asyncio.create_subprocess_exec``。
    """

    def __init__(
        self,
        ctx: AppContext,
        name: str = "pwsh",
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        max_timeout_ms: int = DEFAULT_MAX_TIMEOUT_MS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        grace_ms: int = DEFAULT_GRACE_MS,
        pwsh_path: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> None:
        super().__init__(ctx, name)
        self.timeout_ms = timeout_ms
        self.max_timeout_ms = max_timeout_ms
        self.max_output_bytes = max_output_bytes
        self.grace_ms = grace_ms
        self.cwd = cwd
        self._pwsh_path = pwsh_path or resolve_pwsh_path()
        # 默认沙箱模式：本地执行器无隔离，故为 None（由封堵变体覆盖）。
        self.sandbox_mode: Optional[str] = None

    @property
    def pwsh_path(self) -> str:
        """每条命令经此可执行文件运行。"""
        return self._pwsh_path

    def argv(self, command: str) -> list[str]:
        """一条命令的 pwsh 调用 argv：``[pwsh, -NoLogo, -NoProfile, -NonInteractive, -Command, <preamble+command>]``。"""
        return [
            self._pwsh_path,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"{ENCODING_PREAMBLE}{command}",
        ]

    def _clamp_timeout(self, timeout_ms: Optional[int]) -> int:
        if timeout_ms is None:
            return self.timeout_ms
        return max(1, min(int(timeout_ms), self.max_timeout_ms))

    def _build_env(self, env: Optional[dict], dsh_env: Optional[dict]) -> dict:
        """合并环境：父环境 + ENV_OVERRIDES + 调用方 env + 受信任 DSH_* 快照。

        继承的 ``DSH_*`` 一律剥离（避免嵌套 harness / 并发父子 agent 泄漏陈旧身份），
        但显式经 ``dsh_env`` 携带的 ``DSH_*`` 条目予以保留。
        """
        merged: dict = dict(os.environ)
        merged.update(ENV_OVERRIDES)
        if env:
            merged.update(env)
        if dsh_env:
            merged.update(dsh_env)
        return {k: v for k, v in merged.items() if not (k.startswith("DSH_") and k not in (dsh_env or {}))}

    async def execute(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout_ms: Optional[int] = None,
        env: Optional[dict] = None,
        dsh_env: Optional[dict] = None,
        stdout_max_bytes: Optional[int] = None,
    ) -> dict:
        """运行一条 PowerShell 命令。

        返回 ``{"command", "stdout", "stderr", "exit_code", "timed_out", "aborted", "signal"}``。
        ``exit_code`` 为 -1 表示被超时终止。``timeout_ms`` 经 ``max_timeout_ms`` 钳制。
        """
        if not command.strip():
            raise ValueError("命令不能为空")
        effective_timeout = self._clamp_timeout(timeout_ms)
        workdir = cwd or self.cwd or os.getcwd()
        effective_env = self._build_env(env, dsh_env)
        cap = stdout_max_bytes if stdout_max_bytes is not None and stdout_max_bytes > 0 else self.max_output_bytes
        if self.ctx.has_service("subprocess"):
            result = await self._run_argv(command, self.argv(command), workdir, effective_timeout, effective_env, cap)
        else:
            result = await self._run_argv_direct(command, self.argv(command), workdir, effective_timeout, effective_env, cap)
        result["command"] = command
        return result

    async def _run_argv(self, command: str, argv: list[str], cwd: str, timeout_ms: int, env: dict, cap: int) -> dict:
        """seam 路径：``ctx.subprocess.spawn`` + 收集 + 超时 ``terminate()``。"""
        from dsh_py.services.subprocess import SubprocessCollect, SubprocessSpawnSpec, SubprocessStdio

        spec = SubprocessSpawnSpec(
            argv=tuple(argv),
            cwd=cwd,
            stdio=SubprocessStdio(
                stdin="ignore",
                stdout=SubprocessCollect(maxBytes=cap),
                stderr=SubprocessCollect(maxBytes=self.max_output_bytes),
            ),
            graceMs=self.grace_ms,
            env=env,
        )
        handle = self.ctx.subprocess.spawn(spec)
        timeout = timeout_ms / 1000.0
        timed_out = False
        try:
            # shield：超时仅取消外层等待，内层 handle.done 继续；随后 terminate() 并等其解析。
            outcome = await asyncio.wait_for(asyncio.shield(handle.done), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            handle.terminate()  # 树级：SIGTERM → grace → SIGKILL（Windows taskkill /T）
            outcome = await handle.done
        stdout_text = handle.collected.stdout.read_from(0)["text"] if handle.collected.stdout is not None else ""
        stderr_text = handle.collected.stderr.read_from(0)["text"] if handle.collected.stderr is not None else ""
        exit_code = -1 if timed_out else (outcome.exitCode if outcome.exitCode is not None else -1)
        if timed_out:
            stderr_text = f"{stderr_text}\n（命令超过 {timeout_ms}ms 被终止）"
        return {
            "command": command,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "aborted": False,
            "signal": outcome.signal if not timed_out else None,
        }

    async def _run_argv_direct(self, command: str, argv: list[str], cwd: str, timeout_ms: int, env: dict, cap: int) -> dict:
        """回退路径：直接 ``create_subprocess_exec``（seam 未挂载时；保持既有行为）。"""
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        timeout = timeout_ms / 1000.0
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return {
                "command": command,
                "stdout": stdout_bytes.decode("utf-8", errors="replace"),
                "stderr": stderr_bytes.decode("utf-8", errors="replace"),
                "exit_code": proc.returncode,
                "timed_out": False,
                "aborted": False,
                "signal": None,
            }
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            stdout_bytes, stderr_bytes = await proc.communicate()
            return {
                "command": command,
                "stdout": (stdout_bytes or b"").decode("utf-8", errors="replace"),
                "stderr": (stderr_bytes or b"").decode("utf-8", errors="replace")
                          + f"\n（命令超过 {timeout_ms}ms 被终止）",
                "exit_code": -1,
                "timed_out": True,
                "aborted": False,
                "signal": None,
            }


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``pwsh`` 服务（本地 PowerShell 执行后端；``pwshPath`` 可覆盖探测）。"""
    config = config or {}
    PwshLocalService(
        ctx,
        timeout_ms=config.get("timeoutMs", DEFAULT_TIMEOUT_MS),
        max_timeout_ms=config.get("maxTimeoutMs", DEFAULT_MAX_TIMEOUT_MS),
        max_output_bytes=config.get("maxOutputBytes", DEFAULT_MAX_OUTPUT_BYTES),
        grace_ms=config.get("graceMs", DEFAULT_GRACE_MS),
        pwsh_path=config.get("pwshPath"),
        cwd=config.get("cwd"),
    )


apply.provides = ["pwsh"]  # 声明：本插件提供 pwsh 服务
apply.inject = ["subprocess"]  # 声明：优先经 ctx.subprocess seam 执行

"""Shell 服务（shell seam，对标 dsh 的 ``dsh-shell`` 本地子集）：一个执行世界的命令执行。

后端拥有 shell 探测与命令运行；超时、cwd、环境由调用方指定。本实现为零依赖
本地后端：**优先经 ``ctx.subprocess`` seam 执行**（树级终止、scrub 环境、收集
输出），seam 未挂载时回退 ``asyncio.create_subprocess_shell``（超时 kill）。

- :meth:`ShellService.execute` —— 运行一条命令，返回 ``stdout/stderr/exit_code``；
- shell 探测：Windows 优先 Git Bash（``bash``），回退 ``cmd``；POSIX 用 ``/bin/bash``。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service

#: 受管理环境变量命名空间前缀（与 shell-env 对齐；合并快照前剥离继承的该前缀变量）。
DSH_ENV_PREFIX = "DSH_"
#: seam 路径的收集上限（字节）：旧实现无界读入内存，这里用大上限保留行为。
_SHELL_COLLECT_MAX = 16 * 1024 * 1024
#: 终止升级宽限（毫秒）：SIGTERM → grace → SIGKILL。
_SHELL_GRACE_MS = 1000


class ShellService(Service):
    """``shell`` 服务：本地命令执行（``ctx.shell``）。"""

    def __init__(self, ctx: AppContext, shell: Optional[str] = None, name: str = "shell") -> None:
        super().__init__(ctx, name)
        self._shell = shell or self._default_shell()

    @staticmethod
    def _default_shell() -> str:
        if os.name == "nt":
            bash = shutil.which("bash")
            return bash or "cmd"
        return "/bin/bash"

    @property
    def shell(self) -> str:
        """当前使用的 shell 可执行文件路径。"""
        return self._shell

    def _command_line(self, command: str) -> str:
        """把用户命令包装成当前 shell 的调用行（保留：命令默认/引用语义的辅助）。"""
        if os.path.basename(self._shell).lower() in ("cmd", "cmd.exe"):
            return f'cmd /c "{command}"' if self._shell == "cmd" else f'"{self._shell}" /c "{command}"'
        return f'"{self._shell}" -c "{command.replace(chr(34), chr(92) + chr(34))}"'

    async def execute(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout_ms: Optional[int] = None,
        env: Optional[dict] = None,
    ) -> dict:
        """运行一条命令（异步 subprocess，超时 kill）。

        返回 ``{"command", "stdout", "stderr", "exit_code", "timed_out"}``。
        ``exit_code`` 为 -1 表示被超时终止。经 ``ctx.subprocess`` seam 执行时
        使用**树级**终止（超时杀整棵进程树而非只杀直接子）。
        """
        if not command.strip():
            raise ValueError("命令不能为空")
        # 提供 env（受信任 DSH_* 快照）时，合并前先剥离所有继承的 DSH_*，避免嵌套
        # harness / 并发父子 agent 泄漏陈旧身份（对齐 dsh 的本地执行器行为）。
        effective_env: Optional[dict] = None
        if env is not None:
            effective_env = {k: v for k, v in os.environ.items() if not k.startswith(DSH_ENV_PREFIX)}
            effective_env.update(env)
        if self.ctx.has_service("subprocess"):
            return await self._execute_via_subprocess(command, cwd, timeout_ms, effective_env)
        return await self._execute_direct(command, cwd, timeout_ms, effective_env)

    async def _run_argv(
        self,
        command: str,
        argv: tuple,
        cwd: Optional[str],
        timeout_ms: Optional[int],
        env: Optional[dict],
    ) -> dict:
        """seam 路径：``ctx.subprocess.spawn`` + 收集输出 + 超时 ``terminate()``。

        从具体的 ``argv`` 运行（子类封堵路径传入 ``ctx.sandbox.confine`` 返回的 argv）。
        """
        from dsh_py.services.subprocess import SubprocessCollect, SubprocessSpawnSpec, SubprocessStdio

        spec = SubprocessSpawnSpec(
            argv=argv,
            cwd=cwd or os.getcwd(),
            stdio=SubprocessStdio(
                stdin="ignore",
                stdout=SubprocessCollect(maxBytes=_SHELL_COLLECT_MAX),
                stderr=SubprocessCollect(maxBytes=_SHELL_COLLECT_MAX),
            ),
            graceMs=_SHELL_GRACE_MS,
            env=env,
        )
        handle = self.ctx.subprocess.spawn(spec)
        timeout = (timeout_ms / 1000.0) if timeout_ms is not None else None
        timed_out = False
        try:
            if timeout is None:
                outcome = await handle.done
            else:
                # shield：wait_for 超时会取消被等待的 future——终止升级仍需要
                # 这个 done 在 terminate() 之后解析。
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
        }

    async def _execute_via_subprocess(self, command: str, cwd: Optional[str], timeout_ms: Optional[int], env: Optional[dict]) -> dict:
        """seam 路径（本地 argv）：转交 :meth:`_run_argv`。"""
        base = os.path.basename(self._shell).lower()
        argv = (self._shell, "/c", command) if base in ("cmd", "cmd.exe") else (self._shell, "-c", command)
        return await self._run_argv(command, argv, cwd, timeout_ms, env)

    async def _execute_direct(self, command: str, cwd: Optional[str], timeout_ms: Optional[int], env: Optional[dict]) -> dict:
        """回退路径：直接 ``create_subprocess_shell``（seam 未挂载时；保持既有行为）。"""
        process_env = env if env is not None else dict(os.environ)
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=process_env,
        )
        timeout = (timeout_ms / 1000.0) if timeout_ms is not None else None
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
            return {
                "command": command,
                "stdout": stdout_bytes.decode("utf-8", errors="replace"),
                "stderr": stderr_bytes.decode("utf-8", errors="replace"),
                "exit_code": proc.returncode,
                "timed_out": False,
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
            }


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``shell`` 服务（本地命令执行；``shell`` 配置可覆盖探测结果）。"""
    config = config or {}
    ShellService(ctx, shell=config.get("shell"))


apply.provides = ["shell"]  # 声明：本插件提供 shell 服务

"""Shell 服务（shell seam，对标 dsh 的 ``dsh-shell`` 本地子集）：一个执行世界的命令执行。

后端拥有 shell 探测与命令运行；超时、cwd、环境由调用方指定。本实现为零依赖
本地后端：``asyncio.create_subprocess_shell`` 异步执行，超时 kill。

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


class ShellService(Service):
    """``shell`` 服务：本地命令执行（``ctx.shell``）。"""

    def __init__(self, ctx: AppContext, shell: Optional[str] = None) -> None:
        super().__init__(ctx, "shell")
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
        """把用户命令包装成当前 shell 的调用行。"""
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
        ``exit_code`` 为 -1 表示被超时终止。
        """
        if not command.strip():
            raise ValueError("命令不能为空")
        process_env = dict(os.environ)
        if env:
            process_env.update(env)
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

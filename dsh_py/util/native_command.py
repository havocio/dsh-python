"""不经 shell 运行宿主原生命令（util/native-command，对标 dsh 的 ``dsh-native-command``）。

供宿主原生 OS 集成使用（原生目录选择器、默认应用打开移交）：utf8 stdio 捕获、
中止传播、Windows 隐藏控制台。纯库：无 ctx、无状态、无事件；原生实现绝不调用
shell。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable, Optional


async def run_native_command(
    command: str,
    args: list[str],
    signal: Any = None,
) -> dict:
    """以 utf8 stdio、中止传播与 Windows 隐藏运行一条宿主命令。

    :param command: 可执行路径或 PATH 名。
    :param args: argv（绝不传 shell 字符串）。
    :param signal: 调用方/连接生命周期；中止即终止子进程。
    :returns: 退出码 0 时 ``{"stdout": str, "stderr": str}``。
    :raises: 非零退出时抛带 ``code``/``stdout``/``stderr`` 属性的异常。
    """
    kwargs: dict = {"stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.PIPE}
    if os.name == "nt":
        # Windows 隐藏控制台窗口（对齐 dsh 的 windowsHide）
        kwargs["creationflags"] = getattr(asyncio.subprocess, "CREATE_NO_WINDOW", 0)
    process = await asyncio.create_subprocess_exec(
        command, *args, **kwargs,
    )
    try:
        stdout_b, stderr_b = await process.communicate()
    except asyncio.CancelledError:
        if signal is not None and getattr(signal, "abort", None):
            signal.abort()
        process.kill()
        await process.wait()
        raise
    if signal is not None and getattr(signal, "aborted", False):
        process.kill()
        await process.wait()
        raise RuntimeError("native-command: 调用已中止")
    stdout = stdout_b.decode("utf-8", "replace") if stdout_b else ""
    stderr = stderr_b.decode("utf-8", "replace") if stderr_b else ""
    if process.returncode != 0:
        error = RuntimeError(f"{command} 退出码 {process.returncode}")
        error.code = process.returncode  # type: ignore[attr-defined]
        error.stdout = stdout  # type: ignore[attr-defined]
        error.stderr = stderr  # type: ignore[attr-defined]
        raise error
    return {"stdout": stdout, "stderr": stderr}

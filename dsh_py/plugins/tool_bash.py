"""bash 工具（tool-bash，对标 dsh 的 ``dsh-tool-bash``）：把 ``ctx.shell`` 能力
暴露为模型可调用的 ``bash`` 工具。

参数：``command``（必填）、``timeout_ms``（正数，可选）、``workdir``（可选）。
执行结果渲染为 stdout / stderr / exit_code；超时终止明确标注。
"""

from __future__ import annotations

from typing import Any

from dsh_py.core.context import AppContext

BASH_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "要执行的 shell 命令（非空）"},
        "timeout_ms": {"type": "integer", "description": "超时毫秒（正数，缺省无超时）"},
        "workdir": {"type": "string", "description": "工作目录（缺省继承当前进程）"},
    },
    "required": ["command"],
}


def _render(result: dict) -> str:
    parts: list[str] = []
    if result["stdout"]:
        parts.append(f"$ {result['command']}\n{result['stdout'].rstrip()}")
    else:
        parts.append(f"$ {result['command']}\n（无 stdout 输出）")
    if result["stderr"]:
        parts.append(f"[stderr]\n{result['stderr'].rstrip()}")
    parts.append(f"[exit code: {result['exit_code']}]")
    return "\n".join(parts)


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``bash`` 工具（经 ``ctx.shell`` 执行）。"""
    config = config or {}
    enable_background = bool(config.get("enableRunInBackground", True))

    async def bash_handler(args: dict) -> str:
        command = args.get("command", "")
        if not command.strip():
            return "错误：command 必须是非空字符串", True
        timeout_ms = args.get("timeout_ms")
        if timeout_ms is not None and (not isinstance(timeout_ms, int) or timeout_ms <= 0):
            return "错误：timeout_ms 必须是正整数", True
        workdir = args.get("workdir")
        if not enable_background and args.get("run_in_background"):
            return "错误：run_in_background 已被部署配置禁用", True
        try:
            result = await ctx.shell.execute(command, cwd=workdir, timeout_ms=timeout_ms)
            return _render(result), False
        except (ValueError, OSError) as exc:
            return f"错误：{exc}", True

    ctx.tools.register("bash", "在本地 shell 执行一条命令（返回 stdout/stderr/退出码）",
                       BASH_SCHEMA, bash_handler)


apply.provides = ["toolBash"]
apply.inject = ["tools", "shell"]

"""bash 工具（tool-bash，对标 dsh 的 ``dsh-tool-bash``）：把 ``ctx.shell`` 能力
暴露为模型可调用的 ``bash`` 工具。

参数：``command``（必填）、``timeout_ms``（正数，可选）、``workdir``（可选）、
``run_in_background``（可选布尔）。执行结果渲染为 stdout / stderr / exit_code；
超时终止明确标注。**接线整合（2026-08-24）**：前台执行经 ``ctx.shell``（其内部
走 ``ctx.subprocess`` seam）；``run_in_background: true`` 经 ``ctx.jobs`` 启动一个
由 ``ctx.subprocess`` 支撑的真实 bash 后台任务并返回 job id（需 jobs + subprocess
服务，即装配 tool-jobs 与 subprocess-local）。
"""

from __future__ import annotations

from typing import Any

from dsh_py.core.context import AppContext

BASH_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "要执行的 shell 命令（非空）"},
        "timeout_ms": {"type": "integer", "description": "超时毫秒（正数，缺省无超时；仅前台执行）"},
        "workdir": {"type": "string", "description": "工作目录（缺省继承当前进程）"},
        "run_in_background": {
            "type": "boolean",
            "description": "是否作为后台任务启动（返回 job id；用 job_output 读取）。",
        },
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
    """插件入口：注册 ``bash`` 工具（经 ``ctx.shell`` 前台执行 / ``ctx.jobs`` 后台）。"""
    config = config or {}
    enable_background = bool(config.get("enableRunInBackground", True))
    background_output_limit = config.get("backgroundOutputLimitBytes")

    async def bash_handler(args: dict, exec: dict) -> tuple:
        command = args.get("command", "")
        if not command.strip():
            return "错误：command 必须是非空字符串", True
        timeout_ms = args.get("timeout_ms")
        if timeout_ms is not None and (not isinstance(timeout_ms, int) or timeout_ms <= 0):
            return "错误：timeout_ms 必须是正整数", True
        workdir = args.get("workdir")
        run_background = bool(args.get("run_in_background"))

        if run_background:
            if not enable_background:
                return "错误：run_in_background 已被部署配置禁用", True
            if not ctx.has_service("jobs") or not ctx.has_service("subprocess"):
                return "错误：后台任务需要 jobs（tool-jobs）与 subprocess（subprocess-local）服务", True
            from dsh_py.services.jobs_local import create_bash_job_hooks

            try:
                hooks = create_bash_job_hooks(ctx, command, cwd=workdir, output_limit_bytes=background_output_limit)
                job_id = ctx.jobs.start({
                    "kind": "bash",
                    "label": command[:60],
                    "run": lambda: hooks,
                    "owner": exec.get("agent") if exec else None,
                    "outputLimitBytes": background_output_limit,
                })
                return f"bash 后台任务已启动：{job_id}（用 job_output 读取结果；完成会通知）", False
            except Exception as exc:  # noqa: BLE001 - 启动失败归为错误文本
                return f"错误：{exc}", True

        try:
            result = await ctx.shell.execute(command, cwd=workdir, timeout_ms=timeout_ms)
            return _render(result), False
        except (ValueError, OSError) as exc:
            return f"错误：{exc}", True

    ctx.tools.register("bash", "在本地 shell 执行一条命令（返回 stdout/stderr/退出码；"
                                "可后台运行返回 job id）",
                       BASH_SCHEMA, bash_handler)


apply.provides = ["toolBash"]
apply.inject = ["tools", "shell"]

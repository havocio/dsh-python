"""bash 工具（tool-bash，对标 dsh 的 ``dsh-tool-bash``）：把 ``ctx.shell`` 能力
暴露为模型可调用的 ``bash`` 工具。

参数：``command``（必填）、``timeout_ms``（正数，可选）、``workdir``（可选）、
``run_in_background``（可选布尔）、``sandbox_permissions``、``justification``（封堵装配下）。
执行结果渲染为 stdout / stderr / 沙箱拒绝标记 / 退出码；超时终止明确标注。

**接线整合（与 tool-pwsh 对称）**：
- 前台执行经 ``ctx.shell``（其内部走 ``ctx.subprocess`` seam）；封堵装配下改经
  ``ctx.shellSandbox.execute(policy=...)``，并把调用会话解析出的沙箱策略带进去。
- ``run_in_background: true`` 经 ``ctx.jobs`` 启动一个由 ``ctx.subprocess`` 支撑的真实
  bash 后台任务并返回 job id（需 jobs + subprocess 服务，即装配 tool-jobs 与 subprocess-local）。
- 封堵装配判定：同时挂载 ``shellSandbox`` 与 ``sandboxPolicy`` 才启用升级参数与封堵执行；
  升级经同步 ``approveEscalation``（需挂载 approval 服务，否则 fail-closed 报错）。
"""

from __future__ import annotations

from typing import Any

from dsh_py.core.context import AppContext
from dsh_py.services.shell_env import collect_for
from dsh_py.services.sandbox import (
    ESCALATION_TARGETS,
    SandboxPolicy,
    SandboxUnavailableError,
    approveEscalation,
    escalationHintMarker,
    sandboxDenialMarker,
    validateEscalationArgs,
)
from dsh_py.services.sandbox import EscalationApproval, EscalationRequest
from dsh_py.services.sandbox_policy import owner_of

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


def _render(result: dict, escalation_modes: list[str]) -> str:
    parts: list[str] = []
    if result["stdout"]:
        parts.append(f"$ {result['command']}\n{result['stdout'].rstrip()}")
    else:
        parts.append(f"$ {result['command']}\n（无 stdout 输出）")
    if result["stderr"]:
        parts.append(f"[stderr]\n{result['stderr'].rstrip()}")
    # 沙箱拒绝标记（模型可见，便于一致识别；与 pwsh 共用词汇）。
    sandbox = result.get("sandbox")
    if sandbox and sandbox.get("denied"):
        parts.append(sandboxDenialMarker(sandbox["mode"]))
        if escalation_modes:
            parts.append(escalationHintMarker("command"))
    parts.append(f"[exit code: {result['exit_code']}]")
    return "\n".join(parts)


def _with_mode(policy: Any, mode: str) -> Any:
    return SandboxPolicy(mode=mode, workspaceRoot=policy.workspaceRoot, sessionId=policy.sessionId)


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``bash`` 工具（经 ``ctx.shell`` 前台执行 / 封堵装配下经 ``ctx.shellSandbox``）。"""
    config = config or {}
    enable_background = bool(config.get("enableRunInBackground", True))
    background_output_limit = config.get("backgroundOutputLimitBytes")

    # 封堵装配判定：同时挂载 shellSandbox 与 sandboxPolicy 才启用升级参数与封堵执行。
    sandbox_policy_svc = ctx.sandboxPolicy if ctx.has_service("sandboxPolicy") else None
    escalation_modes: list[str] = (
        list(ESCALATION_TARGETS)
        if (sandbox_policy_svc is not None and ctx.has_service("shellSandbox"))
        else []
    )

    schema = {
        "type": "object",
        "properties": dict(BASH_SCHEMA["properties"]),
        "required": list(BASH_SCHEMA["required"]),
    }
    if enable_background:
        schema["properties"]["run_in_background"] = {
            "type": "boolean",
            "description": "后台运行并立即返回 job id（用 job_output 读取、job_kill 停止）；不施加超时。",
        }
    if escalation_modes:
        schema["properties"]["sandbox_permissions"] = {
            "type": "string",
            "enum": list(escalation_modes),
            "description": "命令刚被沙箱拒绝后一次性重试所需的更宽模式；需 justification 与用户批准。",
        }
        schema["properties"]["justification"] = {
            "type": "string",
            "description": "与 sandbox_permissions 配套：一句话说明为何该确切命令需要更宽访问。",
        }

    async def bash_handler(args: dict, exec: dict) -> tuple:
        command = args.get("command", "")
        if not command.strip():
            return "错误：command 必须是非空字符串", True
        timeout_ms = args.get("timeout_ms")
        if timeout_ms is not None and (not isinstance(timeout_ms, int) or timeout_ms <= 0):
            return "错误：timeout_ms 必须是正整数", True
        workdir = args.get("workdir")
        run_background = bool(args.get("run_in_background"))
        sandbox_permissions = args.get("sandbox_permissions")
        justification = args.get("justification")
        # 升级配对校验（schema 不校验的约束：二者须共存、均非空）。
        try:
            validateEscalationArgs(sandbox_permissions, justification)
        except ValueError as exc:
            return f"错误：{exc}", True

        dsh_env = collect_for(ctx, exec)

        # 解析策略 + 升级批准（同步 approveEscalation）。
        standing_policy = sandbox_policy_svc.resolve(owner_of(exec.get("agent"))) if sandbox_policy_svc is not None else None
        approved_mode: Any = None
        if sandbox_permissions is not None and justification is not None:
            if not escalation_modes:
                return "错误：当前装配无沙箱执行体，sandbox_permissions 不可用", True
            if not ctx.has_service("approval"):
                return f"错误：升级到 {sandbox_permissions} 需要 approval 服务，但当前未挂载", True
            effective_mode = standing_policy.mode if standing_policy is not None else "read-only"
            try:
                approved_mode = approveEscalation(
                    EscalationRequest(
                        requestedMode=sandbox_permissions,
                        justification=justification,
                        effectiveMode=effective_mode,
                        subject="command",
                    ),
                    EscalationApproval(
                        approver=ctx.approval,
                        agent=exec.get("agent"),
                        callId=exec.get("callId"),
                        toolName="bash",
                        signal=exec.get("signal"),
                    ),
                )
            except (ValueError, RuntimeError, TypeError) as exc:
                return f"错误：{exc}", True

        # 后台执行（非沙箱路径，与 pwsh 背景一致：经 ctx.jobs + create_bash_job_hooks）。
        if run_background:
            if not enable_background:
                return "错误：run_in_background 已被部署配置禁用", True
            if not ctx.has_service("jobs") or not ctx.has_service("subprocess"):
                return "错误：后台任务需要 jobs（tool-jobs）与 subprocess（subprocess-local）服务", True
            from dsh_py.services.jobs_local import create_bash_job_hooks

            try:
                hooks = create_bash_job_hooks(
                    ctx, command, cwd=workdir, output_limit_bytes=background_output_limit, env=dsh_env
                )
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

        # 前台执行：封堵装配走 shellSandbox，否则回退 shell。
        try:
            if sandbox_policy_svc is not None and ctx.has_service("shellSandbox"):
                policy = standing_policy
                if approved_mode is not None and policy is not None:
                    policy = _with_mode(policy, approved_mode)
                result = await ctx.shellSandbox.execute(
                    command, cwd=workdir, timeout_ms=timeout_ms, env=dsh_env, policy=policy
                )
            else:
                result = await ctx.shell.execute(command, cwd=workdir, timeout_ms=timeout_ms, env=dsh_env)
        except (ValueError, OSError, SandboxUnavailableError) as exc:
            return f"错误：{exc}", True
        return _render(result, escalation_modes), False

    ctx.tools.register("bash", "在本地 shell 执行一条命令（返回 stdout/stderr/退出码；"
                                "可后台运行返回 job id）",
                       schema, bash_handler)


apply.provides = ["toolBash"]
apply.inject = ["tools", "shell"]

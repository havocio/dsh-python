"""PowerShell 工具（tool-pwsh，对标 dsh 的 ``dsh-tool-pwsh``）：把 ``ctx.pwsh`` 能力暴露为
模型可调用的 ``pwsh`` 工具。

参数：``command``（必填）、``description``（必填，UI 展示）、``timeoutMs``、
``workdir``、``run_in_background``、``sandbox_permissions``、``justification``。
执行结果渲染为 stdout / stderr / 沙箱拒绝标记 / 退出码；超时终止明确标注。

**接线整合（与 tool-bash 对称）**：
- 前台执行经 ``ctx.pwsh``（其内走 ``ctx.subprocess`` seam）；封堵装配下改经
  ``ctx.pwshSandbox.execute(policy=...)``，并把调用会话解析出的沙箱策略带进去。
- ``run_in_background: true`` 经 ``ctx.jobs`` 启动一个由 ``ctx.subprocess`` 支撑的真实
  pwsh 后台任务（``create_pwsh_job_hooks``，kind='pwsh'）并返回 job id。

与 dsh 差异（已注明）：
- dsh 工具依赖 ``ctx.shell``（pwsh 已注册为 ctx.shell）；dsh_py 的 ``ctx.shell`` 是 bash，
  故本工具依赖并列的 ``ctx.pwsh``/``ctx.pwshSandbox``。
- dsh 经 ``ctx.approval`` 做升级批准；dsh_py 仅在挂载 approval 服务时支持
  ``sandbox_permissions`` 升级，否则明确报错（fail-closed）。
- 结果以纯文本渲染（与 tool-bash 一致），不返回 dsh 的结构化 JSON。
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

#: 基础 schema（sandbox_permissions/justification 在封堵装配下动态追加）。
PWSH_SCHEMA_PROPERTIES = {
    "command": {"type": "string", "description": "要执行的 PowerShell 命令（非空）。"},
    "description": {
        "type": "string",
        "description": "清晰简洁的用途描述（主动语态，5-10 词，UI 展示）。"
        '如 "List files" → "列出当前目录文件"；"Get-Process" → "列出运行中的进程"。',
    },
    "timeoutMs": {"type": "integer", "description": "超时毫秒；执行器施加默认上限并钳制，到期杀进程。"},
    "workdir": {
        "type": "string",
        "description": "该命令的工作目录；缺省取会话工作区，相对路径相对它解析。",
    },
}


def _pwsh_description(background_enabled: bool, escalation_modes: list[str]) -> str:
    background = (
        "设 ``run_in_background: true`` 可跑长命令：立即返回 job id，用 ``job_output`` 读取、"
        "``job_kill`` 停止。"
        if background_enabled
        else "后台执行不可用；长命令须在超时内完成。"
    )
    base = (
        "执行一条 PowerShell 命令（``pwsh -Command``）并返回 stdout/stderr。每次调用在全新 pwsh 进程中运行："
        "状态（cwd、变量、函数）不会跨调用保留——用 ``workdir`` 而非 ``cd``。路径用原生 Windows 形式"
        "（``C:\\...``）；用 ``$env:NAME`` 读取环境变量。非零退出以 ``[exit code: N]`` 报告。"
        "当前 harness 环境事实经受管 ``$env:DSH_*`` 变量暴露，需要时自行查看。命令可能运行在文件沙箱下；"
        "被拦截的文件操作报告为 ``[sandbox: file access denied under <mode> mode]``——那是策略拒绝而非命令错误，"
        "不要换种方式重试。超长输出截为尾部，完整输出存盘并在可用时报告路径。"
        "Windows 上被强杀的命令以 ``[exit code: 1]`` 收尾而无信号标记——按中断处理而非命令失败。 "
    ) + background
    if not escalation_modes:
        return base
    return (
        base
        + " 封堵装配下，只读 pwsh 运行于 PowerShell ConstrainedLanguage 模式、workspace-write 保持 FullLanguage"
        "（除非主机策略另有说明）。只读模式下优先用 cmdlet 与核心类型（``[string]``/``[datetime]``/``[regex]``/"
        "``[guid]``）；``[System.IO.*]::``、``[math]::``、``Add-Type``、COM 对象、反射会报“仅核心类型”错误。"
        "``-f`` 格式化、属性访问、核心 cmdlet 可用。两种受限模式下程序无法打开命名管道，经管道 stdio 捕获"
        "另一程序输出的命令会 EPERM 失败，``stdio: 'inherit'``/``'ignore'`` 不受影响。该 EPERM 是既定边界："
        "不要换方式重试——在同轮一次性升级该确切命令，或重构以避免捕获输出。命令被沙箱拒绝且更宽模式可解时，"
        "在同轮立即升级：用 ``sandbox_permissions``（够用的最窄更宽模式）加一句话 ``justification`` 重试一次。"
        "若会话表明批准提示已禁用，拒绝即为终局——不要设 ``sandbox_permissions``。切勿投机升级：须基于真实拒绝"
        "（通常即本命令刚命中者）。升级被拒对该命令为终局——停下来解释，但其他命令仍可尝试或升级。"
    )


def _render(result: dict, escalation_modes: list[str]) -> str:
    """把一个前台运行渲染为模型可见文本：stdout、标记 stderr 段、沙箱标记/退出码。"""
    out = result.get("stdout") or ""
    err = result.get("stderr") or ""
    body = out if out else "(no output)"
    if err:
        if not body.endswith("\n"):
            body += "\n"
        body += f"[stderr]\n{err}"
    markers: list[str] = []
    sandbox = result.get("sandbox")
    if sandbox and sandbox.get("denied"):
        markers.append(sandboxDenialMarker(sandbox["mode"]))
        if escalation_modes:
            markers.append(escalationHintMarker("command"))
    if result.get("timed_out"):
        markers.append("[timed out]")
    sig = result.get("signal")
    if sig is not None:
        markers.append(f"[killed by signal: {sig}]")
    elif result.get("exit_code", 0) != 0:
        markers.append(f"[exit code: {result['exit_code']}]")
    if not markers:
        return body
    if not body.endswith("\n"):
        body += "\n"
    return body + "\n".join(markers)


def _with_mode(policy: Any, mode: str) -> Any:
    return SandboxPolicy(mode=mode, workspaceRoot=policy.workspaceRoot, sessionId=policy.sessionId)


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``pwsh`` 工具（经 ``ctx.pwsh`` 前台执行 / ``ctx.jobs`` 后台）。"""
    config = config or {}
    enable_background = bool(config.get("enableRunInBackground", True))
    background_output_limit = config.get("backgroundOutputLimitBytes")

    # 封堵装配判定：同时挂载 pwshSandbox 与 sandboxPolicy 才启用升级参数与封堵执行。
    sandbox_policy_svc = ctx.sandboxPolicy if ctx.has_service("sandboxPolicy") else None
    escalation_modes: list[str] = (
        list(ESCALATION_TARGETS)
        if (sandbox_policy_svc is not None and ctx.has_service("pwshSandbox"))
        else []
    )

    schema = {
        "type": "object",
        "properties": dict(PWSH_SCHEMA_PROPERTIES),
        "required": ["command", "description"],
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

    description = _pwsh_description(enable_background, escalation_modes)

    async def pwsh_handler(args: dict, exec: dict) -> tuple:
        command = args.get("command", "")
        if not command.strip():
            return "错误：command 必须是非空字符串", True
        description_text = args.get("description", "")
        if not description_text.strip():
            return "错误：description 必须是非空字符串", True
        timeout_ms = args.get("timeoutMs")
        if timeout_ms is not None and (not isinstance(timeout_ms, int) or timeout_ms <= 0):
            return "错误：timeoutMs 必须是正整数", True
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

        # 解析策略 + 升级批准。
        standing_policy = sandbox_policy_svc.resolve(owner_of(exec.get("agent"))) if sandbox_policy_svc is not None else None
        approved_mode: Optional[str] = None
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
                        toolName="pwsh",
                        signal=exec.get("signal"),
                    ),
                )
            except (ValueError, RuntimeError, TypeError) as exc:
                return f"错误：{exc}", True

        # 后台执行。
        if run_background:
            if not enable_background:
                return "错误：run_in_background 已被部署配置禁用", True
            if not ctx.has_service("jobs") or not ctx.has_service("subprocess"):
                return "错误：后台任务需要 jobs（tool-jobs）与 subprocess（subprocess-local）服务", True
            from dsh_py.services.jobs_local import create_pwsh_job_hooks

            try:
                hooks = create_pwsh_job_hooks(
                    ctx, command, cwd=workdir, output_limit_bytes=background_output_limit, env=dsh_env
                )
                job_id = ctx.jobs.start({
                    "kind": "pwsh",
                    "label": command[:60],
                    "run": lambda: hooks,
                    "owner": exec.get("agent") if exec else None,
                    "outputLimitBytes": background_output_limit,
                })
                return f"pwsh 后台任务已启动：{job_id}（用 job_output 读取结果；完成会通知）", False
            except Exception as exc:  # noqa: BLE001 - 启动失败归为错误文本
                return f"错误：{exc}", True

        # 前台执行：封堵装配走 pwshSandbox，否则回退 pwsh。
        try:
            if sandbox_policy_svc is not None and ctx.has_service("pwshSandbox"):
                policy = standing_policy
                if approved_mode is not None and policy is not None:
                    policy = _with_mode(policy, approved_mode)
                result = await ctx.pwshSandbox.execute(
                    command, cwd=workdir, timeout_ms=timeout_ms, dsh_env=dsh_env, policy=policy
                )
            else:
                result = await ctx.pwsh.execute(command, cwd=workdir, timeout_ms=timeout_ms, dsh_env=dsh_env)
        except (ValueError, OSError, SandboxUnavailableError) as exc:
            return f"错误：{exc}", True
        return _render(result, escalation_modes), False

    ctx.tools.register("pwsh", description, schema, pwsh_handler)


apply.provides = ["toolPwsh"]
apply.inject = ["tools", "pwsh", "shellEnv"]

"""bash/shell/jobs 接线到 ``ctx.subprocess`` seam 的验证（2026-08-24 整合）。

运行：python dsh_py/tests/test_subprocess_integration.py

覆盖：shell 经 seam 执行、shell 经 seam 超时树级终止、jobs 真实 bash 任务
（completed/failed/killed）、bash 工具 run_in_background 后台任务闭环。
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.plugins.tool_bash import apply as apply_tool_bash
from dsh_py.plugins.tool_jobs import apply as apply_tool_jobs
from dsh_py.services.jobs_local import apply as apply_jobs, create_bash_job_hooks
from dsh_py.services.shell import apply as apply_shell
from dsh_py.services.subprocess_local import apply as apply_subprocess
from dsh_py.services.system_prompt import apply as apply_system_prompt


def _setup():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    apply_subprocess(ctx, {})
    apply_shell(ctx, {})
    apply_system_prompt(ctx, {})
    apply_jobs(ctx, {})
    apply_tool_jobs(ctx, {})
    apply_tool_bash(ctx, {})
    return ctx


def _sleep_command(shell: str, seconds: int = 10) -> str:
    """构造跨 shell 的睡眠命令（Git Bash 用 sleep；cmd 用 ping）。"""
    base = os.path.basename(shell).lower()
    if base in ("cmd", "cmd.exe"):
        return f"ping -n {seconds + 1} 127.0.0.1 >nul"
    return f"sleep {seconds}"


async def test_shell_via_seam():
    ctx = _setup()
    result = await ctx.shell.execute("echo shell-seam")
    assert result["exit_code"] == 0
    assert "shell-seam" in result["stdout"]
    assert not result["timed_out"]


async def test_shell_timeout_via_seam():
    ctx = _setup()
    command = _sleep_command(ctx.shell.shell)
    result = await ctx.shell.execute(command, timeout_ms=300)
    assert result["timed_out"] is True
    assert result["exit_code"] == -1
    assert "被终止" in result["stderr"]


async def test_jobs_real_bash_completed():
    ctx = _setup()
    hooks = create_bash_job_hooks(ctx, "echo job-output", cwd=os.getcwd())
    job_id = ctx.jobs.start({
        "kind": "bash", "label": "echo", "run": lambda: hooks,
        "outputLimitBytes": 4096,
    })
    snapshot = await ctx.jobs.wait(job_id, 10_000)
    assert snapshot["status"] == "completed"
    read = ctx.jobs.read(job_id)
    assert "job-output" in read["text"]


async def test_jobs_real_bash_killed():
    ctx = _setup()
    hooks = create_bash_job_hooks(ctx, _sleep_command(ctx.shell.shell))
    job_id = ctx.jobs.start({"kind": "bash", "label": "hang", "run": lambda: hooks})
    await asyncio.sleep(0.3)  # 让进程真正起来
    ctx.jobs.kill(job_id)
    snapshot = await ctx.jobs.wait(job_id, 10_000)
    assert snapshot["status"] == "killed"


async def test_bash_tool_background():
    ctx = _setup()
    text, is_error = await ctx.tools.execute(
        "bash", json.dumps({"command": "echo bg-job", "run_in_background": True}),
    )
    assert not is_error
    assert "bash-1" in text
    # 经 job_output 读结果（等待终止）
    result, err = await ctx.tools.execute(
        "job_output", json.dumps({"job_id": "bash-1", "wait": True, "timeout_ms": 10_000}),
    )
    assert not err
    assert "bg-job" in result


async def test_bash_tool_background_requires_jobs():
    # 未装配 tool-jobs（无控制器）时后台应给出明确错误
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    apply_subprocess(ctx, {})
    apply_shell(ctx, {})
    apply_tool_bash(ctx, {})
    text, is_error = await ctx.tools.execute(
        "bash", json.dumps({"command": "echo x", "run_in_background": True}),
    )
    assert is_error
    assert "jobs" in text


async def _main():
    tests = [
        test_shell_via_seam,
        test_shell_timeout_via_seam,
        test_jobs_real_bash_completed,
        test_jobs_real_bash_killed,
        test_bash_tool_background,
        test_bash_tool_background_requires_jobs,
    ]
    failures = 0
    for test in tests:
        try:
            await test()
            print(f"  ✓ {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback

            traceback.print_exc()
            print(f"  ✗ {test.__name__}: {exc}")
    print(f"subprocess-integration: {'全部通过' if failures == 0 else f'{failures} 个失败'}")
    return failures


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(_main()) else 0)

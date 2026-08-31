"""tool-bash-persistent 的验证（A 类第 4 项）。

运行：python dsh_py/tests/test_tool_bash_persistent.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.terminal import apply as apply_terminal
from dsh_py.plugins.tool_bash_persistent import (
    TRUNCATED_MESSAGE,
    apply as apply_bash_persistent,
)


def _ctx():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    apply_terminal(ctx, {})
    apply_bash_persistent(ctx, {})
    return ctx


def _fake_agent(session_id="agent-1", cwd="/tmp"):
    class _Session:
        header = type("H", (), {"cwd": cwd})()

    return type("A", (), {"id": session_id, "session": _Session()})()


async def _run(ctx, agent, command):
    text, is_error, _ = await ctx.tools.execute_with_agent("bash", json.dumps({"command": command}), agent=agent)
    return text, is_error


async def test_persistent_cwd_across_calls():
    """cwd 在两次调用间保留（cd 后第二次能看到新目录）。"""
    ctx = _ctx()
    agent = _fake_agent()
    await _run(ctx, agent, "pwd")
    await _run(ctx, agent, "mkdir -p /tmp/pb_test && cd /tmp/pb_test && pwd")
    out2, _ = await _run(ctx, agent, "pwd")
    assert "/tmp/pb_test" in out2, f"cwd 未跨调用保留: {out2!r}"
    await _run(ctx, agent, "cd / && rm -rf /tmp/pb_test")


async def test_exit_code_captured():
    """非零退出码应被捕获并展示（用 ``false`` 这种不致命的命令）。"""
    ctx = _ctx()
    agent = _fake_agent()
    out, err = await _run(ctx, agent, "false")
    assert err is False, f"非零退出不应判为工具错误: {out!r}"
    assert "exit code: 1" in out, f"未捕获退出码: {out!r}"


async def test_exit_kills_shell():
    """``exit`` 会终止持久 shell，工具报告 SHELL_EXITED 并在下次调用重建。"""
    c2 = AppContext()
    load_profile(c2, CORE_PROFILE)
    apply_terminal(c2, {})
    apply_bash_persistent(c2, {"timeoutMs": 5000})
    agent = _fake_agent()
    out, err = await _run(c2, agent, "exit 7")
    assert err is False, f"SHELL_EXITED 应是正常结果而非工具错误: {out!r}"
    assert "[SHELL_EXITED]" in out, f"未报告 shell 退出: {out!r}"
    # 下一调用应从全新 shell 开始（不再残留退出状态）
    out2, err2 = await _run(c2, agent, "echo alive")
    assert err2 is False and "alive" in out2, f"shell 未重建: {out2!r}"


async def test_env_persists_across_calls():
    """导出的环境变量跨调用保留。"""
    ctx = _ctx()
    agent = _fake_agent()
    await _run(ctx, agent, "export PB_VAR=hello")
    out, _ = await _run(ctx, agent, "echo $PB_VAR")
    assert "hello" in out, f"环境变量未跨调用保留: {out!r}"


async def test_empty_command_rejected():
    ctx = _ctx()
    agent = _fake_agent()
    out, err = await _run(ctx, agent, "   ")
    assert err is True
    assert "非空" in out


async def test_max_output_truncation():
    """超长输出按 maxOutputChars 截断。"""
    c2 = AppContext()
    load_profile(c2, CORE_PROFILE)
    apply_terminal(c2, {})
    apply_bash_persistent(c2, {"maxOutputChars": 20})
    agent = _fake_agent()
    out, _ = await _run(c2, agent, "printf '%0.sA' {1..200}")
    assert "clipped" in out or "<response clipped>" in out, out
    assert len(out) <= 20 + len(TRUNCATED_MESSAGE) + 5, f"截断长度异常: {len(out)}"


async def test_owner_isolation():
    """不同 owner 拥有不同持久 shell（env 不串台）。"""
    ctx = _ctx()
    a1 = _fake_agent(session_id="owner-A")
    a2 = _fake_agent(session_id="owner-B")
    await ctx.tools.execute_with_agent("bash", '{"command":"export PB_ISO=AAA"}', agent=a1)
    out2, _, _ = await ctx.tools.execute_with_agent("bash", '{"command":"echo $PB_ISO"}', agent=a2)
    assert "AAA" not in out2, f"owner 间环境变量串台: {out2!r}"


async def test_list_registered():
    ctx = _ctx()
    assert ctx.tools.has("bash")


async def main():
    await test_persistent_cwd_across_calls()
    await test_exit_code_captured()
    await test_exit_kills_shell()
    await test_env_persists_across_calls()
    await test_empty_command_rejected()
    await test_max_output_truncation()
    await test_owner_isolation()
    await test_list_registered()
    print("OK: tool-bash-persistent 测试通过（cwd/env 跨调用、退出码、截断、owner 隔离）")


if __name__ == "__main__":
    asyncio.run(main())

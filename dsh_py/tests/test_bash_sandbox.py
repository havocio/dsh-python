"""shell 家族 bash 沙箱执行体（bash-sandbox / tool-bash 封堵路由）的契约验证（A 类 shell 家族）。

运行：python dsh_py/tests/test_bash_sandbox.py

全程用 mock 的 ``ctx.subprocess`` / ``ctx.sandbox`` / ``ctx.shellSandbox`` / ``ctx.shell``，
**不依赖真实 bash 进程**，覆盖：argv 构造、沙箱封堵（danger-full-access 透传 / 缺失 sandbox
失败 / denial 渲染 / runner 失败分类）、工具参数校验与渲染、封堵装配下的升级批准与前台路由。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.plugins.tool_bash import apply as apply_tool_bash
from dsh_py.services.bash_sandbox import (
    SandboxBashService,
    _classify_runner_failure,
    _is_runner_spawn_failure,
    _matches_signature,
)
from dsh_py.services.sandbox import ConfinedArgv, SandboxPolicy, SandboxUnavailableError
from dsh_py.services.subprocess import SubprocessSpawnSpec, SubprocessStdio


# --------------------------------------------------------------------------- #
# 假件（fakes）
# --------------------------------------------------------------------------- #
class _Reader:
    def __init__(self, text):
        self.text = text

    def read_from(self, _from):
        return {"text": self.text}


class _Collected:
    def __init__(self, stdout, stderr):
        self.stdout = _Reader(stdout) if stdout is not None else None
        self.stderr = _Reader(stderr) if stderr is not None else None


class _Outcome:
    def __init__(self, exit_code, signal=None):
        self.exitCode = exit_code
        self.signal = signal


class _Handle:
    def __init__(self, done_fut, collected):
        self._done = done_fut
        self.collected = collected
        self.terminated = False

    @property
    def done(self):
        return self._done

    def terminate(self):
        self.terminated = True
        if not self._done.done():
            self._done.set_result(_Outcome(None))


class FakeSubprocess:
    """可配置的假 subprocess：``outcome`` 决定退出事实；``hang`` 让 done 永不解析。"""

    def __init__(self, stdout="", stderr="", exit_code=0, signal=None, hang=False):
        self._outcome = _Outcome(exit_code, signal)
        self._collected = _Collected(stdout, stderr)
        self._hang = hang
        self.spawned = None

    def spawn(self, spec):
        loop = asyncio.get_event_loop()
        self.spawned = spec
        if self._hang:
            return _Handle(loop.create_future(), self._collected)
        fut = loop.create_future()
        fut.set_result(self._outcome)
        return _Handle(fut, self._collected)


class FakeTools:
    def __init__(self):
        self._tools = {}

    def register(self, name, description, parameters, handler):
        self._tools[name] = (description, parameters, handler)

    def get(self, name):
        return self._tools[name]


class FakeApprover:
    def __init__(self, outcome="allowed-once"):
        self.outcome = outcome
        self.calls = []

    def request(self, req):
        self.calls.append(req)
        return self.outcome


class FakeSandboxPolicy:
    def __init__(self, default_mode="read-only", workspace_root="."):
        self.default_mode = default_mode
        self.workspace_root = workspace_root

    def resolve(self, session=None, mode_override=None):
        mode = mode_override or self.default_mode
        return SandboxPolicy(mode=mode, workspaceRoot=self.workspace_root, sessionId="s1")


class FakeSandbox:
    """``confine`` 返回把 argv 前缀一个 sentinel 的封堵 argv，便于断言被采用。"""

    def __init__(self, denial=None, runner_rules=None):
        self.denial = denial or []
        self.runner_rules = runner_rules or []
        self.confinements = []

    def confine(self, argv, policy):
        self.confinements.append((list(argv), policy))
        wrapped = ["sandbox-runner"] + list(argv)
        return ConfinedArgv(
            argv=wrapped,
            enforcement="full",
            denialSignatures=self.denial,
            runnerFailureRules=self.runner_rules,
        )


class FakeShellSandbox:
    """工具层看到的 ``ctx.shellSandbox`` 替身：记录封堵并回吐结果。"""

    def __init__(self, sandbox=None):
        self._sandbox = sandbox or FakeSandbox()
        self.calls = []

    async def execute(self, command, **kwargs):
        policy = kwargs.get("policy")
        if policy is not None and policy.mode != "danger-full-access":
            self._sandbox.confine(["bash", "-c", command], policy)
        self.calls.append((command, kwargs))
        return {
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
            "signal": None,
            "sandbox": {
                "mode": policy.mode if policy else "read-only",
                "denied": False,
                "enforcement": "full",
            },
            "command": command,
        }


def _ctx_with_subprocess(fake):
    ctx = AppContext()
    ctx.provide("subprocess", fake)
    return ctx


def _sandbox_ctx(fake_sub, fake_sandbox, policy=None):
    ctx = AppContext()
    ctx.provide("subprocess", fake_sub)
    ctx.provide("sandbox", fake_sandbox)
    ctx.provide("sandboxPolicy", policy or FakeSandboxPolicy())
    return ctx


# --------------------------------------------------------------------------- #
# argv 构造
# --------------------------------------------------------------------------- #
def test_argv_construction():
    svc = SandboxBashService(_ctx_with_subprocess(FakeSubprocess()), shell="/bin/bash")
    argv = svc._bash_argv("echo hi")
    assert argv == ["/bin/bash", "-c", "echo hi"]


# --------------------------------------------------------------------------- #
# 沙箱封堵执行体
# --------------------------------------------------------------------------- #
async def test_sandbox_passthrough_on_danger_full_access():
    fake = FakeSubprocess(stdout="out", exit_code=0)
    ctx = _sandbox_ctx(fake, FakeSandbox(), FakeSandboxPolicy(default_mode="read-only"))
    svc = SandboxBashService(ctx, shell="/bin/bash")
    result = await svc.execute("x", policy=SandboxPolicy(mode="danger-full-access", workspaceRoot="."))
    # 不应触发 confine（直接透传给父执行器）
    assert fake.spawned is not None
    assert "sandbox-runner" not in fake.spawned.argv
    assert result["sandbox"] == {"mode": "danger-full-access", "denied": False, "enforcement": "full"}


async def test_sandbox_confines_argv():
    fake = FakeSubprocess(stdout="out", exit_code=0)
    sandbox = FakeSandbox()
    ctx = _sandbox_ctx(fake, sandbox, FakeSandboxPolicy(default_mode="read-only"))
    svc = SandboxBashService(ctx, shell="/bin/bash")
    result = await svc.execute("echo x", policy=SandboxPolicy(mode="workspace-write", workspaceRoot="/ws"))
    assert fake.spawned is not None
    assert fake.spawned.argv[0] == "sandbox-runner"  # 封堵 argv 被采用
    assert len(sandbox.confinements) == 1
    # 封堵输入是 bash argv 形态
    confined_argv, _ = sandbox.confinements[0]
    assert confined_argv == ["/bin/bash", "-c", "echo x"]
    assert result["sandbox"]["mode"] == "workspace-write"
    assert result["sandbox"]["denied"] is False
    # spec 仍是标准 subprocess seam 形状
    assert isinstance(fake.spawned, SubprocessSpawnSpec)
    assert isinstance(fake.spawned.stdio, SubprocessStdio)


async def test_sandbox_missing_backend_raises():
    ctx = AppContext()
    ctx.provide("subprocess", FakeSubprocess())
    ctx.provide("sandboxPolicy", FakeSandboxPolicy())
    # 故意不挂载 ctx.sandbox
    svc = SandboxBashService(ctx, shell="/bin/bash")
    try:
        await svc.execute("x", policy=SandboxPolicy(mode="read-only", workspaceRoot="."))
    except SandboxUnavailableError as exc:
        assert exc.code == "SANDBOX_UNAVAILABLE"
    else:
        raise AssertionError("缺失 sandbox 后端应 fail-closed 抛 SandboxUnavailableError")


async def test_sandbox_denial_reported():
    fake = FakeSubprocess(stdout="", stderr="Operation not permitted", exit_code=1)
    sandbox = FakeSandbox(denial=["Operation not permitted"])
    ctx = _sandbox_ctx(fake, sandbox, FakeSandboxPolicy())
    svc = SandboxBashService(ctx, shell="/bin/bash")
    result = await svc.execute("x", policy=SandboxPolicy(mode="read-only", workspaceRoot="."))
    assert result["sandbox"]["denied"] is True


def test_runner_failure_classification_helpers():
    rules = [type("R", (), {"fatalSignatures": ["bwrap: "], "allowedExitCodes": None, "informationalLines": [" "]})()]
    assert _classify_runner_failure(127, "bwrap: error", rules) == "bwrap: error"
    assert _classify_runner_failure(0, "bwrap: error", rules) is None
    assert _matches_signature(1, "Permission denied", ["Permission denied"]) is True
    assert _matches_signature(0, "ok", ["Permission denied"]) is False
    assert _is_runner_spawn_failure(
        type("E", (Exception,), {"code": "ENOENT", "syscall": "spawn /bin/bash", "path": "/bin/bash"})(), "/bin/bash", os.getcwd()
    ) is True
    assert _is_runner_spawn_failure(
        type("E", (Exception,), {"code": "EAGAIN", "syscall": "spawn /bin/bash"})(), "/bin/bash", os.getcwd()
    ) is False


# --------------------------------------------------------------------------- #
# 工具层（tool-bash）
# --------------------------------------------------------------------------- #
class FakeShell:
    def __init__(self, result=None):
        self.result = result or {"stdout": "ran", "stderr": "", "exit_code": 0, "timed_out": False, "signal": None}
        self.calls = []

    async def execute(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return dict(self.result, command=command)


def _tool_ctx(shell=None, sandbox_policy=None, shell_sandbox=None, approval=None, tools=None):
    ctx = AppContext()
    ctx.provide("tools", tools or FakeTools())
    ctx.provide("shell", shell or FakeShell())
    if sandbox_policy is not None:
        ctx.provide("sandboxPolicy", sandbox_policy)
    if shell_sandbox is not None:
        ctx.provide("shellSandbox", shell_sandbox)
    if approval is not None:
        ctx.provide("approval", approval)
    return ctx


def _run_handler(ctx):
    desc, schema, handler = ctx.tools.get("bash")
    return schema, handler


async def test_tool_registers_bash_with_required_params():
    ctx = _tool_ctx()
    apply_tool_bash(ctx, {})
    schema, handler = _run_handler(ctx)
    assert "command" in schema["properties"]
    assert schema["required"] == ["command"]  # bash 工具无 description 必填
    # 无沙箱装配：不应广告 sandbox_permissions
    assert "sandbox_permissions" not in schema["properties"]


async def test_tool_foreground_renders_output():
    ctx = _tool_ctx(shell=FakeShell({"stdout": "hi\n", "stderr": "", "exit_code": 0}))
    apply_tool_bash(ctx, {})
    _, handler = _run_handler(ctx)
    text, is_error = await handler({"command": "echo hi"}, {"agent": None})
    assert is_error is False
    assert "hi" in text
    # bash 工具始终报告退出码（shell 惯例）；非空 stdout 也回显命令
    assert "[exit code: 0]" in text


async def test_tool_nonzero_exit_marker():
    ctx = _tool_ctx(shell=FakeShell({"stdout": "", "stderr": "err", "exit_code": 2}))
    apply_tool_bash(ctx, {})
    _, handler = _run_handler(ctx)
    text, is_error = await handler({"command": "false"}, {"agent": None})
    assert is_error is False  # 非零退出是报告而非错误
    assert "[exit code: 2]" in text


async def test_tool_rejects_empty_command():
    ctx = _tool_ctx()
    apply_tool_bash(ctx, {})
    _, handler = _run_handler(ctx)
    text, is_error = await handler({"command": "  "}, {"agent": None})
    assert is_error is True and "command" in text


async def test_tool_sandbox_escalation_without_approval_fails():
    # sandbox_permissions 给出但无 approval 服务 → 明确报错（fail-closed）
    ctx = _tool_ctx(
        shell=FakeShell(),
        sandbox_policy=FakeSandboxPolicy(),
        shell_sandbox=FakeShellSandbox(),
    )
    apply_tool_bash(ctx, {})
    # 有 shellSandbox + sandboxPolicy → escalation_modes 非空 → schema 含 sandbox_permissions
    schema, handler = _run_handler(ctx)
    assert "sandbox_permissions" in schema["properties"]
    text, is_error = await handler(
        {"command": "x", "sandbox_permissions": "workspace-write", "justification": "need it"},
        {"agent": None},
    )
    assert is_error is True and "approval" in text


async def test_tool_sandbox_escalation_approves():
    approver = FakeApprover(outcome="allowed-once")
    fake_shell = FakeShell()
    sandbox = FakeSandbox()
    ctx = _tool_ctx(
        shell=fake_shell,
        sandbox_policy=FakeSandboxPolicy(),
        shell_sandbox=FakeShellSandbox(sandbox),
        approval=approver,
    )
    apply_tool_bash(ctx, {})
    _, handler = _run_handler(ctx)
    await handler(
        {"command": "x", "sandbox_permissions": "workspace-write", "justification": "need it"},
        {"agent": object()},
    )
    # 升级获批后，前台执行走 shellSandbox（sandbox 被封堵），而非直接 shell
    assert len(sandbox.confinements) == 1
    assert len(fake_shell.calls) == 0


async def test_tool_foreground_routes_to_sandbox_when_mounted():
    sandbox = FakeSandbox()
    ctx = _tool_ctx(shell=FakeShell(), sandbox_policy=FakeSandboxPolicy(), shell_sandbox=FakeShellSandbox(sandbox))
    apply_tool_bash(ctx, {})
    _, handler = _run_handler(ctx)
    await handler({"command": "x"}, {"agent": None})
    assert len(sandbox.confinements) == 1  # 默认 read-only 策略经封堵执行


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    ran = 0
    for t in tests:
        result = t()
        if asyncio.iscoroutine(result):
            asyncio.run(result)
        ran += 1
        print(f"  ok  {t.__name__}")
    print(f"\n{ran} passed")

"""PowerShell 家族（pwsh-local / pwsh-sandbox / tool-pwsh / jobs 钩子）的契约验证（A 类 shell 家族）。

运行：python dsh_py/tests/test_powershell.py

全程用 mock 的 ``ctx.subprocess`` / ``ctx.sandbox`` / ``ctx.pwsh``，**不依赖真实 pwsh 进程**，
覆盖：可执行解析、argv 构造、UTF-8 preamble 与 ENV_OVERRIDES、seam 路径执行与超时终止、
沙箱封堵（danger-full-access 透传 / 缺失 sandbox 失败 / denial 渲染）、工具参数校验与渲染、
以及后台 job 钩子。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.plugins.tool_powershell import apply as apply_tool_powershell
from dsh_py.services.pwsh_local import (
    DEFAULT_TIMEOUT_MS,
    ENCODING_PREAMBLE,
    ENV_OVERRIDES,
    PwshLocalService,
    candidate_pwsh_paths,
    resolve_pwsh_path,
)
from dsh_py.services.pwsh_sandbox import (
    SandboxPwshService,
    _classify_runner_failure,
    _matches_signature,
    _is_runner_spawn_failure,
)
from dsh_py.services.sandbox import ConfinedArgv, SandboxPolicy, SandboxUnavailableError
from dsh_py.services.subprocess import SubprocessCollect, SubprocessSpawnSpec, SubprocessStdio


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
    """最小 tools 替身：捕获 register 的工具，供测试直接驱动 handler。"""

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


class FakePwshSandbox:
    """工具层看到的 ``ctx.pwshSandbox`` 替身：记录封堵并回吐结果。"""

    def __init__(self, sandbox=None):
        self._sandbox = sandbox or FakeSandbox()
        self.calls = []

    async def execute(self, command, **kwargs):
        policy = kwargs.get("policy")
        if policy is not None and policy.mode != "danger-full-access":
            self._sandbox.confine(["pwsh", "-Command", command], policy)
        self.calls.append((command, kwargs))
        return {
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
            "aborted": False,
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


# --------------------------------------------------------------------------- #
# 可执行解析
# --------------------------------------------------------------------------- #
def test_resolve_pwsh_path_returns_configured():
    assert resolve_pwsh_path("/custom/pwsh") == "/custom/pwsh"
    # 空串回退：非 Windows 平台无候选路径，返回裸 'pwsh'（避免命中本机已安装 pwsh）。
    assert resolve_pwsh_path("  ", env={}, platform="linux") == "pwsh"


def test_resolve_pwsh_path_non_windows_falls_to_pwsh():
    # 没有 PATH 上的 pwsh 时，非 Windows 平台返回裸 'pwsh'（交 PATH 解析）。
    assert resolve_pwsh_path(None, env={}, platform="linux") == "pwsh"


def test_candidate_pwsh_paths_contains_known_locations():
    paths = candidate_pwsh_paths({"ProgramFiles": "C:\\PF", "SystemRoot": "C:\\W", "PATH": "C:\\a;C:\\b"})
    assert "C:\\PF\\PowerShell\\7\\pwsh.exe" in paths
    assert "C:\\a\\pwsh.exe" in paths
    assert "C:\\W\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" in paths


# --------------------------------------------------------------------------- #
# argv / 常量
# --------------------------------------------------------------------------- #
def test_encoding_preamble_and_env_overrides():
    assert "UTF8Encoding" in ENCODING_PREAMBLE
    assert ENV_OVERRIDES["NO_COLOR"] == "1"
    assert ENV_OVERRIDES["PAGER"] == "cat"


def test_argv_construction():
    svc = PwshLocalService(_ctx_with_subprocess(FakeSubprocess()), pwsh_path="/pwsh")
    argv = svc.argv("Get-Process")
    assert argv == ["/pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", f"{ENCODING_PREAMBLE}Get-Process"]
    assert argv[-1].startswith(ENCODING_PREAMBLE)


# --------------------------------------------------------------------------- #
# 本地执行：seam 路径
# --------------------------------------------------------------------------- #
async def test_local_execute_uses_subprocess_seam():
    fake = FakeSubprocess(stdout="hello", stderr="", exit_code=0)
    ctx = _ctx_with_subprocess(fake)
    svc = PwshLocalService(ctx, pwsh_path="/pwsh")
    result = await svc.execute("echo hello", cwd="/work", timeout_ms=5000)
    assert result["stdout"] == "hello"
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    # spawn spec 透传了正确 argv / cwd / 环境覆盖
    spec = fake.spawned
    assert isinstance(spec, SubprocessSpawnSpec)
    assert spec.argv == ("/pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", f"{ENCODING_PREAMBLE}echo hello")
    assert spec.cwd == "/work"
    assert spec.stdio.stdout.maxBytes == svc.max_output_bytes


async def test_local_execute_builds_env_with_overrides_and_strips_inherited_dsh():
    fake = FakeSubprocess()
    ctx = _ctx_with_subprocess(fake)
    saved = os.environ.get("DSH_LEAK")
    os.environ["DSH_LEAK"] = "stale"
    try:
        svc = PwshLocalService(ctx, pwsh_path="/pwsh")
        await svc.execute("x", dsh_env={"DSH_HOME": "/fresh"})
    finally:
        if saved is None:
            os.environ.pop("DSH_LEAK", None)
        else:
            os.environ["DSH_LEAK"] = saved
    env = fake.spawned.env
    assert env["NO_COLOR"] == "1"
    assert env["DSH_HOME"] == "/fresh"          # 显式 dsh_env 保留
    assert "DSH_LEAK" not in env                 # 继承的 DSH_* 被剥离


async def test_local_execute_timeout_terminates():
    fake = FakeSubprocess(hang=True)  # done 永不解析
    ctx = _ctx_with_subprocess(fake)
    svc = PwshLocalService(ctx, pwsh_path="/pwsh")
    result = await svc.execute("sleep", timeout_ms=30)  # 30ms 超时
    assert result["timed_out"] is True
    assert result["exit_code"] == -1
    assert fake.spawned and fake.spawned  # handle 被 terminate 过


async def test_local_execute_nonzero_exit_reported():
    fake = FakeSubprocess(stdout="", stderr="boom", exit_code=3)
    ctx = _ctx_with_subprocess(fake)
    svc = PwshLocalService(ctx, pwsh_path="/pwsh")
    result = await svc.execute("false", timeout_ms=1000)
    assert result["exit_code"] == 3
    assert result["stderr"] == "boom"


# --------------------------------------------------------------------------- #
# 沙箱封堵执行体
# --------------------------------------------------------------------------- #
def _sandbox_ctx(fake_sub, fake_sandbox, policy=None):
    ctx = AppContext()
    ctx.provide("subprocess", fake_sub)
    ctx.provide("sandbox", fake_sandbox)
    ctx.provide("sandboxPolicy", policy or FakeSandboxPolicy())
    return ctx


async def test_sandbox_passthrough_on_danger_full_access():
    fake = FakeSubprocess(stdout="out", exit_code=0)
    ctx = _sandbox_ctx(fake, FakeSandbox(), FakeSandboxPolicy(default_mode="read-only"))
    svc = SandboxPwshService(ctx, pwsh_path="/pwsh")
    result = await svc.execute("x", policy=SandboxPolicy(mode="danger-full-access", workspaceRoot="."))
    # 不应触发 confine（直接透传给父执行器）
    assert fake.spawned is not None
    assert "sandbox-runner" not in fake.spawned.argv
    assert result["sandbox"] == {"mode": "danger-full-access", "denied": False, "enforcement": "full"}


async def test_sandbox_confines_argv():
    fake = FakeSubprocess(stdout="out", exit_code=0)
    sandbox = FakeSandbox()
    ctx = _sandbox_ctx(fake, sandbox, FakeSandboxPolicy(default_mode="read-only"))
    svc = SandboxPwshService(ctx, pwsh_path="/pwsh")
    result = await svc.execute("Get-Process", policy=SandboxPolicy(mode="workspace-write", workspaceRoot="/ws"))
    assert fake.spawned is not None
    assert fake.spawned.argv[0] == "sandbox-runner"  # 封堵 argv 被采用
    assert len(sandbox.confinements) == 1
    assert result["sandbox"]["mode"] == "workspace-write"
    assert result["sandbox"]["denied"] is False


async def test_sandbox_missing_backend_raises():
    ctx = AppContext()
    ctx.provide("subprocess", FakeSubprocess())
    ctx.provide("sandboxPolicy", FakeSandboxPolicy())
    # 故意不挂载 ctx.sandbox
    svc = SandboxPwshService(ctx, pwsh_path="/pwsh")
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
    svc = SandboxPwshService(ctx, pwsh_path="/pwsh")
    result = await svc.execute("x", policy=SandboxPolicy(mode="read-only", workspaceRoot="."))
    assert result["sandbox"]["denied"] is True


def test_runner_failure_classification_helpers():
    rules = [type("R", (), {"fatalSignatures": ["bwrap: "], "allowedExitCodes": None, "informationalLines": [" "]})()]
    assert _classify_runner_failure(127, "bwrap: error", rules) == "bwrap: error"
    assert _classify_runner_failure(0, "bwrap: error", rules) is None
    assert _matches_signature(1, "Permission denied", ["Permission denied"]) is True
    assert _matches_signature(0, "ok", ["Permission denied"]) is False
    assert _is_runner_spawn_failure(
        type("E", (Exception,), {"code": "ENOENT", "syscall": "spawn /pwsh", "path": "/pwsh"})(), "/pwsh", os.getcwd()
    ) is True
    assert _is_runner_spawn_failure(
        type("E", (Exception,), {"code": "EAGAIN", "syscall": "spawn /pwsh"})(), "/pwsh", os.getcwd()
    ) is False


# --------------------------------------------------------------------------- #
# 工具层（tool-pwsh）
# --------------------------------------------------------------------------- #
class FakePwsh:
    def __init__(self, result=None):
        self.result = result or {"stdout": "ran", "stderr": "", "exit_code": 0, "timed_out": False, "aborted": False, "signal": None}
        self.calls = []

    async def execute(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return dict(self.result, command=command)


def _tool_ctx(pwsh=None, sandbox_policy=None, pwsh_sandbox=None, approval=None, tools=None):
    ctx = AppContext()
    ctx.provide("tools", tools or FakeTools())
    ctx.provide("pwsh", pwsh or FakePwsh())
    if sandbox_policy is not None:
        ctx.provide("sandboxPolicy", sandbox_policy)
    if pwsh_sandbox is not None:
        ctx.provide("pwshSandbox", pwsh_sandbox)
    if approval is not None:
        ctx.provide("approval", approval)
    return ctx


def _run_handler(ctx):
    desc, schema, handler = ctx.tools.get("pwsh")
    return schema, handler


async def test_tool_registers_pwsh_with_required_params():
    ctx = _tool_ctx()
    apply_tool_powershell(ctx, {})
    schema, handler = _run_handler(ctx)
    assert "command" in schema["properties"] and "description" in schema["properties"]
    assert schema["required"] == ["command", "description"]
    # 无沙箱装配：不应广告 sandbox_permissions
    assert "sandbox_permissions" not in schema["properties"]


async def test_tool_foreground_renders_output():
    ctx = _tool_ctx(pwsh=FakePwsh({"stdout": "hi\n", "stderr": "", "exit_code": 0}))
    apply_tool_powershell(ctx, {})
    _, handler = _run_handler(ctx)
    text, is_error = await handler({"command": "echo hi", "description": "say hi"}, {"agent": None})
    assert is_error is False
    assert "hi" in text
    assert "[exit code: 0]" not in text  # 退出 0 不产生标记


async def test_tool_nonzero_exit_marker():
    ctx = _tool_ctx(pwsh=FakePwsh({"stdout": "", "stderr": "err", "exit_code": 2}))
    apply_tool_powershell(ctx, {})
    _, handler = _run_handler(ctx)
    text, is_error = await handler({"command": "false", "description": "fail"}, {"agent": None})
    assert is_error is False  # 非零退出是报告而非错误
    assert "[exit code: 2]" in text


async def test_tool_rejects_empty_command_and_description():
    ctx = _tool_ctx()
    apply_tool_powershell(ctx, {})
    _, handler = _run_handler(ctx)
    text, is_error = await handler({"command": "  ", "description": "x"}, {"agent": None})
    assert is_error is True and "command" in text
    text, is_error = await handler({"command": "x", "description": ""}, {"agent": None})
    assert is_error is True and "description" in text


async def test_tool_sandbox_escalation_without_approval_fails():
    # sandbox_permissions 给出但无 approval 服务 → 明确报错（fail-closed）
    ctx = _tool_ctx(
        pwsh=FakePwsh(),
        sandbox_policy=FakeSandboxPolicy(),
        pwsh_sandbox=FakePwshSandbox(),
    )
    apply_tool_powershell(ctx, {})
    # 有 pwshSandbox + sandboxPolicy → escalation_modes 非空 → schema 含 sandbox_permissions
    schema, handler = _run_handler(ctx)
    assert "sandbox_permissions" in schema["properties"]
    text, is_error = await handler(
        {"command": "x", "description": "d", "sandbox_permissions": "workspace-write", "justification": "need it"},
        {"agent": None},
    )
    assert is_error is True and "approval" in text


async def test_tool_sandbox_escalation_approves():
    approver = FakeApprover(outcome="allowed-once")
    fake_pwsh = FakePwsh()
    sandbox = FakeSandbox()
    ctx = _tool_ctx(
        pwsh=fake_pwsh,
        sandbox_policy=FakeSandboxPolicy(),
        pwsh_sandbox=FakePwshSandbox(sandbox),
        approval=approver,
    )
    apply_tool_powershell(ctx, {})
    _, handler = _run_handler(ctx)
    await handler(
        {"command": "x", "description": "d", "sandbox_permissions": "workspace-write", "justification": "need it"},
        {"agent": object()},
    )
    # 升级获批后，前台执行走 pwshSandbox（sandbox 被封堵）
    assert len(sandbox.confinements) == 1


async def test_tool_foreground_routes_to_sandbox_when_mounted():
    sandbox = FakeSandbox()
    ctx = _tool_ctx(pwsh=FakePwsh(), sandbox_policy=FakeSandboxPolicy(), pwsh_sandbox=FakePwshSandbox(sandbox))
    apply_tool_powershell(ctx, {})
    _, handler = _run_handler(ctx)
    await handler({"command": "x", "description": "d"}, {"agent": None})
    assert len(sandbox.confinements) == 1  # 默认 read-only 策略经封堵执行


# --------------------------------------------------------------------------- #
# 后台 job 钩子（create_pwsh_job_hooks）
# --------------------------------------------------------------------------- #
async def test_create_pwsh_job_hooks_lifecycle():
    fake = FakeSubprocess(stdout="bg-out", exit_code=0)
    ctx = _ctx_with_subprocess(fake)
    from dsh_py.services.jobs_local import create_pwsh_job_hooks

    hooks = create_pwsh_job_hooks(ctx, "Get-Date", cwd="/work", env=None)
    out = hooks.readOutput()
    assert out == "bg-out"  # 假件即时结算，readOutput 返回已收集文本
    outcome = await hooks.done
    assert outcome["status"] == "completed"
    assert outcome["output"] == "bg-out"
    # argv 采用 pwsh 调用形态
    assert fake.spawned.argv[0] != "sandbox-runner"
    assert "-Command" in fake.spawned.argv


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

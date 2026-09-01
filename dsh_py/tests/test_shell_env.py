"""shell-env（``ctx.shellEnv`` 受信任 ``DSH_*`` 注册表）的验证（A 类 shell 家族）。

运行：python dsh_py/tests/test_shell_env.py
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.plugins.tool_bash import apply as apply_tool_bash
from dsh_py.services.shell import apply as apply_shell
from dsh_py.services.shell_env import (
    DSH_HOME_ENV,
    DSH_SESSION_ID_KEY,
    DSH_SESSION_JSONL_KEY,
    DSH_SHELL_KEY,
    BashEnvContributor,
    BashEnvVariable,
    ShellEnvRegistry,
    apply as apply_shell_env,
    collect_for,
    merge_env,
    resolve_dsh_home,
)
from dsh_py.services.session_persistence import apply as apply_persistence


def _abs(path):
    """按注册表同款规范化（Windows 上 ``abspath`` 会补盘符）。"""
    return os.path.normpath(os.path.abspath(path))


def _agent(session_id="agent-1", cwd="/tmp"):
    """最小 agent 替身：只需 ``session.header.id``。"""
    header = type("H", (), {"id": session_id, "cwd": cwd})()
    session = type("S", (), {"header": header})()
    return type("A", (), {"id": session_id, "session": session})()


def _contributor(name, key, description="test variable", value_fn=None):
    return BashEnvContributor(
        name=name,
        variables={key: BashEnvVariable(description)},
        resolve=(value_fn or (lambda _execution: {key: "resolved"})),
    )


# --------------------------------------------------------------------------- #
# 内置事实
# --------------------------------------------------------------------------- #
def test_collect_builtin_facts():
    """无 agent 时只收集注册表自身的 shell 事实，且按键排序。"""
    ctx = AppContext()
    apply_shell_env(ctx, {"dshHome": "/custom/dsh"})
    snapshot = ctx.shellEnv.collect(None)
    assert snapshot == {DSH_HOME_ENV: _abs("/custom/dsh"), DSH_SHELL_KEY: "1"}, snapshot
    assert list(snapshot) == sorted(snapshot), "快照应按键排序（稳定输出）"


def test_collect_includes_session_id_when_agent_present():
    """有调用方 agent 时补上 ``DSH_SESSION_ID``。"""
    ctx = AppContext()
    apply_shell_env(ctx, {})
    snapshot = ctx.shellEnv.collect({"agent": _agent("sess-42")})
    assert snapshot[DSH_SESSION_ID_KEY] == "sess-42", snapshot
    assert snapshot[DSH_SHELL_KEY] == "1", snapshot


def test_default_dsh_home_falls_back_to_user_home():
    """未配置且环境未设置时回退 ``~/.dsh``，绝不解析到当前工作目录。"""
    home = resolve_dsh_home(None, {})
    assert home == os.path.normpath(os.path.join(os.path.expanduser("~"), ".dsh")), home
    assert os.path.isabs(home)


def test_resolve_dsh_home_priority_and_blank_env():
    """优先级：显式配置 > ``$DSH_HOME`` > ``~/.dsh``；空白 ``$DSH_HOME`` 视为未设置。"""
    assert resolve_dsh_home("/cfg/dsh", {DSH_HOME_ENV: "/env/dsh"}) == _abs("/cfg/dsh")
    assert resolve_dsh_home(None, {DSH_HOME_ENV: "/env/dsh"}) == _abs("/env/dsh")
    assert resolve_dsh_home(None, {DSH_HOME_ENV: "   "}) == _abs(os.path.join(os.path.expanduser("~"), ".dsh"))


# --------------------------------------------------------------------------- #
# 贡献者注册与收集
# --------------------------------------------------------------------------- #
def test_register_and_collect_contributor():
    """注册的贡献者变量会被收集，且由 ``list()`` 可枚举。"""
    ctx = AppContext()
    apply_shell_env(ctx, {})
    ctx.shellEnv.register(_contributor("demo", "DSH_DEMO", "a demo var", lambda _e: {"DSH_DEMO": "v"}))

    snapshot = ctx.shellEnv.collect(None)
    assert snapshot["DSH_DEMO"] == "v", snapshot

    infos = [i.as_dict() for i in ctx.shellEnv.list() if i.key == "DSH_DEMO"]
    assert infos == [{"contributor": "demo", "key": "DSH_DEMO", "description": "a demo var"}], infos


def test_list_excludes_registry_builtins():
    """``list()`` 只列插件贡献的变量，不含注册表内置事实，也不执行 resolver。"""
    ctx = AppContext()
    apply_shell_env(ctx, {})
    boom = {"called": False}

    def _resolve(_execution):
        boom["called"] = True
        return {"DSH_BOOM": "x"}

    ctx.shellEnv.register(BashEnvContributor("boom", {"DSH_BOOM": BashEnvVariable("d")}, _resolve))
    keys = [i.key for i in ctx.shellEnv.list()]
    # 内置 session-persistence 声明的 DSH_SESSION_JSONL 也在列（按 key 排序）
    assert keys == sorted(["DSH_BOOM", DSH_SESSION_JSONL_KEY]), keys
    assert boom["called"] is False, "list() 不应执行 resolver"


def test_register_rejects_duplicate_name():
    ctx = AppContext()
    apply_shell_env(ctx, {})
    ctx.shellEnv.register(_contributor("dup", "DSH_A"))
    try:
        ctx.shellEnv.register(_contributor("dup", "DSH_B"))
    except ValueError as exc:
        assert "already registered" in str(exc), exc
    else:
        raise AssertionError("重复贡献者名应响亮失败")


def test_register_rejects_reserved_key():
    """内置保留键不可被插件认领。"""
    ctx = AppContext()
    apply_shell_env(ctx, {})
    for reserved in (DSH_HOME_ENV, DSH_SHELL_KEY, DSH_SESSION_ID_KEY):
        try:
            ctx.shellEnv.register(_contributor("owner", reserved))
        except ValueError as exc:
            assert "reserved" in str(exc), exc
        else:
            raise AssertionError(f"保留键 {reserved} 应被拒绝")


def test_register_rejects_invalid_keys():
    """键必须在 ``DSH_`` 命名空间内且后缀为 ``[A-Z][A-Z0-9_]*``。"""
    ctx = AppContext()
    apply_shell_env(ctx, {})
    for bad in ("NOT_DSH", "DSH_lower", "DSH_9LEAD", "DSH_has-dash"):
        try:
            ctx.shellEnv.register(_contributor(f"c-{bad}", bad))
        except ValueError as exc:
            assert "invalid key" in str(exc), f"{bad}: {exc}"
        else:
            raise AssertionError(f"非法键 {bad} 应被拒绝")


def test_register_rejects_undescribed_variable():
    ctx = AppContext()
    apply_shell_env(ctx, {})
    try:
        ctx.shellEnv.register(
            BashEnvContributor("nodesc", {"DSH_NODESC": BashEnvVariable("  ")}, lambda _e: {})
        )
    except ValueError as exc:
        assert "must describe" in str(exc), exc
    else:
        raise AssertionError("缺描述的变量应被拒绝")


def test_register_rejects_duplicate_key_ownership():
    """同一键不能被两个贡献者拥有（声明即所有权）。"""
    ctx = AppContext()
    apply_shell_env(ctx, {})
    ctx.shellEnv.register(_contributor("first", "DSH_SHARED"))
    try:
        ctx.shellEnv.register(_contributor("second", "DSH_SHARED"))
    except ValueError as exc:
        assert "already owned" in str(exc), exc
    else:
        raise AssertionError("键所有权冲突应响亮失败")


def test_disposer_releases_registration():
    """注册随插件纤维释放（disposer 同时摘掉贡献者与键所有权）。"""
    ctx = AppContext()
    apply_shell_env(ctx, {})
    dispose = ctx.shellEnv.register(_contributor("temp", "DSH_TEMP"))
    assert any(i.key == "DSH_TEMP" for i in ctx.shellEnv.list())
    dispose()
    # 内置 session-persistence 贡献者仍在，故按「本贡献者的键消失」断言
    assert not any(i.key == "DSH_TEMP" for i in ctx.shellEnv.list())
    assert "DSH_TEMP" not in ctx.shellEnv.collect(None)
    # 释放后同一键可被他人重新认领
    ctx.shellEnv.register(_contributor("other", "DSH_TEMP"))
    assert any(i.key == "DSH_TEMP" for i in ctx.shellEnv.list())


# --------------------------------------------------------------------------- #
# resolver 契约（fail loud）
# --------------------------------------------------------------------------- #
def test_resolve_undeclared_key_fails_loud():
    ctx = AppContext()
    apply_shell_env(ctx, {})
    ctx.shellEnv.register(
        BashEnvContributor(
            "sneaky",
            {"DSH_DECLARED": BashEnvVariable("d")},
            lambda _e: {"DSH_SNEAKY": "x"},
        )
    )
    try:
        ctx.shellEnv.collect(None)
    except RuntimeError as exc:
        assert "undeclared key" in str(exc), exc
    else:
        raise AssertionError("返回未声明的键应响亮失败")


def test_resolve_non_string_fails_loud():
    ctx = AppContext()
    apply_shell_env(ctx, {})
    ctx.shellEnv.register(
        BashEnvContributor("typed", {"DSH_NUM": BashEnvVariable("d")}, lambda _e: {"DSH_NUM": 1})
    )
    try:
        ctx.shellEnv.collect(None)
    except RuntimeError as exc:
        assert "non-string" in str(exc), exc
    else:
        raise AssertionError("非字符串值应响亮失败")


def test_resolver_receives_execution():
    """resolver 拿到当前执行（据此按 agent 计算值）。"""
    ctx = AppContext()
    apply_shell_env(ctx, {})
    seen = {}

    def _resolve(execution):
        seen["agent"] = execution.get("agent")
        return {"DSH_SEEN": str(execution["agent"].session.header.id)}

    ctx.shellEnv.register(BashEnvContributor("seen", {"DSH_SEEN": BashEnvVariable("d")}, _resolve))
    snapshot = ctx.shellEnv.collect({"agent": _agent("sess-7")})
    assert snapshot["DSH_SEEN"] == "sess-7", snapshot
    assert seen["agent"] is not None


# --------------------------------------------------------------------------- #
# 环境合并
# --------------------------------------------------------------------------- #
def test_merge_env_strips_inherited_dsh():
    """合并快照前剥离继承的 ``DSH_*``，避免嵌套 harness 泄漏陈旧身份。"""
    base = {"PATH": "/bin", DSH_HOME_ENV: "/stale", DSH_SESSION_ID_KEY: "old-session"}
    merged = merge_env(base, {DSH_HOME_ENV: "/fresh", DSH_SHELL_KEY: "1"})
    assert merged == {"PATH": "/bin", DSH_HOME_ENV: "/fresh", DSH_SHELL_KEY: "1"}, merged
    assert base[DSH_HOME_ENV] == "/stale", "merge_env 不得原地修改入参"


def test_merge_env_none_snapshot_copies_base():
    """快照为 ``None`` 时原样拷贝（``os.environ`` 永不被修改）。"""
    base = {"PATH": "/bin", DSH_HOME_ENV: "/stale"}
    merged = merge_env(base, None)
    assert merged == base and merged is not base


def test_collect_for_without_service_returns_none():
    """``shellEnv`` 未挂载时收集入口安全返回 ``None``（工具回退继承环境）。"""
    ctx = AppContext()
    assert collect_for(ctx, {"agent": _agent()}) is None
    apply_shell_env(ctx, {})
    assert collect_for(ctx, None) is not None


# --------------------------------------------------------------------------- #
# 内置 session-persistence 贡献者
# --------------------------------------------------------------------------- #
def test_session_persistence_contributor():
    """内置贡献者在 JSONL 后端可用时贡献 ``DSH_SESSION_JSONL``。"""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = AppContext()
        load_profile(ctx, ["dsh_py.services.session:apply"])
        apply_persistence(ctx, {"dir": tmp})
        apply_shell_env(ctx, {})
        session = ctx.sessions.create(cwd="/workspace/a")
        agent = type("A", (), {"id": session.header.id, "session": session})()

        snapshot = ctx.shellEnv.collect({"agent": agent})
        assert snapshot[DSH_SESSION_JSONL_KEY] == os.path.join(tmp, f"{session.header.id}.jsonl"), snapshot
        # 内置贡献者同样可枚举（模型可见的声明）
        assert any(i.key == DSH_SESSION_JSONL_KEY for i in ctx.shellEnv.list())


def test_session_persistence_omitted_without_agent():
    """无 agent / 无 JSONL 后端时省略 ``DSH_SESSION_JSONL``。"""
    ctx = AppContext()
    apply_shell_env(ctx, {})
    assert DSH_SESSION_JSONL_KEY not in ctx.shellEnv.collect(None)
    assert DSH_SESSION_JSONL_KEY not in ctx.shellEnv.collect({"agent": _agent()})


# --------------------------------------------------------------------------- #
# 端到端：经 tool-bash 注入到真实子进程
# --------------------------------------------------------------------------- #
def _print_env_command(*names):
    """跨 shell（cmd/bash）稳定的「打印环境变量」命令。"""
    items = ",".join(f"{n}={{}}".format("{" + f"os.environ.get('{n}')" + "}") for n in names)
    body = f"import os; print({items!r}.format())" if False else (
        "import os; print(" + ", ".join(f"os.environ.get('{n}')" for n in names) + ")"
    )
    return f'"{sys.executable}" -c "{body}"'


async def test_tool_bash_injects_dsh_env():
    """``tool-bash`` 把快照注入真实命令：``DSH_SHELL`` / ``DSH_SESSION_ID`` 可见。"""
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    apply_shell(ctx, {})
    apply_shell_env(ctx, {})
    apply_tool_bash(ctx, {})
    agent = _agent("sess-99")
    text, err, _ = await ctx.tools.execute_with_agent(
        "bash", json.dumps({"command": _print_env_command(DSH_SHELL_KEY, DSH_SESSION_ID_KEY)}), agent=agent
    )
    assert err is False, text
    assert "1 sess-99" in text, f"DSH_* 未注入子进程: {text!r}"


async def test_shell_strips_inherited_dsh_env():
    """执行器丢弃继承的 ``DSH_*``：陈旧身份不会泄漏进嵌套命令。"""
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    apply_shell(ctx, {})
    apply_shell_env(ctx, {})
    apply_tool_bash(ctx, {})
    agent = _agent("sess-live")
    saved = {k: os.environ.get(k) for k in (DSH_HOME_ENV, DSH_SESSION_ID_KEY)}
    os.environ[DSH_HOME_ENV] = "/stale/home"
    os.environ[DSH_SESSION_ID_KEY] = "stale-session"
    try:
        text, err, _ = await ctx.tools.execute_with_agent(
            "bash", json.dumps({"command": _print_env_command(DSH_SESSION_ID_KEY)}), agent=agent
        )
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert err is False, text
    assert "stale-session" not in text, f"继承的 DSH_SESSION_ID 未剥离: {text!r}"
    assert "sess-live" in text, f"未注入当前会话 id: {text!r}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        result = t()
        if asyncio.iscoroutine(result):
            asyncio.run(result)
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")

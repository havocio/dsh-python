"""hooks-claude-code / hooks-codex 桥集成冒烟（FakeCtx + FakeShell，无需真实进程）。

验证各拦截点的接线与决策映射：
- CC：PreToolUse 拦截/放行、PostToolUse 上下文、SessionStart 注入、UserPromptSubmit 拒绝/放行+上下文、Stop 强制继续；
- Codex：PreToolUse 拦截、PostToolUse 纯 stdout 转上下文。
并验证缺 configPath 时安全降级（不抛错、不注册监听器）。
"""

import asyncio
import json
import os
import sys
import tempfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dsh_py.plugins.hooks_claude_code import apply as cc_apply  # noqa: E402
from dsh_py.plugins.hooks_codex import apply as cx_apply  # noqa: E402
from dsh_py.services.message import TextBlock  # noqa: E402


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class FakeSession:
    def __init__(self, sid, cwd):
        self.header = SimpleNamespace(id=sid, cwd=cwd)
        self.events = []

    def append(self, type, data):
        self.events.append(SimpleNamespace(type=type, data=data))


class FakeAgent:
    def __init__(self, sid, cwd):
        self.session = FakeSession(sid, cwd)
        self.injected = []
        self.followups = []

    def insert(self, msg, target="next-turn"):
        self.injected.append(msg)

    def followup(self, msg):
        self.followups.append(msg)


class FakeShell:
    def __init__(self, script):
        # command 子串 -> (stdout, exit_code, stderr)
        self.script = script
        self.calls = []

    async def run(self, request):
        self.calls.append(request)
        cmd = request.get("command", "")
        for key, (out, code, err) in self.script.items():
            if key in cmd:
                return {"stdout": out, "stderr": err, "exit_code": code}
        return {"stdout": "", "stderr": "", "exit_code": 0}


class FakeCtx:
    def __init__(self, shell):
        self.shell = shell
        self.listeners = {}
        self.effects = []
        self.warns = []
        self.logger = SimpleNamespace(
            warn=lambda *a, **k: self.warns.append(a[0] if a else ""),
            info=lambda *a, **k: None,
            error=lambda *a, **k: None,
        )

    def on(self, name, listener=None, **opts):
        # 支持装饰器用法：@ctx.on("name") 时 listener 为 None，返回注册装饰器
        if listener is None:
            def decorator(fn):
                self.listeners.setdefault(name, []).append(fn)
                return fn
            return decorator
        self.listeners.setdefault(name, []).append(listener)
        return listener

    def effect(self, disposer, label=""):
        self.effects.append(disposer)
        return lambda: True

    def get_listener(self, name):
        return self.listeners.get(name, [])


def _msg_texts(msg):
    out = []
    for b in getattr(msg, "content", []) or []:
        if isinstance(b, dict):
            if b.get("type") == "text":
                out.append(b.get("text", ""))
        elif isinstance(b, TextBlock) or getattr(b, "type", None) == "text":
            out.append(getattr(b, "text", ""))
    return out


def _contains(msgs, needle):
    return any(needle in t for m in msgs for t in _msg_texts(m))


def make_next(value):
    """构造瀑布流 ``next`` 替身：真实 next 是协程函数，监听器会 ``await next()``。"""
    async def _n():
        return value
    return _n


JSON = lambda d: json.dumps(d)


def _write_config(d):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(d, f)
    f.close()
    return f.name


async def _drain_loop(n=10):
    for _ in range(n):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# 测试用例
# --------------------------------------------------------------------------- #
async def test_cc_pre_tool_block_and_allow():
    shell = FakeShell({
        "block-tool": (JSON({"decision": "block", "reason": "no bash"}), 0, ""),
        "allow-tool": ("{}", 0, ""),
    })
    ctx = FakeCtx(shell)
    path = _write_config({"hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"command": "block-tool"}]},
            {"matcher": "*", "hooks": [{"command": "allow-tool"}]},
        ],
    }})
    cc_apply(ctx, {"configPath": path})
    agent = FakeAgent("s1", "/work")
    pre = ctx.get_listener("tools/execute")[0]

    res = await pre({"exec": {"name": "Bash", "arguments": '{"command":"ls"}', "agent": agent, "signal": None}}, make_next(("ok", False)))
    assert isinstance(res, tuple) and res[1] is True and "拦截" in res[0], res

    res2 = await pre({"exec": {"name": "Read", "arguments": "{}", "agent": agent, "signal": None}}, make_next(("ok", False)))
    assert res2 == ("ok", False), res2


async def test_cc_post_tool_context():
    shell = FakeShell({"ctx-tool": (JSON({"additionalContext": "ctx here"}), 0, "")})
    ctx = FakeCtx(shell)
    path = _write_config({"hooks": {"PostToolUse": [{"hooks": [{"command": "ctx-tool"}]}]}})
    cc_apply(ctx, {"configPath": path})
    agent = FakeAgent("s1", "/work")
    post = ctx.get_listener("tools/post-execute")[0]
    res = await post(
        {"exec": {"name": "Bash", "arguments": "{}", "agent": agent, "signal": None},
         "result": {"content": [{"type": "text", "text": "done"}]}},
        make_next({"additionalContexts": []}),
    )
    assert isinstance(res, dict) and res.get("additionalContexts"), res
    assert _contains(res["additionalContexts"], "ctx here"), res


async def test_cc_session_start_inject():
    shell = FakeShell({"session-ctx": (JSON({"additionalContext": "session ctx"}), 0, "")})
    ctx = FakeCtx(shell)
    path = _write_config({"hooks": {"SessionStart": [{"hooks": [{"command": "session-ctx"}]}]}})
    cc_apply(ctx, {"configPath": path})
    agent = FakeAgent("s1", "/work")
    sess = ctx.get_listener("agent/session-start")[0]
    sess({"agent": agent, "source": "cli"})
    await _drain_loop()
    assert _contains(agent.injected, "session ctx"), agent.injected


async def test_cc_user_prompt_deny():
    shell = FakeShell({"deny-prompt": (JSON({"decision": "block"}), 0, "")})
    ctx = FakeCtx(shell)
    path = _write_config({"hooks": {"UserPromptSubmit": [{"hooks": [{"command": "deny-prompt"}]}]}})
    cc_apply(ctx, {"configPath": path})
    agent = FakeAgent("s1", "/work")
    pre_step = ctx.get_listener("agent/pre-step")[0]
    msg = SimpleNamespace(content=[SimpleNamespace(type="text", text="hello")])
    res = await pre_step({"messages": [msg], "agent": agent, "turn": 1, "signal": None},
                         make_next({"kind": "enter", "messages": [msg]}))
    assert res == {"kind": "reject"}, res


async def test_cc_user_prompt_allow_context():
    shell = FakeShell({"ctx-prompt": (JSON({"additionalContext": "prompt ctx"}), 0, "")})
    ctx = FakeCtx(shell)
    path = _write_config({"hooks": {"UserPromptSubmit": [{"hooks": [{"command": "ctx-prompt"}]}]}})
    cc_apply(ctx, {"configPath": path})
    agent = FakeAgent("s1", "/work")
    pre_step = ctx.get_listener("agent/pre-step")[0]
    msg = SimpleNamespace(content=[SimpleNamespace(type="text", text="hello")])
    res = await pre_step({"messages": [msg], "agent": agent, "turn": 1, "signal": None},
                         make_next({"kind": "enter", "messages": [msg]}))
    assert isinstance(res, dict) and res.get("kind") == "enter", res
    assert _contains(res.get("messages", []), "prompt ctx"), res


async def test_cc_stop_force_continue():
    shell = FakeShell({"stop-block": (JSON({"decision": "block", "reason": "keep going"}), 0, "")})
    ctx = FakeCtx(shell)
    path = _write_config({"hooks": {"Stop": [{"hooks": [{"command": "stop-block"}]}]}})
    cc_apply(ctx, {"configPath": path})
    agent = FakeAgent("s1", "/work")
    status = ctx.get_listener("agent/status")[0]
    await status({"agent": agent, "status": "idle"}, None)
    await _drain_loop()
    assert _contains(agent.followups, "keep going"), agent.followups


async def test_cx_pre_tool_block():
    # Codex 原生阻断形态：permissionDecision=deny
    shell = FakeShell({"cx-block": (JSON({"hookSpecificOutput": {"permissionDecision": "deny"}}), 0, "")})
    ctx = FakeCtx(shell)
    path = _write_config({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"command": "cx-block"}]}]}})
    cx_apply(ctx, {"configPath": path})
    agent = FakeAgent("s1", "/work")
    pre = ctx.get_listener("tools/execute")[0]
    res = await pre({"exec": {"name": "Bash", "arguments": '{"command":"ls"}', "agent": agent, "signal": None}}, make_next(("ok", False)))
    assert isinstance(res, tuple) and res[1] is True and "拦截" in res[0], res


async def test_cx_plain_stdout_context():
    shell = FakeShell({"cx-plain": ("plain text ctx", 0, "")})
    ctx = FakeCtx(shell)
    path = _write_config({"hooks": {"PostToolUse": [{"hooks": [{"command": "cx-plain"}]}]}})
    cx_apply(ctx, {"configPath": path})
    agent = FakeAgent("s1", "/work")
    post = ctx.get_listener("tools/post-execute")[0]
    res = await post(
        {"exec": {"name": "Bash", "arguments": '{"command":"ls"}', "agent": agent, "signal": None},
         "result": {"content": [{"type": "text", "text": "done"}]}},
        make_next({"additionalContexts": []}),
    )
    assert _contains(res.get("additionalContexts", []), "plain text ctx"), res


async def test_missing_config_path_safe():
    ctx = FakeCtx(FakeShell({}))
    cc_apply(ctx, {})  # 无 configPath
    assert ctx.get_listener("tools/execute") == [], "缺 configPath 不应注册监听器"
    assert any("configPath" in w for w in ctx.warns), ctx.warns


async def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        await t()
        print(f"  ✓ {t.__name__}")
    print(f"test_hooks_bridges: 全部通过 ✅ ({len(tests)} 例)")


if __name__ == "__main__":
    asyncio.run(main())

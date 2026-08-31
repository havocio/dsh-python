"""hooks_config 解析器单测（纯函数，无需 ctx）。

覆盖：
- Claude Code：settings 包裹 / 裸事件映射、命令替换、跳过非 command、SubagentStart 仍解析、非法正则抛错；
- Codex：跳过 async、timeout|timeoutSec、不做替换、非法正则抛错；
- substitute_command 变量未设置时保持原样。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dsh_py.services.hooks_config import (  # noqa: E402
    parse_claude_code_config,
    parse_codex_config,
    substitute_command,
)


def test_cc_settings_wrap_and_substitution():
    raw = {"hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/h.py", "timeout": 30}]}],
        "PostToolUse": [{"hooks": [{"command": "echo hi"}]}],
        "SubagentStart": [{"hooks": [{"command": "echo sub"}]}],
    }}
    r = parse_claude_code_config(raw, plugin_root="/root", project_dir="/proj")
    assert "PreToolUse" in r.config and "PostToolUse" in r.config and "SubagentStart" in r.config
    cmd = r.config["PreToolUse"][0].hooks[0].command
    assert cmd == "/root/h.py", cmd
    # matcher 在纯 token 时原样保留
    assert r.config["PreToolUse"][0].matcher == "Bash"


def test_cc_bare_event_map():
    raw = {"PreToolUse": [{"hooks": [{"command": "x"}]}]}
    r = parse_claude_code_config(raw)
    assert "PreToolUse" in r.config


def test_cc_skips_non_command():
    raw = {"hooks": {"Stop": [{"hooks": [{"type": "prompt", "command": "x"}]}]}}
    r = parse_claude_code_config(raw)
    assert "Stop" not in r.config
    assert any(s.event == "Stop" and "prompt" in s.reason for s in r.skipped)


def test_cc_drops_matcher_on_no_subject_events():
    raw = {"hooks": {"UserPromptSubmit": [{"matcher": "Bash", "hooks": [{"command": "x"}]}]}}
    r = parse_claude_code_config(raw)
    assert r.config["UserPromptSubmit"][0].matcher is None


def test_cc_invalid_regex_raises():
    raw = {"hooks": {"PreToolUse": [{"matcher": "[bad(", "hooks": [{"command": "x"}]}]}}
    try:
        parse_claude_code_config(raw)
        raise AssertionError("应当抛 ValueError")
    except ValueError:
        pass


def test_cc_literal_matcher_not_validated_as_regex():
    # 纯 token 视为字面，不是正则，不应抛错
    raw = {"hooks": {"PreToolUse": [{"matcher": "Bash|Edit", "hooks": [{"command": "x"}]}]}}
    r = parse_claude_code_config(raw)
    assert r.config["PreToolUse"][0].matcher == "Bash|Edit"


def test_codex_skips_async_and_uses_timeoutsec():
    raw = {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"command": "echo c", "timeoutSec": 5}]}],
        "SessionStart": [{"hooks": [{"command": "echo s", "async": True}]}],
    }
    r = parse_codex_config(raw)
    assert "PreToolUse" in r.config
    assert "SessionStart" not in r.config
    assert r.config["PreToolUse"][0].hooks[0].timeout_sec == 5.0
    assert any(s.event == "SessionStart" and s.reason == "async hook" for s in r.skipped)


def test_codex_no_substitution():
    raw = {"PreToolUse": [{"hooks": [{"command": "${CLAUDE_PROJECT_DIR}/x"}]}]}
    r = parse_codex_config(raw)
    assert r.config["PreToolUse"][0].hooks[0].command == "${CLAUDE_PROJECT_DIR}/x"


def test_codex_invalid_regex_raises():
    raw = {"PreToolUse": [{"matcher": "(unclosed", "hooks": [{"command": "x"}]}]}
    try:
        parse_codex_config(raw)
        raise AssertionError("应当抛 ValueError")
    except ValueError:
        pass


def test_substitute_command_unset_var_kept():
    assert substitute_command("run ${CLAUDE_PROJECT_DIR}/x", None, "/proj") == "run /proj/x"
    assert substitute_command("run ${CLAUDE_PLUGIN_ROOT}/x", "/r", None) == "run /r/x"
    # 未设置的变量保持原样，不替换成空
    assert substitute_command("a ${CLAUDE_PROJECT_DIR} b", None, None) == "a ${CLAUDE_PROJECT_DIR} b"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"test_hooks_config: 全部通过 ✅ ({len(tests)} 例)")


if __name__ == "__main__":
    main()

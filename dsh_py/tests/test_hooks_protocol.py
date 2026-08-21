"""钩子协议核心（hooks_protocol）验证（第 3 层治理类）。

运行：python dsh_py/tests/test_hooks_protocol.py

覆盖：matcher 匹配（literal 管道 / regex）、输出解码（exit 0/2 语义、事件名不匹配
丢弃）、结果合并（block/ask/pass 克制合并）、stderr 摘要。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.services.hooks_protocol import (
    HookOutput,
    MatcherMode,
    MergedOutcome,
    matches_matcher,
    matcher_diagnostic,
    merge_hook_outputs,
    parse_hook_output,
    summarize_stderr,
)


def test_matcher_literal_pipe_alternation():
    # claude-code 纯 token → literal 管道交替
    assert matches_matcher("Read|Write", "Read", "claude-code") is True
    assert matches_matcher("Read|Write", "Write", "claude-code") is True
    assert matches_matcher("Read|Write", "Edit", "claude-code") is False
    # '' / '*' / None → 匹配全部
    assert matches_matcher("", "Anything", "claude-code") is True
    assert matches_matcher("*", "Anything", "claude-code") is True
    assert matches_matcher(None, "Anything", "claude-code") is True


def test_matcher_regex_mode():
    # codex 永远 regex
    assert matches_matcher("^Read$", "Read", "codex") is True
    assert matches_matcher("^Read$", "ReadX", "codex") is False
    # claude-code 含非 token 字符 → 走 regex
    assert matches_matcher("Read.*", "ReadX", "claude-code") is True


def test_matcher_diagnostic():
    assert matcher_diagnostic("Read|Write", "claude-code") == "literal"
    assert matcher_diagnostic("Read.*", "claude-code") == "regex"
    assert matcher_diagnostic("*", "claude-code") == "match-all"
    assert matcher_diagnostic("^x$", "codex") == "regex"


def test_parse_exit_0_apply_parsed():
    out = parse_hook_output(0, '{"decision":"approve","reason":"ok"}', "", "PreToolUse")
    assert out.exit_code == 0
    assert out.decision == "approve"
    assert out.reason == "ok"


def test_parse_exit_2_blocks():
    out = parse_hook_output(2, '{"hookEventName":"PreToolUse","decision":"block","reason":"nope"}',
                             "denied", "PreToolUse")
    assert out.continue_flag is False
    assert out.decision == "block"
    # exit 2 无 reason 时回退 stderr
    out2 = parse_hook_output(2, "{}", "denied", "PreToolUse")
    assert out2.stop_reason == "denied"


def test_parse_event_name_mismatch_drops_event_fields():
    # 事件名不匹配 → 丢弃 additionalContext / updatedInput，但保留 hookEventName
    out = parse_hook_output(0, '{"hookEventName":"PostToolUse","additionalContext":"X","updatedInput":{"a":1}}',
                             "", "PreToolUse")
    assert out.hook_event_name == "PostToolUse"
    assert out.additional_context is None
    assert out.updated_input is None


def test_parse_no_exit_code_infrastructure_reject():
    out = parse_hook_output(None, "", "cannot run", "PreToolUse")
    assert out.exit_code is None
    # 永不抛错
    assert out.stderr == "cannot run"


def test_merge_block_takes_first_reason():
    a = HookOutput(exit_code=2, continue_flag=False, stop_reason="denied by A")
    b = HookOutput(exit_code=0, additional_context="ctx from B")
    merged = merge_hook_outputs([a, b])
    assert merged.decision == "block"
    assert merged.block_reason == "denied by A"
    assert "ctx from B" in merged.additional_contexts


def test_merge_ask_promotes():
    a = HookOutput(exit_code=0, decision="ask")
    b = HookOutput(exit_code=0, decision="allow")
    merged = merge_hook_outputs([a, b])
    assert merged.decision == "ask"


def test_merge_pass_with_contexts():
    a = HookOutput(exit_code=0, additional_context="c1")
    b = HookOutput(exit_code=0, system_message="sys", updated_input={"x": 1})
    merged = merge_hook_outputs([a, b])
    assert merged.decision == "pass"
    assert merged.additional_contexts == ["c1"]
    assert merged.system_messages == ["sys"]
    # updatedInput 被推迟，记警告而非兑现
    assert any("deferred" in w for w in merged.warnings)


def test_summarize_stderr_truncates():
    long = "x" * 600
    summary = summarize_stderr(long, max_chars=500)
    assert summary.startswith("x" * 500)
    assert "more chars" in summary
    assert summarize_stderr("short", max_chars=500) == "short"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\nOK: hooks_protocol 测试通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()

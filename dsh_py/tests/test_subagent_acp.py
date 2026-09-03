"""subagent-acp 契约单测（纯逻辑，不依赖真实 ACP 子进程）。

覆盖 ``services/subagent_acp.py`` 的纯函数协议映射：stop-reason 归一、
内容块文本提取、harness 提示→ACP prompt 翻译、输出折叠。运行范式与仓库其他
``test_*.py`` 一致。
"""

from __future__ import annotations

from types import SimpleNamespace

from dsh_py.services.subagent_acp import (
    OutputFold,
    acp_content_text,
    acp_stop_reason,
    to_acp_prompt,
)


def test_acp_stop_reason() -> None:
    assert acp_stop_reason("end_turn") == "completed"
    assert acp_stop_reason("max_tokens") == "max-tokens"
    assert acp_stop_reason("refusal") == "refusal"
    assert acp_stop_reason("cancelled") == "aborted"
    # 不洁停止一律归 error（含未来未知变体）
    assert acp_stop_reason("max_turn_requests") == "error"
    assert acp_stop_reason("something_new") == "error"
    print("  ✓ acp_stop_reason 映射正确")


def test_acp_content_text() -> None:
    assert acp_content_text({"type": "text", "text": "hi"}) == "hi"
    assert acp_content_text({"type": "image"}) == ""
    assert acp_content_text({"notype": "x"}) == ""
    assert acp_content_text(SimpleNamespace(type="text", text="obj")) == "obj"
    assert acp_content_text(SimpleNamespace(type="image", text="obj")) == ""
    assert acp_content_text(None) == ""
    assert acp_content_text("plain") == ""
    print("  ✓ acp_content_text 提取/兜底正确")


def test_to_acp_prompt() -> None:
    prompt = [
        {"type": "text", "text": "a"},
        {"type": "image"},
        SimpleNamespace(type="text", text="b"),
    ]
    out = to_acp_prompt(prompt)
    assert out == [
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b"},
    ]
    assert to_acp_prompt([]) == []
    assert to_acp_prompt(None) == []
    print("  ✓ to_acp_prompt 仅保留 text 块正确")


def test_output_fold() -> None:
    fold = OutputFold()
    assert fold.collect() == []
    fold.push_text("a")
    fold.push_text("b")
    fold.push_text("")
    assert fold.collect() == [{"type": "text", "text": "ab"}]
    print("  ✓ OutputFold 拼接正确")


def _main() -> None:
    print("== test_subagent_acp ==")
    test_acp_stop_reason()
    test_acp_content_text()
    test_to_acp_prompt()
    test_output_fold()
    print("== test_subagent_acp: ALL PASS ==")


if __name__ == "__main__":
    _main()

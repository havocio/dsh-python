"""acp seam 契约单测（对照 dsh 临时冒烟脚本，正式入库）。

覆盖：prompt→text 拼接、不支持内容检测、turn 结束原因映射、绝对路径判定。
均不依赖真实 ACP 传输进程。
"""

from __future__ import annotations

import os

from dsh_py.services.acp import (
    _is_absolute,
    acp_prompt_to_text,
    prompt_has_unsupported_content,
    turn_end_to_stop_reason,
)


async def test_prompt_to_text() -> None:
    assert acp_prompt_to_text([{"type": "text", "text": "hello"},
                               {"type": "resource_link", "uri": "u://x"}]) == "hello\n[u://x]"
    assert acp_prompt_to_text(["plain"]) == "plain"
    assert acp_prompt_to_text([{"type": "text", "text": "a"}, "b",
                               {"type": "resource_link", "uri": "c"}]) == "a\nb\n[c]"
    print("  ✓ acp_prompt_to_text 正确")


async def test_unsupported_content() -> None:
    assert not prompt_has_unsupported_content([{"type": "text", "text": "x"}])
    assert not prompt_has_unsupported_content([{"type": "resource_link", "uri": "u"}])
    assert prompt_has_unsupported_content([{"type": "image", "uri": "u"}])
    print("  ✓ prompt_has_unsupported_content 正确")


async def test_stop_reason() -> None:
    assert turn_end_to_stop_reason({"kind": "max-tokens"}) == "end_turn"
    assert turn_end_to_stop_reason({"kind": "error"}) == "error"
    assert turn_end_to_stop_reason({"kind": "other"}) == "end_turn"
    assert turn_end_to_stop_reason("not-dict") == "end_turn"
    print("  ✓ turn_end_to_stop_reason 正确")


async def test_is_absolute() -> None:
    assert _is_absolute(os.path.abspath("x"))
    assert not _is_absolute("relative/path")
    print("  ✓ _is_absolute 正确")


async def main() -> None:
    print("== test_acp ==")
    await test_prompt_to_text()
    await test_unsupported_content()
    await test_stop_reason()
    await test_is_absolute()
    print("OK: acp seam 契约单测通过")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

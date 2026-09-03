"""agent-presets 契约单测（纯逻辑，不依赖文件系统或组合挂载）。

覆盖 ``services/agent_presets/preset.py`` 的词汇与错误：预设 id 模式、未知/
挂载失败两类不同信号。运行范式与仓库其他 ``test_*.py`` 一致。
"""

from __future__ import annotations

from dsh_py.services.agent_presets.preset import (
    PRESET_ID,
    PresetMountError,
    UnknownPresetError,
)


def test_preset_id() -> None:
    assert PRESET_ID.match("my-preset")
    assert PRESET_ID.match("a")
    assert PRESET_ID.match("a1-b2")
    assert not PRESET_ID.match("My")
    assert not PRESET_ID.match("-abc")
    assert not PRESET_ID.match("")
    assert not PRESET_ID.match("a/b")
    assert not PRESET_ID.match("..")
    assert not PRESET_ID.match("a.b")
    print("  ✓ PRESET_ID 路径段安全模式正确")


def test_unknown_preset_error() -> None:
    e = UnknownPresetError("x", ["a", "b"])
    assert e.preset_id == "x"
    assert e.available == ["a", "b"]
    assert "x" in str(e) and "a" in str(e)
    print("  ✓ UnknownPresetError 携带可用清单正确")


def test_preset_mount_error() -> None:
    e = PresetMountError("x", "reason")
    assert e.preset_id == "x"
    assert e.reason == "reason"
    assert "x" in str(e)

    cause = ValueError("boom")
    e2 = PresetMountError("x", "r", cause=cause)
    assert e2.__cause__ is cause
    print("  ✓ PresetMountError 携带原因/因果链正确")


def _main() -> None:
    print("== test_preset ==")
    test_preset_id()
    test_unknown_preset_error()
    test_preset_mount_error()
    print("== test_preset: ALL PASS ==")


if __name__ == "__main__":
    _main()

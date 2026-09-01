"""fs-sandbox 后端测试（B/C 类）：沙箱围栏语义（read-only / workspace-write /
danger-full-access）、fail-closed 回退、``is_path_under`` 词法/inode 判定，以及
工具层在 read-only 下的拒绝。

运行：``python dsh_py/tests/test_fs_sandbox.py``
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from types import SimpleNamespace

from dsh_py.core.context import AppContext
from dsh_py.services import tools as T
from dsh_py.services.fs import FS_SANDBOX_DENIED, FsError
from dsh_py.services.sandbox_policy import SandboxPolicyResolver
from dsh_py.services.fs_sandbox import (
    SandboxedFileSystem,
    apply as apply_fs_sandbox,
    is_path_under,
)
from dsh_py.plugins.fs_observation_policy import apply as apply_fs_observation_policy
from dsh_py.plugins.tool_fs import apply as apply_tool_fs
from dsh_py.plugins.tool_str_replace_editor import apply as apply_tool_str_replace_editor


def _ctx(mode: str = "read-only", workspace_root: str = ".") -> AppContext:
    """装配沙箱化 fs 栈：sandboxPolicy + 沙箱后端 + 观测门 + 两个文件工具。"""
    ctx = AppContext()
    T.apply(ctx)  # 先有 ctx.tools
    resolver = SandboxPolicyResolver(default_mode=mode, workspace_root=workspace_root)
    ctx.provide("sandboxPolicy", resolver)
    apply_fs_sandbox(ctx)                    # ctx.fs（沙箱化后端，读取 ctx.sandboxPolicy）
    apply_fs_observation_policy(ctx)         # fs 观测态门（3 个同步监听器）
    apply_tool_fs(ctx)                       # read / write / edit 工具
    apply_tool_str_replace_editor(ctx)       # str_replace_editor 工具
    return ctx


# --------------------------------------------------------------------------- #
# 1. read-only：拒绝全部变更
# --------------------------------------------------------------------------- #
def test_read_only_rejects_write() -> None:
    ctx = _ctx(mode="read-only")
    path = os.path.join(tempfile.mkdtemp(), "x.txt")
    try:
        ctx.fs.write_text(path, "hi")
        raise AssertionError("read-only 应当拒绝写入")
    except FsError as exc:
        assert exc.code == FS_SANDBOX_DENIED


def test_read_only_rejects_edit() -> None:
    ctx = _ctx(mode="read-only")
    path = os.path.join(tempfile.mkdtemp(), "x.txt")
    try:
        ctx.fs.edit_text(path, "a", "b")
        raise AssertionError("read-only 应当拒绝编辑")
    except FsError as exc:
        assert exc.code == FS_SANDBOX_DENIED


# --------------------------------------------------------------------------- #
# 2. workspace-write：仅放行可写根内
# --------------------------------------------------------------------------- #
def test_workspace_write_allows_within_root() -> None:
    ws = tempfile.mkdtemp()
    ctx = _ctx(mode="workspace-write", workspace_root=ws)
    path = os.path.join(ws, "x.txt")
    res = ctx.fs.write_text(path, "hi")
    assert res["bytes"] == 2
    assert ctx.fs.exists(path)


def test_workspace_write_rejects_outside_root() -> None:
    ws = tempfile.mkdtemp()
    other = tempfile.mkdtemp()
    ctx = _ctx(mode="workspace-write", workspace_root=ws)
    path = os.path.join(other, "x.txt")
    try:
        ctx.fs.write_text(path, "hi")
        raise AssertionError("workspace-write 应当拒绝越界写入")
    except FsError as exc:
        assert exc.code == FS_SANDBOX_DENIED


# --------------------------------------------------------------------------- #
# 3. danger-full-access：透传
# --------------------------------------------------------------------------- #
def test_danger_full_access_passthrough() -> None:
    ctx = _ctx(mode="danger-full-access")
    path = os.path.join(tempfile.mkdtemp(), "x.txt")
    res = ctx.fs.write_text(path, "hi")
    assert res["bytes"] == 2
    assert ctx.fs.exists(path)


# --------------------------------------------------------------------------- #
# 4. 缺失策略 fail-closed 回退 read-only
# --------------------------------------------------------------------------- #
def test_missing_policy_fail_closed() -> None:
    ctx = AppContext()
    # 不提供 sandboxPolicy → SandboxedFileSystem 内部 fail-closed 回退 read-only
    SandboxedFileSystem(ctx)
    path = os.path.join(tempfile.mkdtemp(), "x.txt")
    try:
        ctx.fs.write_text(path, "hi")
        raise AssertionError("缺失策略应当 fail-closed 回退 read-only")
    except FsError as exc:
        assert exc.code == FS_SANDBOX_DENIED


# --------------------------------------------------------------------------- #
# 5. is_path_under：词法快路径 + inode 身份回退
# --------------------------------------------------------------------------- #
def test_is_path_under() -> None:
    root = tempfile.mkdtemp()
    child = os.path.join(root, "sub", "f.txt")
    assert is_path_under(child, root) is True
    assert is_path_under(root, root) is True  # 自身
    other = tempfile.mkdtemp()
    assert is_path_under(other, root) is False
    # 词法大小写（Windows 默认不敏感）
    upper = os.path.join(root, "SUB", "F.TXT").upper()
    assert is_path_under(upper, root, case_sensitive=False) is True
    # 明确要求大小写敏感时，大小写不一致不算在内
    if not upper == os.path.normcase(upper):
        assert is_path_under(upper, root, case_sensitive=True) is False


# --------------------------------------------------------------------------- #
# 6. 工具层透传沙箱策略（read-only 下工具报错）
# --------------------------------------------------------------------------- #
def test_tool_fs_read_only_rejected() -> None:
    ctx = _ctx(mode="read-only")
    path = os.path.join(tempfile.mkdtemp(), "x.txt")
    out, is_error = asyncio.run(
        ctx.tools.execute("write", json.dumps({"file_path": path, "content": "hi"}))
    )
    assert is_error, f"read-only 下工具应报错，实际：{out}"
    assert "FS_SANDBOX_DENIED" in out or "拒绝" in out


def main() -> None:
    tests = [
        test_read_only_rejects_write,
        test_read_only_rejects_edit,
        test_workspace_write_allows_within_root,
        test_workspace_write_rejects_outside_root,
        test_danger_full_access_passthrough,
        test_missing_policy_fail_closed,
        test_is_path_under,
        test_tool_fs_read_only_rejected,
    ]
    passed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"FAIL {test.__name__}: {exc}")
            traceback.print_exc()
    print(f"== {passed}/{len(tests)} passed ==")
    if passed != len(tests):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

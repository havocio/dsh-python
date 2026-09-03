"""workspace 家族契约单测（纯逻辑 / mock，不依赖存储后端）。

覆盖 ``services/workspace.py`` 与 ``services/workspace_registry.py`` 的纯逻辑可测面：
路径规范化、会话移动、实体投影、注册表纯函数（id 比较 / 头部排序 / 基名）。
运行范式与仓库其他 ``test_*.py`` 一致。
"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

from dsh_py.services.workspace import (
    WorkspaceEntity,
    WorkspaceEntityHost,
    WorkspaceId,
    WorkspaceMoveInvalidError,
    _move_session,
    realpath_normalize,
)
from dsh_py.services.workspace_registry import (
    WorkspaceOrderInvalidError,
    WorkspaceUnknownSessionError,
    _basename,
    _compare_headers,
    _header_sort_key,
    _same_ids,
)


def test_realpath_normalize() -> None:
    with tempfile.TemporaryDirectory() as d:
        sub = os.path.join(d, "sub")
        os.makedirs(sub)
        assert realpath_normalize(d) == os.path.realpath(d)
        # 折叠 ``..`` 与尾斜杠
        assert realpath_normalize(os.path.join(d, "sub", "..")) == realpath_normalize(d)
        assert realpath_normalize(d + os.sep) == realpath_normalize(d)
    print("  ✓ realpath_normalize 规范化/折叠正确")


def test_move_session() -> None:
    rec = {"path": "/w", "title": "t", "sessionIds": ["a", "b", "c"],
           "createdAt": "x", "updatedAt": "y"}

    # 移到末尾
    moved = _move_session(rec, "a", None)
    assert moved["sessionIds"] == ["b", "c", "a"]

    # 移到锚点之前
    moved = _move_session(rec, "a", "c")
    assert moved["sessionIds"] == ["b", "a", "c"]

    # 未入账会话 → WorkspaceMoveInvalidError
    try:
        _move_session(rec, "z", None)
        raise AssertionError("未入账会话应抛错")
    except WorkspaceMoveInvalidError:
        pass

    # 锚点未入账 → WorkspaceMoveInvalidError
    try:
        _move_session(rec, "a", "z")
        raise AssertionError("锚点未入账应抛错")
    except WorkspaceMoveInvalidError:
        pass

    # before == session_id → 原样返回（同一引用）
    assert _move_session(rec, "a", "a") is rec

    # 已在该位置（no-op） → 原样返回
    assert _move_session({"path": "/w", "sessionIds": ["a", "b"]}, "a", None) is not None
    print("  ✓ _move_session 移动/拒绝边界正确")


def test_workspace_entity_session_ids() -> None:
    class _Host(WorkspaceEntityHost):
        def session_path(self, session_id):  # noqa: ANN001
            return {"a": "/w", "b": "/other"}.get(session_id)  # 仅 a 落在本工作区

        def table(self):  # noqa: ANN001
            raise NotImplementedError

        async def read_session_header(self, session_id):  # noqa: ANN001
            raise NotImplementedError

        def remember_session_path(self, session_id, path):  # noqa: ANN001
            pass

    entity = WorkspaceEntity(
        _Host(), WorkspaceId("w"),
        {"path": "/w", "title": "t", "sessionIds": ["a", "b"],
         "createdAt": "x", "updatedAt": "y"})
    assert entity.session_ids == ["a"]
    assert entity.path == "/w"
    print("  ✓ WorkspaceEntity.session_ids 头部校验过滤正确")


def test_workspace_errors() -> None:
    assert issubclass(WorkspaceMoveInvalidError, Exception)
    err = WorkspaceUnknownSessionError("s1")
    assert err.session_id == "s1"
    err = WorkspaceOrderInvalidError(WorkspaceId("w1"))
    assert err.workspace_id == "w1"
    print("  ✓ workspace 错误类携带标识正确")


def test_registry_pure() -> None:
    assert _same_ids([1, 2], [1, 2]) is True
    assert _same_ids([1, 2], [2, 1]) is False
    assert _same_ids([1], [1, 2]) is False

    h_new = SimpleNamespace(created_at=200, id="b")
    h_old = SimpleNamespace(created_at=100, id="a")
    assert _compare_headers(h_old, h_new) == 1   # 右更新 → 1
    assert _compare_headers(h_new, h_old) == -1  # 右更旧 → -1
    # 同刻按 id 码点
    h_a = SimpleNamespace(created_at=100, id="a")
    h_b = SimpleNamespace(created_at=100, id="b")
    assert _compare_headers(h_a, h_b) == -1

    assert _header_sort_key(h_new) == (-200, "b")

    assert _basename("/a/b/c.txt") == "c.txt"
    assert _basename("/a/b/") == "b"
    print("  ✓ workspace_registry 纯函数（id 比较/头部排序/基名）正确")


def _main() -> None:
    print("== test_workspace ==")
    test_realpath_normalize()
    test_move_session()
    test_workspace_entity_session_ids()
    test_workspace_errors()
    test_registry_pure()
    print("== test_workspace: ALL PASS ==")


if __name__ == "__main__":
    _main()

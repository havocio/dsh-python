"""fs-observation-policy 测试（B/C 类）：观测态门逻辑（按 owner 隔离）、意图瀑布
决策（write-intent / edit-intent）、版本守卫（FS_NOT_OBSERVED / FS_NOT_FOUND /
FS_STALE_VERSION），以及工具层透传 actor 的端到端隔离。

运行：``python dsh_py/tests/test_fs_observation_policy.py``
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from types import SimpleNamespace

from dsh_py.core.context import AppContext
from dsh_py.services import tools as T
from dsh_py.services.fs import FS_NOT_FOUND, FS_NOT_OBSERVED, FS_STALE_VERSION, FsError
from dsh_py.services.sandbox_policy import SandboxPolicyResolver
from dsh_py.plugins.fs_observation_policy import (
    ObservedStateGate,
    apply as apply_fs_observation_policy,
)
from dsh_py.plugins.tool_fs import apply as apply_tool_fs
from dsh_py.services.fs_sandbox import apply as apply_fs_sandbox


class FakeSession:
    """最小会话桩：``SandboxPolicyResolver.resolve`` 读取 ``.events``，
    ``owner_of`` 仅取 ``.session``，``WeakKeyDictionary`` 需要可弱引用对象。"""

    def __init__(self, sid: str) -> None:
        self.events: list = []
        self.header = SimpleNamespace(id=sid)


def _agent_for(session: "FakeSession") -> SimpleNamespace:
    """构造一个充当 ``actor`` 的 agent（``actor.agent.session`` 收敛到 session）。"""
    return SimpleNamespace(session=session)


def _ctx(mode: str = "danger-full-access", workspace_root: str = ".") -> AppContext:
    """装配沙箱化 fs 栈；默认 ``danger-full-access`` 以隔离观测逻辑（不被围栏干扰）。"""
    ctx = AppContext()
    T.apply(ctx)
    resolver = SandboxPolicyResolver(default_mode=mode, workspace_root=workspace_root)
    ctx.provide("sandboxPolicy", resolver)
    apply_fs_sandbox(ctx)
    apply_fs_observation_policy(ctx)
    apply_tool_fs(ctx)  # read / write / edit 工具（透传 session 为 actor）
    return ctx


# --------------------------------------------------------------------------- #
# 1. 门逻辑单元测试（纯函数，不依赖 ctx）
# --------------------------------------------------------------------------- #
def test_gate_unit() -> None:
    gate = ObservedStateGate()
    payload = {"path": "/a", "actor": None}
    # 未观测 → 交由后端默认（无条件写）
    assert gate.write_intent(payload) is None
    # 观测为缺失 → 允许创建
    gate.observe({"path": "/a", "present": False, "actor": None})
    assert gate.write_intent(payload) == {"createIfAbsent": True, "replaceIfVersion": None}
    # 观测为存在 → 要求替换时版本匹配最近一次观测版本
    gate.observe({"path": "/a", "present": True, "version": 3, "actor": None})
    assert gate.write_intent(payload) == {"createIfAbsent": False, "replaceIfVersion": 3}
    # edit 未观测 → FS_NOT_OBSERVED
    try:
        gate.edit_intent({"path": "/b", "actor": None})
        raise AssertionError("edit 未观测应抛 FS_NOT_OBSERVED")
    except FsError as exc:
        assert exc.code == FS_NOT_OBSERVED
    # edit 观测缺失 → FS_NOT_FOUND
    gate.observe({"path": "/b", "present": False, "actor": None})
    try:
        gate.edit_intent({"path": "/b", "actor": None})
        raise AssertionError("edit 观测缺失应抛 FS_NOT_FOUND")
    except FsError as exc:
        assert exc.code == FS_NOT_FOUND
    # edit 观测存在 → 返回其观测版本
    gate.observe({"path": "/b", "present": True, "version": 7, "actor": None})
    assert gate.edit_intent({"path": "/b", "actor": None}) == {"version": 7}


# --------------------------------------------------------------------------- #
# 2. edit 未观测 / 观测缺失：经 ctx.fs 端到端
# --------------------------------------------------------------------------- #
def test_edit_not_observed() -> None:
    ctx = _ctx(mode="danger-full-access")
    path = os.path.join(tempfile.mkdtemp(), "new.txt")
    try:
        ctx.fs.edit_text(path, "a", "b")
        raise AssertionError("edit 未观测应抛 FS_NOT_OBSERVED")
    except FsError as exc:
        assert exc.code == FS_NOT_OBSERVED


def test_edit_observed_missing() -> None:
    ctx = _ctx(mode="danger-full-access")
    path = os.path.join(tempfile.mkdtemp(), "missing.txt")
    # read 不存在 → 抛 FileNotFoundError，但 emit fs/observed(present=False)
    try:
        ctx.fs.read_text(path)
    except FileNotFoundError:
        pass
    # 之后 edit → FS_NOT_FOUND
    try:
        ctx.fs.edit_text(path, "a", "b")
        raise AssertionError("观测缺失应抛 FS_NOT_FOUND")
    except FsError as exc:
        assert exc.code == FS_NOT_FOUND


# --------------------------------------------------------------------------- #
# 3. 观测态按 owner（会话）隔离
# --------------------------------------------------------------------------- #
def test_observation_isolated_per_session() -> None:
    ctx = _ctx(mode="danger-full-access")
    s1, s2 = FakeSession("s1"), FakeSession("s2")  # 两个独立会话身份
    a1 = _agent_for(s1)
    a2 = _agent_for(s2)
    path = os.path.join(tempfile.mkdtemp(), "iso.txt")
    # 仅 s1 观测到存在（写）
    ctx.fs.write_text(path, "hello", actor=a1)
    # s2 从未观测 → edit 应 FS_NOT_OBSERVED
    try:
        ctx.fs.edit_text(path, "hello", "x", actor=a2)
        raise AssertionError("s2 未观测应抛 FS_NOT_OBSERVED")
    except FsError as exc:
        assert exc.code == FS_NOT_OBSERVED
    # s1 观测过 → edit 成功
    res = ctx.fs.edit_text(path, "hello", "world", actor=a1)
    assert res["replaced"] is True


# --------------------------------------------------------------------------- #
# 4. 版本守卫：两个会话交错写入触发 FS_STALE_VERSION
# --------------------------------------------------------------------------- #
def test_stale_version_across_sessions() -> None:
    ctx = _ctx(mode="danger-full-access")
    s1, s2 = FakeSession("s1"), FakeSession("s2")
    a1 = _agent_for(s1)
    a2 = _agent_for(s2)
    path = os.path.join(tempfile.mkdtemp(), "shared.txt")
    # s1 写（版本 1，s1 观测到版本 1）
    ctx.fs.write_text(path, "s1-v1", actor=a1)
    # s2 写（版本 2，s2 观测到版本 2；s1 的观测仍为版本 1）
    ctx.fs.write_text(path, "s2-v2", actor=a2)
    # s1 基于其陈旧观测版本 1 编辑 → 磁盘当前版本 2 ≠ 1 → FS_STALE_VERSION
    try:
        ctx.fs.edit_text(path, "s2-v2", "s1-edited", actor=a1)
        raise AssertionError("s1 陈旧观测应触发 FS_STALE_VERSION")
    except FsError as exc:
        assert exc.code == FS_STALE_VERSION
    # s2 基于版本 2 编辑 → 成功
    res = ctx.fs.edit_text(path, "s2-v2", "s2-edited", actor=a2)
    assert res["replaced"] is True


# --------------------------------------------------------------------------- #
# 5. 工具层透传 actor：edit 按会话隔离
# --------------------------------------------------------------------------- #
def test_tool_fs_edit_actor_routing() -> None:
    ctx = _ctx(mode="danger-full-access")
    s1 = FakeSession("s1")
    agent = _agent_for(s1)
    path = os.path.join(tempfile.mkdtemp(), "edit.txt")

    async def main() -> None:
        # s1 写（观测到存在，版本 1）
        out, is_error, _ = await ctx.tools.execute_with_agent(
            "write", json.dumps({"file_path": path, "content": "hello"}), agent=agent)
        assert not is_error, out
        # s1 edit 成功（基于观测版本 1）
        out, is_error, _ = await ctx.tools.execute_with_agent(
            "edit", json.dumps({"file_path": path, "old_string": "hello",
                                "new_string": "HELLO"}), agent=agent)
        assert not is_error, out
        # 全局（未观测）edit → 错误（FS_NOT_OBSERVED 经工具翻译）
        out, is_error, _ = await ctx.tools.execute_with_agent(
            "edit", json.dumps({"file_path": path, "old_string": "HELLO",
                                "new_string": "X"}), agent=None)
        assert is_error, f"未观测 edit 应被拒绝，实际：{out}"

    asyncio.run(main())


def main() -> None:
    tests = [
        test_gate_unit,
        test_edit_not_observed,
        test_edit_observed_missing,
        test_observation_isolated_per_session,
        test_stale_version_across_sessions,
        test_tool_fs_edit_actor_routing,
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

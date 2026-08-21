"""runtime-diagnostics/invariants 验证（第 3 层）。

运行：python dsh_py/tests/test_invariants.py

覆盖：
- 配置校验：空条目/周边空白/重复/非法正则 fail loud；
- 全局 enabled=False 与 allowlist/blocklist 过滤：不安装但保留名额（可查、不可重注册）；
- 正常安装：installer 在子 fiber 挂监听器、fail 报告器抛 InvariantError（code/packageName）；
- disposer：清理监听器 + 释放名额（可重注册）；
- 安装失败（installer 抛错）：释放名额并传播；
- installer 的 inject 依赖：缺失 → 安装失败释放名额。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.invariants import (
    InvariantError,
    apply as invariants_apply,
)


def _ctx(config=None):
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    invariants_apply(ctx, config or {})
    return ctx


# --------------------------------------------------------------------------- #
# 配置校验
# --------------------------------------------------------------------------- #
def test_config_validation_fail_loud():
    for bad in (
        {"package_allowlist": [""]},
        {"package_allowlist": [" padded "]},
        {"package_allowlist": ["a", "a"]},
        {"package_blocklist": ["["]},  # 非法正则
    ):
        try:
            _ctx(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"应拒绝配置：{bad}")


def test_package_name_validation():
    ctx = _ctx()
    for bad in ("", " padded ", "with space", "with\ttab"):
        try:
            ctx.invariants.register(bad, lambda c, f: None)
        except ValueError:
            pass
        else:
            raise AssertionError(f"应拒绝包名：{bad!r}")


# --------------------------------------------------------------------------- #
# 选择与过滤
# --------------------------------------------------------------------------- #
def test_disabled_keeps_reservation():
    ctx = _ctx({"enabled": False})
    called = []
    disposer = ctx.invariants.register("pkg.disabled", lambda c, f: called.append(1))
    assert called == []  # 未安装
    assert ctx.invariants.list() == ["pkg.disabled"]  # 名额保留
    try:
        ctx.invariants.register("pkg.disabled", lambda c, f: None)
    except ValueError:
        pass
    else:
        raise AssertionError("名额保留：重复注册应报错")
    disposer()
    assert ctx.invariants.list() == []  # 释放后可重注册
    ctx.invariants.register("pkg.disabled", lambda c, f: None)


def test_allowlist_and_blocklist_filtering():
    ctx = _ctx({"package_allowlist": ["^app\\.core"], "package_blocklist": ["^app\\.core\\.skip"]})
    installed = []
    ctx.invariants.register("app.core.api", lambda c, f: installed.append("api"))
    ctx.invariants.register("other.lib", lambda c, f: installed.append("other"))
    ctx.invariants.register("app.core.skip.x", lambda c, f: installed.append("skip"))
    assert installed == ["api"]  # 仅 allowlist 命中且未被 blocklist 排除
    assert set(ctx.invariants.list()) == {"app.core.api", "other.lib", "app.core.skip.x"}


# --------------------------------------------------------------------------- #
# 安装 / 报告 / 卸载
# --------------------------------------------------------------------------- #
async def test_install_listener_and_fail():
    ctx = _ctx()
    events = []

    def installer(c, fail):
        c.on("probe/event", lambda ev: events.append(ev))
        return None

    disposer = ctx.invariants.register("app.core.invariant", installer)
    ctx.emit("probe/event", "payload")
    assert events == ["payload"]

    # fail 报告器抛 InvariantError
    def failing_installer(c, fail):
        fail("core state mismatch")
        return None

    try:
        ctx.invariants.register("app.core.broken", failing_installer)
    except InvariantError as e:
        assert e.code == "INVARIANT"
        assert e.packageName == "app.core.broken"
        assert "core state mismatch" in str(e)
    else:
        raise AssertionError("fail 应抛 InvariantError")
    # 安装失败 → 名额释放
    assert "app.core.broken" not in ctx.invariants.list()

    # disposer：监听器清理 + 名额释放（可重注册）
    disposer()
    assert "app.core.invariant" not in ctx.invariants.list()
    events.clear()
    ctx.emit("probe/event", "after")
    assert events == []
    ctx.invariants.register("app.core.invariant", installer)  # 可重注册


async def test_installer_sync_error_releases_reservation():
    ctx = _ctx()

    def throwing_installer(c, fail):
        raise RuntimeError("boom")

    try:
        ctx.invariants.register("app.core.throw", throwing_installer)
    except RuntimeError:
        pass
    else:
        raise AssertionError("installer 抛错应传播")
    assert "app.core.throw" not in ctx.invariants.list()


async def test_installer_inject_missing_releases_reservation():
    ctx = _ctx()

    def needs_missing(c, fail):
        return None

    needs_missing.inject = ["noSuchService"]  # type: ignore[attr-defined]
    try:
        ctx.invariants.register("app.core.missing-dep", needs_missing)
    except RuntimeError:
        pass
    else:
        raise AssertionError("inject 依赖缺失应报错")
    assert "app.core.missing-dep" not in ctx.invariants.list()


async def test_installer_with_valid_inject():
    ctx = _ctx()

    def uses_tools(c, fail):
        assert c.tools is not None
        return None

    uses_tools.inject = ["tools"]  # type: ignore[attr-defined]
    disposer = ctx.invariants.register("app.core.tools-user", uses_tools)
    assert "app.core.tools-user" in ctx.invariants.list()
    disposer()


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    sync_tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and not asyncio.iscoroutinefunction(v)]
    fails = []
    for t in sync_tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            fails.append((t.__name__, repr(e)))
    for t in tests:
        try:
            asyncio.run(t())
        except Exception as e:  # noqa: BLE001
            fails.append((t.__name__, repr(e)))
    for name, err in fails:
        print(f"FAIL {name}: {err}")
    total = len(tests) + len(sync_tests)
    print(f"{total} 项，{len(fails)} 失败")
    if fails:
        raise SystemExit(f"\n{len(fails)} 项失败")


if __name__ == "__main__":
    _run_all()

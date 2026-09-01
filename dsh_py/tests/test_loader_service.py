"""LoaderService（``ctx.loader`` 只读投影）验证（host 范畴）。

运行：python dsh_py/tests/test_loader_service.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import boot
from dsh_py.services.loader import LoaderService


def _plugin(name):
    """造一个可装载的插件 apply（带模块/名称，便于投影 id 合成）。"""
    def apply_fn(ctx, config=None):  # noqa: ANN001
        ctx.provide(name, object())
    apply_fn.__name__ = name
    apply_fn.__qualname__ = name
    apply_fn.__module__ = f"dsh_py.plugins.{name}"
    return apply_fn


def test_entries_projection_fields():
    """投影每行含 id / module / enabled / phase，且字段值正确。"""
    ctx = AppContext()
    ls = LoaderService(ctx)

    alpha = _plugin("alpha")

    # 带 id 的启用行
    ls.record([{"id": "a", "apply": alpha, "config": {}}])
    # 无 id 行（id 由 模块:属性 合成）
    ls.record([{"apply": alpha}])
    # 被禁用行（enabled=False，phase=None）
    ls.record([{"id": "d", "apply": alpha, "disabled": True}])

    entries = ls.entries()
    by_id = {e["id"]: e for e in entries}

    a = by_id["a"]
    assert a["module"] == "dsh_py.plugins.alpha", a
    assert a["enabled"] is True
    assert a["phase"] == "active", a

    synth = by_id["dsh_py.plugins.alpha:alpha"]
    assert synth["enabled"] is True and synth["phase"] == "active", synth

    d = by_id["d"]
    assert d["enabled"] is False, d
    assert d["phase"] is None, d  # 禁用 → 无活 Fiber


def test_record_dedup_by_id_patch_semantics():
    """同 id 的新行覆盖旧行（对齐 patch 覆盖语义），无 id 行始终追加。"""
    ctx = AppContext()
    ls = LoaderService(ctx)
    p = _plugin("p")

    ls.record([{"id": "x", "apply": p}])
    assert len(ls.entries()) == 1
    # 同 id 再次 record → 覆盖而非翻倍
    ls.record([{"id": "x", "apply": p, "disabled": True}])
    assert len(ls.entries()) == 1
    assert ls.entries()[0]["enabled"] is False

    # 无 id 行追加两次 → 两条
    ls.record([{"apply": p}])
    ls.record([{"apply": p}])
    synth = [e for e in ls.entries() if e["id"] == "dsh_py.plugins.p:p"]
    assert len(synth) == 2, synth


def test_boot_populates_ctx_loader():
    """``boot`` 组合后写入 ``ctx.loader``，插件行可在投影中查到。"""
    ctx = AppContext()
    boot(ctx, [_plugin("svc_a"), _plugin("svc_b")])

    assert ctx.has_service("loader"), "boot 应惰性内置 ctx.loader"
    assert ctx.has_service("pluginInventory"), "boot 应惰性内置 ctx.pluginInventory"

    entries = ctx.loader.entries()
    ids = {e["id"] for e in entries}
    assert "dsh_py.plugins.svc_a:svc_a" in ids, ids
    assert "dsh_py.plugins.svc_b:svc_b" in ids, ids
    # 全部启用且 active
    for e in entries:
        assert e["enabled"] is True and e["phase"] == "active", e


def test_plugin_inventory_over_loader():
    """``pluginInventory.list()`` 直接透传 loader 投影。"""
    ctx = AppContext()
    boot(ctx, [_plugin("svc_c")])
    entries = ctx.pluginInventory.list()
    assert any(e["id"] == "dsh_py.plugins.svc_c:svc_c" for e in entries), entries


if __name__ == "__main__":
    test_entries_projection_fields()
    test_record_dedup_by_id_patch_semantics()
    test_boot_populates_ctx_loader()
    test_plugin_inventory_over_loader()
    print("test_loader_service: ALL PASS")

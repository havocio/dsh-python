"""PluginInventoryService（``ctx.pluginInventory`` 只读投影 + 网关 RPC）验证（host 范畴）。

运行：python dsh_py/tests/test_plugin_inventory.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.api.server import HarnessSdkJsonRpcServer
from dsh_py.core.context import AppContext
from dsh_py.loader import boot
from dsh_py.services.loader import LoaderService
from dsh_py.services.plugin_inventory import PluginInventoryService


class _FakeTransport:
    """最小 transport：notify 同步吞掉（测试 RPC 分支不需要真实连接）。"""
    def notify(self, method, params):  # noqa: ANN001, ANN201
        return None


def _plugin(name):
    def apply_fn(ctx, config=None):  # noqa: ANN001
        ctx.provide(name, object())
    apply_fn.__name__ = name
    apply_fn.__qualname__ = name
    apply_fn.__module__ = f"dsh_py.plugins.{name}"
    return apply_fn


def test_service_list_passthrough():
    """``pluginInventory.list()`` 透传 loader 投影（含字段）。"""
    ctx = AppContext()
    boot(ctx, [_plugin("svc_x")])
    entries = ctx.pluginInventory.list()
    assert any(e["id"] == "dsh_py.plugins.svc_x:svc_x" for e in entries), entries
    for e in entries:
        assert set(e.keys()) == {"id", "module", "enabled", "phase"}, e


def test_missing_loader_returns_empty():
    """缺 ``ctx.loader`` 时 list() 退化为空列表（不抛错）。"""
    ctx = AppContext()
    svc = PluginInventoryService(ctx)
    assert svc.list() == []


async def test_rpc_available():
    """网关 ``pluginInventory/list`` 在 pluginInventory 挂载时返回 available:True。"""
    ctx = AppContext()
    boot(ctx, [_plugin("svc_y")])
    server = HarnessSdkJsonRpcServer(ctx, _FakeTransport())
    result = await server._plugin_inventory_list()
    assert result["available"] is True, result
    assert any(e["id"] == "dsh_py.plugins.svc_y:svc_y" for e in result["entries"]), result


async def test_rpc_unavailable_without_service():
    """缺 ``ctx.pluginInventory`` 时网关分支返回 available:False。"""
    ctx = AppContext()
    # 只挂 loader、不挂 pluginInventory
    LoaderService(ctx)
    server = HarnessSdkJsonRpcServer(ctx, _FakeTransport())
    result = await server._plugin_inventory_list()
    assert result == {"available": False, "entries": []}, result


if __name__ == "__main__":
    test_service_list_passthrough()
    test_missing_loader_returns_empty()

    async def _run():
        await test_rpc_available()
        await test_rpc_unavailable_without_service()
    asyncio.run(_run())
    print("test_plugin_inventory: ALL PASS")

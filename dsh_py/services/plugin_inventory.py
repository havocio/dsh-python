"""Loader 只读投影服务（host/plugin-inventory，对标 dsh 的 ``PluginInventoryGateway``）。

``PluginInventoryGateway`` 注册 ``pluginInventory`` 服务并发布一个生成的 Remote
``pluginInventory/list``：每次调用直接读 ``ctx.loader.entries()``，跳过结构性分组行，
按 Loader 顺序返回各条目的 id、模块指示符、有效启用态与当前根 Fiber 阶段。

dsh_py 无 Remote/客户端装配（apiproxy 属桌面/UI 范畴、不在 Python 后端范围内），故本模块
只落地「只读投影服务」本身——``list()`` 即 RPC 的数据面；网关侧 ``pluginInventory/list``
分支已在 :mod:`dsh_py.api.server` 透传本服务。服务不缓存、不订阅、不变更 Loader 生命周期。
"""

from __future__ import annotations

from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service


class PluginInventoryService(Service):
    """``ctx.pluginInventory``：Loader 装载树的远程只读投影（仅 ``list()``）。"""

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx, "pluginInventory")

    def list(self) -> list[dict]:
        """返回当前 Loader 条目投影（缺 ``ctx.loader`` 时退化为空列表）。

        投影字段与 dsh ``pluginInventory/list`` 契约一致：``id`` / ``module`` /
        ``enabled`` / ``phase``。
        """
        if not self.ctx.has_service("loader"):
            return []
        return self.ctx.loader.entries()


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：注册 ``ctx.pluginInventory`` 服务（惰性，已存在则跳过）。"""
    if not ctx.has_service("pluginInventory"):
        PluginInventoryService(ctx, config or {})


apply.provides = ["pluginInventory"]
apply.inject: list[str] = []

"""文件系统观测策略（fs-observation-policy，对标 dsh 的 ``@deepseek-ai/dsh-fs-observation-policy``）。

纯事件插件（无服务）：监听 ``fs/write-intent`` / ``fs/edit-intent`` / ``fs/observed``
三个事件，维护**按 owner（会话）隔离**的文件观测态，并据之为意图瀑布提供单槽决策：

- ``fs/observed`` —— 同步记录某 ``(owner, target)`` 的存在 / 版本（读、写、编辑均广播）；
- ``fs/write-intent`` —— 推导 ``createIfAbsent`` / ``replaceIfVersion`` 决策（从未观测则
  交由后端默认；观测为缺失则允许创建；观测为存在则要求替换时版本匹配）；
- ``fs/edit-intent`` —— 推导版本守卫：从未观测 → :data:`FS_NOT_OBSERVED`；观测为缺失
  → :data:`FS_NOT_FOUND`；观测为存在 → 返回其观测版本（后端据之做磁盘版本比对）。

owner 由 ``actor.agent.session`` 推导（对齐 dsh 的跨会话隔离）；用 ``WeakKeyDictionary``
使会话对象 GC 后观测态自动回收，避免跨会话泄漏。监听器均为同步、异常可经瀑布传播。
"""

from __future__ import annotations

import weakref
from typing import Any

from dsh_py.core.context import AppContext
from dsh_py.services.fs import FS_NOT_FOUND, FS_NOT_OBSERVED, FsError
from dsh_py.services.sandbox_policy import owner_of


class ObservedStateGate:
    """按 owner 隔离的文件观测态门（对标 dsh 的 ``ObservedStateGate``）。"""

    def __init__(self) -> None:
        # owner(session) -> {absolute_path: {"present": bool, "version": int | None}}
        self._owners: "weakref.WeakKeyDictionary[Any, dict]" = weakref.WeakKeyDictionary()
        # 无归属 actor 时的全局槽
        self._global: dict = {}

    def _store_for(self, owner: Any) -> dict:
        if owner is None:
            return self._global
        store = self._owners.get(owner)
        if store is None:
            store = {}
            self._owners[owner] = store
        return store

    def _get(self, owner: Any, target: str) -> Any:
        return self._store_for(owner).get(target)

    # ------------------------------------------------------------------ #
    # fs/observed：记录观测态
    # ------------------------------------------------------------------ #
    def observe(self, payload: dict) -> None:
        """``fs/observed`` 监听器：记录 (owner, target) 的存在 / 版本。"""
        target = payload["path"]
        owner = owner_of(payload.get("actor"))
        self._store_for(owner)[target] = {
            "present": bool(payload.get("present", False)),
            "version": payload.get("version"),
        }

    # ------------------------------------------------------------------ #
    # fs/write-intent：单槽决策（不调用 next → 否决后续链路，本身即决策者）
    # ------------------------------------------------------------------ #
    def write_intent(self, payload: dict, next: Any = None) -> Any:
        target = payload["path"]
        owner = owner_of(payload.get("actor"))
        obs = self._get(owner, target)
        if obs is None:
            # 从未观测 → 交由后端默认（无条件写入）
            return None
        if not obs["present"]:
            # 观测为缺失 → 允许创建（不强制既有版本）
            return {"createIfAbsent": True, "replaceIfVersion": None}
        # 观测为存在 → 替换时要求版本匹配最近一次观测版本
        return {"createIfAbsent": False, "replaceIfVersion": obs["version"]}

    # ------------------------------------------------------------------ #
    # fs/edit-intent：版本守卫（可拒绝 → 抛 FsError 经瀑布传播）
    # ------------------------------------------------------------------ #
    def edit_intent(self, payload: dict, next: Any = None) -> Any:
        target = payload["path"]
        owner = owner_of(payload.get("actor"))
        obs = self._get(owner, target)
        if obs is None:
            raise FsError(FS_NOT_OBSERVED, f"fs/edit-intent 要求先观察目标：{target}")
        if not obs["present"]:
            raise FsError(FS_NOT_FOUND, f"fs/edit-intent 目标经观测为不存在，无法编辑：{target}")
        return {"version": obs["version"]}


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 fs 观测态门（3 个同步 ``fs/*`` 监听器，无服务提供）。"""
    gate = ObservedStateGate()
    ctx.on("fs/write-intent", gate.write_intent)
    ctx.on("fs/edit-intent", gate.edit_intent)
    ctx.on("fs/observed", gate.observe)


apply.provides = []   # 纯事件插件，不提供命名服务
apply.inject = ["fs"]  # 依赖：在 fs 后端加载之后注册监听器（保证事件词汇已存在）

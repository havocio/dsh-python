"""Loader 只读投影服务（host 范畴，对标 dsh 的 ``ctx.loader``）。

``host/plugin-inventory`` 需要一份「当前 Loader 装载树」的只读视图。dsh_py 的装载走
:func:`dsh_py.loader.boot` / :func:`dsh_py.loader.load_profile`，组合后的插件行只在
局部存在、且**没有** ``ctx.loader`` 服务。本模块补一个轻量 ``LoaderService`` 持有这些
行，并在装载时被填充，对外暴露 :meth:`LoaderService.entries` 投影。

投影每行只含（对齐 dsh ``pluginInventory/list`` 契约）：

- ``id``：装载条目 id（无显式 id 时由 ``模块:属性`` 合成）；
- ``module``：模块指示符（apply 函数的 ``__module__``，如 ``dsh_py.services.llm``）；
- ``enabled``：有效启用态（``disabled`` 标记为假即视为禁用）；
- ``phase``：根 Fiber 阶段——dsh_py 急切加载，故启用项恒为 ``"active"``、禁用项无活
  Fiber 记为 ``null``；``failed`` 集合供未来跟踪失败条目（默认空）。

dsh 的 ``pending/loading/unloading`` 等运行时阶段在 dsh_py 同步装载模型下不会出现，
这是与 dsh 的有意偏差（已在差距分析中标注）。
"""

from __future__ import annotations

from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service


def _synthesize_id(apply_fn: Any) -> str:
    """无显式 id 时由 apply 函数合成稳定 id。"""
    if not callable(apply_fn):
        return "<unknown>"
    module = getattr(apply_fn, "__module__", "")
    qualname = getattr(apply_fn, "__qualname__", getattr(apply_fn, "__name__", ""))
    return f"{module}:{qualname}"


class LoaderService(Service):
    """``ctx.loader``：装载树只读投影（含被禁用行，启用态由 ``enabled`` 区分）。

    由 :func:`dsh_py.loader.boot` / :func:`dsh_py.loader.load_profile` 在组合后填充
    （按 id 去重，后者覆盖前者，与 patch 语义一致）。服务本身不缓存、不订阅、不变更
    Loader 生命周期——Loader 仍是唯一权威。
    """

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx, "loader")
        # 规范化行：{"id"?, "apply", "config"?, "disabled"?}
        self._rows: list[dict] = []
        # 加载失败的条目 id（当前 dsh_py 急切装载，失败即中断组合，故通常为空）
        self._failed: set[str] = set()

    def record(self, rows: list[dict]) -> None:
        """填入（累积）一组规范化装载行。

        按 ``id`` 去重：同 id 的新行覆盖旧行（对齐 patch 覆盖语义）；无 id 行始终追加。
        """
        for row in rows:
            rid = row.get("id")
            if rid is not None:
                for index, existing in enumerate(self._rows):
                    if existing.get("id") == rid:
                        self._rows[index] = row
                        break
                else:
                    self._rows.append(row)
            else:
                self._rows.append(row)

    def mark_failed(self, entry_id: str) -> None:
        """标记某条目加载失败（供未来运行时阶段跟踪；当前组合失败即中断）。"""
        self._failed.add(entry_id)

    def entries(self) -> list[dict]:
        """当前装载树的只读投影（Loader 顺序，跳过结构性分组行——dsh_py 无此概念）。"""
        out: list[dict] = []
        for row in self._rows:
            apply_fn = row.get("apply")
            module = getattr(apply_fn, "__module__", "") if callable(apply_fn) else ""
            rid = row.get("id") or _synthesize_id(apply_fn)
            enabled = not row.get("disabled", False)
            # 急切加载模型：启用→active；禁用→无活 Fiber（null）；失败集合兜底
            phase = None if not enabled else ("failed" if rid in self._failed else "active")
            out.append({
                "id": rid,
                "module": module,
                "enabled": enabled,
                "phase": phase,
            })
        return out


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：注册 ``ctx.loader`` 服务（惰性，已存在则跳过）。"""
    if not ctx.has_service("loader"):
        LoaderService(ctx, config or {})


apply.provides = ["loader"]
apply.inject: list[str] = []

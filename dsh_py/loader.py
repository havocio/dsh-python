"""配置即插件清单（loader，对标 dsh 的 profile + cordis.patch 装配）。

一个 profile 就是一份**有序的 Python 列表**，每个元素描述一个插件及其配置：
- 字符串 ``"dsh_py.plugins.long_term_memory"``：按模块名导入并调用其 ``apply(ctx, None)``。
- 字符串 ``"dsh_py.services.agent:apply_loop"``：``模块:属性`` 形式，调用指定导出。
- 字典 ``{"module": "...", "config": {...}}``：导入模块并调用 ``apply(ctx, config)``。
- 字典 ``{"apply": <callable>, "config": {...}}``：直接调用可调用对象（便于测试内联）。
- 字典 ``{"id": "x", "plugin": "mod:apply", "config": {...}}``：带 id 的插件行
  （``plugin`` 与 ``module`` 等价，但允许 ``模块:属性`` 形式；id 供 patch 定位）。

**多 layer 合并（对标 cordis-plugin-include 的 applyEntryPatches）**：
:func:`compose_entries` 按层顺序合并插件行，并支持 patch 指令——
- ``{"id": "x", "config": {...}}``：覆盖 id 为 ``x`` 的行的配置（深合并）；
- ``{"id": "x", "disabled": True}``：禁用该行（加载时跳过）；
- ``{"insert": [行...]}`` 或 ``{"insert": {"before"/"after": id, "entries": [...]}}``：
  插入新行。
层顺序（对齐 dsh 的 boot）：bundle 层 → profile 用户层 → home patch → overlay。

**boot**：compose 合并 → 环境变量插值（``${VAR}``）→ 依赖拓扑排序 → 逐行加载。
"""

from __future__ import annotations

import importlib
from typing import Any, Callable, Optional, Union

from dsh_py.core.context import AppContext
from dsh_py.env import interpolate_env

ProfileEntry = Union[str, dict, Callable[..., None]]


def _resolve_apply(entry: ProfileEntry) -> tuple[Callable[..., None], Optional[dict]]:
    """把一个 profile 条目解析成 ``(apply, config)``。"""
    if callable(entry) and not isinstance(entry, str):
        return entry, None
    if isinstance(entry, str):
        if ":" in entry:
            # “模块:属性” 形式：精准指向某个插件入口（如 apply_loop）
            module_name, attr = entry.split(":", 1)
            module = importlib.import_module(module_name)
            apply_fn = getattr(module, attr, None)
            if apply_fn is None:
                raise AttributeError(f"插件模块 {module_name!r} 未导出 {attr!r}")
            return apply_fn, None
        module = importlib.import_module(entry)
        if not hasattr(module, "apply"):
            raise AttributeError(f"插件模块 {entry!r} 未导出 apply(ctx, config)")
        return module.apply, None
    if isinstance(entry, dict):
        config = entry.get("config")
        if entry.get("apply") is not None:
            return entry["apply"], config
        module_name = entry.get("module")
        if module_name is None:
            module_name = entry.get("plugin")  # plugin 与 module 等价，但允许 模块:属性
        if module_name is None:
            raise KeyError("插件条目缺少 'module'/'plugin' 或 'apply' 字段")
        if ":" in module_name:
            mod, attr = module_name.split(":", 1)
            module = importlib.import_module(mod)
            apply_fn = getattr(module, attr, None)
            if apply_fn is None:
                raise AttributeError(f"插件模块 {mod!r} 未导出 {attr!r}")
            return apply_fn, config
        module = importlib.import_module(module_name)
        if not hasattr(module, "apply"):
            raise AttributeError(f"插件模块 {module_name!r} 未导出 apply(ctx, config)")
        return module.apply, config
    raise TypeError(f"无法识别的 profile 条目类型：{type(entry)!r}")


def _normalize_row(entry: ProfileEntry) -> dict:
    """把一条普通 profile 条目规范化为 ``{"id"?, "apply", "config"?}``。"""
    if isinstance(entry, dict):
        row_id = entry.get("id")
        apply_fn, config = _resolve_apply(entry)
        row: dict = {"apply": apply_fn}
        if row_id is not None:
            row["id"] = row_id
        if config is not None:
            row["config"] = config
        return row
    apply_fn, config = _resolve_apply(entry)
    row = {"apply": apply_fn}
    if config is not None:
        row["config"] = config
    return row


def compose_entries(*layers: list) -> list[dict]:
    """把多层 profile 合并成最终插件行（对标 applyEntryPatches）。

    :param layers: 按应用顺序传入的层（bundle → 用户 → home patch → overlay）。
    :returns: 规范化的行列表（``{"id"?, "apply", "config"?}``，含 disabled 标记）。
    """
    entries: list[dict] = []

    def find_index(row_id: str) -> Optional[int]:
        for i, e in enumerate(entries):
            if e.get("id") == row_id:
                return i
        return None

    def merge_config(target: dict, patch: dict) -> dict:
        """浅层深合并：patch 里的 dict 值递归合并，其余覆盖。"""
        out = dict(target)
        for key, value in patch.items():
            if isinstance(out.get(key), dict) and isinstance(value, dict):
                out[key] = merge_config(out[key], value)
            else:
                out[key] = value
        return out

    for layer in layers:
        for item in layer:
            # patch 指令：insert
            if isinstance(item, dict) and "insert" in item and "apply" not in item and "plugin" not in item:
                ins = item["insert"]
                if isinstance(ins, dict):
                    before = ins.get("before")
                    after = ins.get("after")
                    rows = [ _normalize_row(e) for e in ins.get("entries", []) ]
                    if before is not None:
                        idx = find_index(before)
                        if idx is None:
                            raise RuntimeError(f"insert.before 引用了不存在的行 id {before!r}")
                        entries[idx:idx] = rows
                    elif after is not None:
                        idx = find_index(after)
                        if idx is None:
                            raise RuntimeError(f"insert.after 引用了不存在的行 id {after!r}")
                        entries[idx + 1:idx + 1] = rows
                    else:
                        entries.extend(rows)
                else:
                    entries.extend(_normalize_row(e) for e in ins)
                continue
            # patch 指令：id 定位的覆盖 / 禁用（普通行有 apply/plugin/module，不进这里）
            if isinstance(item, dict) and "apply" not in item and "plugin" not in item and "module" not in item:
                row_id = item.get("id")
                if row_id is not None:
                    idx = find_index(row_id)
                    if idx is None:
                        raise RuntimeError(f"patch 引用了不存在的行 id {row_id!r}")
                    if item.get("disabled") is True:
                        entries[idx]["disabled"] = True
                        continue
                    if "config" in item:
                        entries[idx]["config"] = merge_config(
                            entries[idx].get("config") or {}, item["config"])
                        continue
            # 普通行
            entries.append(_normalize_row(item))
    return entries


def _collect_entries(rows: list[dict]) -> list[tuple]:
    """把规范化的行转成 ``(apply_fn, config, inject, provides)`` 元组（拓扑用）。"""
    entries = []
    for row in rows:
        if row.get("disabled"):
            continue  # 禁用行不参与拓扑与加载
        apply_fn = row["apply"]
        inject = list(getattr(apply_fn, "inject", None) or [])
        provides = list(getattr(apply_fn, "provides", None) or [])
        entries.append((apply_fn, row.get("config"), inject, provides))
    return entries


def _topo_sort(entries: list[tuple]) -> list[tuple]:
    """按 ``inject``/``provides`` 声明做依赖拓扑排序（被依赖者先加载）。

    - 插件 A 声明 ``provides = ["x"]``，插件 B 声明 ``inject = ["x"]`` →
      A 必须先于 B 加载，即使列表顺序颠倒也会被纠正；
    - 无依赖约束的插件保持原顺序（稳定排序）；
    - 某个 ``inject`` 依赖全程无人提供 → 抛错（明确列出缺失）；
    - 检测到循环依赖 → 抛错。
    """
    providers: dict[str, list[int]] = {}   # 服务名 -> 提供它的条目下标
    for index, (_fn, _cfg, _inj, provides) in enumerate(entries):
        for name in provides:
            providers.setdefault(name, []).append(index)

    # 依赖缺失校验
    missing = set()
    for index, (_fn, _cfg, inject, _prov) in enumerate(entries):
        for name in inject:
            if name not in providers:
                missing.add(name)
    if missing:
        raise RuntimeError(f"profile 中缺少以下服务的提供者：{sorted(missing)}")

    # 循环依赖校验：边 = 提供者 -> 依赖者
    import collections
    out_edges: dict[int, list[int]] = {i: [] for i in range(len(entries))}
    in_degree = [0] * len(entries)
    for index, (_fn, _cfg, inject, _prov) in enumerate(entries):
        for name in inject:
            for provider in providers[name]:
                if provider == index:
                    continue
                out_edges[provider].append(index)
                in_degree[index] += 1

    queue = collections.deque(i for i in range(len(entries)) if in_degree[i] == 0)
    ordered: list[int] = []
    while queue:
        node = queue.popleft()
        ordered.append(node)
        for nxt in out_edges[node]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)
    if len(ordered) != len(entries):
        raise RuntimeError("profile 存在循环依赖（inject/provides 成环）")

    return [entries[i] for i in ordered]


def _load_rows(ctx: AppContext, rows: list[dict]) -> list:
    """按拓扑顺序加载规范化行；返回可卸载句柄列表。"""
    handles = []
    for apply_fn, config, _inject, _provides in _topo_sort(_collect_entries(rows)):
        handle = ctx.plugin(apply_fn, config, name=getattr(apply_fn, "__name__", None))
        handles.append(handle)
    return handles


def load_profile(ctx: AppContext, profile: list[ProfileEntry]) -> list:
    """加载单个 profile 的全部插件（兼容带 id 的条目；依赖拓扑自动排序）。

    每个条目经 ``ctx.plugin`` 加载——配置先经 ``Config`` schema 校验，插件注册
    的监听器/服务会记录到各自的 Fiber 上。返回值是各插件的可卸载句柄列表
    （对标 cordis loader 的 fiber 集合）：调用 ``handle.dispose()`` 即可卸载该
    插件并回收资源。
    """
    rows = [_normalize_row(entry) for entry in profile]
    return _load_rows(ctx, rows)


def boot(
    ctx: AppContext,
    *layers: list,
    env: dict | None = None,
) -> list:
    """完整装配：多 layer 合并 → 环境变量插值 → 拓扑排序 → 逐行加载。

    对标 dsh 的 ``boot`` 管线（bundle 层 + profile 用户层 + home patch + overlay）：
    - ``compose_entries(*layers)`` 合并各层（含 id 覆盖 / 禁用 / insert patch）；
    - 每行的 ``config`` 先经 :func:`dsh_py.env.interpolate_env` 做 ``${VAR}`` 插值；
    - 按 ``inject``/``provides`` 依赖拓扑排序后加载。
    :returns: 全部已加载插件的可卸载句柄列表。
    """
    rows = compose_entries(*layers)
    for row in rows:
        if row.get("config") is not None:
            row["config"] = interpolate_env(row["config"], env)
    return _load_rows(ctx, rows)


# 内置核心 profile（bundle 层）：核心 seam（+ 智能体循环）全部以插件形式装配。
# 带 id 供用户层 / overlay 以 patch 指令定位覆盖。
CORE_PROFILE: list[ProfileEntry] = [
    {"id": "llm", "plugin": "dsh_py.services.llm:apply"},            # llm 服务
    {"id": "sessions", "plugin": "dsh_py.services.session:apply"},   # 会话服务
    {"id": "tools", "plugin": "dsh_py.services.tools:apply"},        # 工具服务
    {"id": "agents", "plugin": "dsh_py.services.agent:apply_registry"},  # agents 注册表
    {"id": "agentLoop", "plugin": "dsh_py.services.agent:apply_loop"},   # 默认智能体循环
]


def bootstrap(ctx: AppContext) -> None:
    """装配框架核心服务——等价于加载 :data:`CORE_PROFILE`。

    这是「一切皆插件」的落地：核心 seam 不再被硬编码注册，而是作为普通 profile
    条目按序加载。想替换任意一部分时，改用你自己的 profile 调用
    :func:`load_profile` 或 :func:`boot` 即可，无需触碰框架代码。
    """
    load_profile(ctx, CORE_PROFILE)

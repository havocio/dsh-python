"""把一份预设组合挂到作用域上下文（dsh ``mount.ts`` 的 dsh_py 适配）。

dsh 经 cordis ``Include``/``EntryTree`` 把组合子树挂进 agent 作用域，并做两道
审计：未达可用状态的行、向根领域发布服务的行。dsh_py 的 loader 形态不同，
适配如下（均已注明）：

- **装载**：解析组合 YAML → 归一化行（``{name, config}`` → ``{"plugin", "config"}``，
  ``group`` 行递归展开扁平化）→ :func:`dsh_py.loader.load_profile` 装入
  作用域上下文。
- **「未激活行」审计**：dsh_py loader 的 ``_topo_sort`` 对缺失 inject 依赖**直接
  抛错**（行未达可用状态即整体拒绝）——该审计由 loader 自身承担，无需二次检查。
- **「泄漏到根领域的服务」审计**：dsh_py 无 isolate 领域概念；预设行提供的服务
  落在作用域上下文（沿父链对后代可见），不会污染进程全局——审计天然满足，省略。
- 组合文件是可写来源还是输入？dsh_py 的 ``load_profile`` 不写回任何文件
  （dsh 的 ``PresetTree.write()`` 显式 no-op 同理）。
"""

from __future__ import annotations

from typing import Any, Optional

from dsh_py.loader import load_profile
from dsh_py.services.agent_presets.preset import PresetMountError
from dsh_py.services.scope import scope_of, scope_parent_of


def normalize_rows(rows: Any) -> list:
    """把组合 YAML 行归一化为 dsh_py loader 的行格式（group 递归展开）。"""
    out: list = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue  # 形状检查已在 discovery 层做过（损坏即 broken）
        if row.get("group") is True:
            out.extend(normalize_rows(row.get("config")))
            continue
        name = row.get("name")
        if not isinstance(name, str) or name == "":
            continue
        entry: dict = {"plugin": name}
        if isinstance(row.get("config"), dict):
            entry["config"] = row["config"]
        out.append(entry)
    return out


def mount_preset(scope_ctx: Any, preset: dict) -> None:
    """在 ``scope_ctx`` 上装载预设组合；失败抛 :class:`PresetMountError`。

    dsh_py 的 load_profile 是同步装载（含拓扑排序）；组合不可用（行缺失依赖 /
    插件导入失败）会以 loader 的错误整体拒绝。
    """
    try:
        with open(preset["path"], encoding="utf-8") as fh:
            content = fh.read()
        import yaml  # 懒加载：可选依赖

        rows = yaml.safe_load(content) or []
        load_profile(scope_ctx, normalize_rows(rows))
    except PresetMountError:
        raise
    except Exception as exc:  # noqa: BLE001 -- 折叠为可读的挂载诊断
        raise PresetMountError(preset["id"], f"{exc} ({preset['path']})", cause=exc) from exc


def standing_mount_for(agent_ctx: Any, live_mounts: list) -> Optional[dict]:
    """一个 agent 加入的常驻组合；未加入返回 None。

    agent 自身键被父链指向其预设的常驻键——按匹配该父来查找（挂载不在 agent
    的 fiber 之下）。未加入预设的 agent（无 roster 的部署、加入前的子 agent）
    无父链，解析为 None。
    """
    agent_key = scope_of(agent_ctx)
    if agent_key is None:
        return None
    standing_key = scope_parent_of(agent_key)
    if standing_key is None:
        return None
    return next((m for m in live_mounts if m["key"] is standing_key), None)


__all__ = ["normalize_rows", "mount_preset", "standing_mount_for"]

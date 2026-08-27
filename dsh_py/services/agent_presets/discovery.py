"""Agent 预设的文件系统发现（dsh ``discovery.ts``）。

预设 = 一个目录，内含 :data:`COMPOSITION_FILE`（可选旁带 :data:`METADATA_FILE`）；
目录名即预设 id。发现每次调用都重读根——运行期创作的预设立即可见，被删除的
预设从下一次读取消失。

发现同时拥有预设健康：组合缺失或不可加载的目录**报告为损坏行**而非跳过
（跳过的目录仍占据其 id，复制路径拒绝该名却无任何表面可删除；坏组合在首个
会话挂载失败前会被当成普通预设）。

适配：``readdir/readFile/stat`` → 同步 ``os.scandir``/``open``/``os.stat``；
YAML 懒加载（``yaml`` 缺失时组合一律判损坏并注明原因）。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from dsh_py.services.agent_presets.metadata import read_preset_metadata
from dsh_py.services.agent_presets.preset import PRESET_ID

# 使目录成为预设的组合文件
COMPOSITION_FILE = "agent.cordis.yml"

# harness home 下本地创作预设的目录
USER_PRESET_DIR = ".agent-presets"


def entry_list_problem(rows: Any, at: str = "") -> Optional[str]:
    """组合文档能否被 loader 起步的形状检查；可加载返回 None。

    刻意止于 loader 的浅层检查：不解析插件名、不应用配置。能抓的是手工编辑
    出的「loader 根本无法开始」的文件——且必须接受 loader 接受的一切，故行只
    要求是携带插件 ``name`` 的映射（group 递归进自己的列表）。
    """
    if not isinstance(rows, list):
        return "the composition must be a top-level list of plugin rows" if at == "" else f"group {at} must hold a list of plugin rows"
    for index, row in enumerate(rows):
        label = f"row {index + 1}" if at == "" else f"{at} row {index + 1}"
        if not isinstance(row, dict):
            return f"{label} is not a plugin row (expected a map with a \"name\")"
        name = row.get("name")
        if not isinstance(name, str) or name == "":
            return f'{label} names no plugin (a "name" string is required)'
        if row.get("group") is True:
            nested = entry_list_problem(row.get("config"), label)
            if nested is not None:
                return nested
    return None


def composition_problem(path: str) -> Optional[str]:
    """组合为何不能挂载；可加载返回 None。用 loader 自己的 YAML 方言解析，
    健康检查绝不会把 loader 会接受的组合判为损坏。"""
    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return f"the composition file {COMPOSITION_FILE} cannot be read"
    try:
        import yaml  # 懒加载：可选依赖

        rows = yaml.safe_load(content)
    except ImportError:
        return f"the composition file {COMPOSITION_FILE} cannot be parsed: PyYAML is not installed"
    except Exception as exc:  # noqa: BLE001 -- YAML 解析失败（首行即可读信息）
        first_line = str(exc).splitlines()[0] if str(exc) else "unparsable"
        return f"the composition is not valid YAML: {first_line}"
    return entry_list_problem(rows)


def _is_file(path: str) -> bool:
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def scan_root(root: dict) -> list:
    """扫描一个根下的预设目录；缺失根返回空（用户根在首个本地预设前不存在）。"""
    raw_path = root["path"]
    if raw_path.startswith("~"):
        raw_path = os.path.expanduser(raw_path)
    directory = os.path.abspath(raw_path)
    try:
        children = list(os.scandir(directory))
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RuntimeError(f"agent-presets: cannot read preset root {directory}: {exc}") from exc
    found: list = []
    for child in children:
        try:
            is_dir = child.is_dir()
        except OSError:
            continue
        if not is_dir or PRESET_ID.match(child.name) is None:
            continue
        child_dir = os.path.join(directory, child.name)
        path = os.path.join(child_dir, COMPOSITION_FILE)
        if _is_file(path):
            broken = composition_problem(path)
        else:
            broken = (
                f"the composition file {COMPOSITION_FILE} is missing — the directory still "
                "occupies the id; delete it or restore the file"
            )
        metadata = read_preset_metadata(child_dir)
        entry: dict = {"id": child.name, "trust": root["trust"], "path": path, **metadata}
        if broken is not None:
            entry["broken"] = broken
        found.append(entry)
    # 声明的 order 优先（shipped 集按能力阅读）；其余按 id 兜底（创作预设稳定）
    return sorted(
        found,
        key=lambda p: (p.get("order") if p.get("order") is not None else float("inf"), p["id"]),
    )


def discover_presets(roots: list) -> list:
    """按优先级扫描所有根；早根胜出重复 id。"""
    by_id: dict = {}
    for root in roots:
        for preset in scan_root(root):
            if preset["id"] in by_id:
                continue
            by_id[preset["id"]] = preset
    return list(by_id.values())


__all__ = [
    "COMPOSITION_FILE",
    "USER_PRESET_DIR",
    "entry_list_problem",
    "composition_problem",
    "scan_root",
    "discover_presets",
]

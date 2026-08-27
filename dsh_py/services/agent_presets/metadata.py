"""预设显示元数据（``preset.yml``）：选择器展示的名称与描述。

组合文件是插件行顶层列表——YAML 无法在其旁承载兄弟键，伪造元数据行会把
Loader 不该加载的东西喂给它；独立成文件也让组合文件保持纯粹。

文件只承载显示文本：``id`` 是目录名、``trust`` 来自发现根，二者不可在此写入
（否则本地预设可自称 shipped）。任何读取失败降级为无元数据——展示不是能力，
坏名字绝不能让 agent 无法启动。

适配：懒加载 ``yaml``；缺失时按「无元数据」处理（发现不因缺依赖失败）。
"""

from __future__ import annotations

import os
from typing import Any, Optional

METADATA_FILE = "preset.yml"


def _text(value: Any) -> Optional[str]:
    """非空去空白字符串；其余返回 None。"""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed if trimmed != "" else None


def read_preset_metadata(directory: str) -> dict:
    """读一个预设目录的显示元数据；缺失/不可解析/形状错误都视为空。"""
    path = os.path.join(directory, METADATA_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return {}
    try:
        import yaml  # 懒加载：可选依赖

        parsed = yaml.safe_load(raw)
    except Exception:  # noqa: BLE001 -- 坏元数据不配让发现失败
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict = {}
    name = _text(parsed.get("name"))
    description = _text(parsed.get("description"))
    order = parsed.get("order")
    if name is not None:
        result["name"] = name
    if description is not None:
        result["description"] = description
    if isinstance(order, (int, float)) and order == order:  # 有限数
        result["order"] = order
    return result


def render_preset_metadata(metadata: dict) -> Optional[str]:
    """渲染显示元数据为 YAML 文档；无任何字段时返回 None（不写空文档）。"""
    name = _text(metadata.get("name"))
    description = _text(metadata.get("description"))
    order = metadata.get("order")
    if name is None and description is None and order is None:
        return None
    payload: dict = {}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if order is not None:
        payload["order"] = order
    try:
        import yaml  # 懒加载：可选依赖

        return yaml.safe_dump(payload, sort_keys=False)
    except Exception:  # noqa: BLE001 -- 渲染失败视为无可写内容
        return None


__all__ = ["METADATA_FILE", "read_preset_metadata", "render_preset_metadata"]

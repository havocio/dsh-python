"""启动期环境快照（util/launch-environment，对标 dsh 的 ``dsh-launch-environment``）。

记录每次启动时哪个层提供了每个值（process / 项目 .env / DSH home .env），
Harness 消费方经它解析而不是拍平的 ``os.environ``；launcher 仍可把已接受的值
物化给配置表达式与第三方库。

- :func:`create_launch_environment_snapshot` —— 从各层内容构建不可变快照
  （复制每一层，后续变更不影响快照；Windows 折叠大小写名，POSIX 精确）；
- :func:`launch_environment_of` —— 取 launcher 快照；宿主未提供时以继承环境
  为唯一层；
- ``apply`` —— 插件入口：在任意配置条目挂载前填充 ``ctx.launchEnvironment``
  （基于 :mod:`dsh_py.env` 的 .env 解析，但保留分层来源信息）。

**与 dsh 的差异（已注明）**：dsh 由 launcher 进程在 boot 前填充该槽；dsh_py
以 profile 插件（``provides=["launchEnvironment"]``）按依赖拓扑加载，效果等价
——依赖它的插件会被排序在其后。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.env import parse_env
from dsh_py.util.home_paths import resolve_dsh_home

# 启动环境来源（信任度从高到低）
SOURCE_ORDER = ("process", "project-env", "user-env")

# launcher 填充此 run 快照的 ctx 槽键
DSH_LAUNCH_ENVIRONMENT_KEY = "launchEnvironment"


def _lookup_key(name: str) -> str:
    """一个变量名解析所依据的键：Windows 环境名大小写不敏感，其余平台精确。"""
    return name.upper() if os.name == "nt" else name


def create_launch_environment_snapshot(layers: list[dict]) -> dict:
    """从各层内容构建不可变快照；层内 key 按平台折叠。

    :param layers: ``{"source", "path"?, "values"}`` 列表（任意顺序；结果按
        规范信任序搜索）。
    :returns: ``{"get", "getFrom"}`` 快照。
    """
    by_source: dict[str, dict] = {}
    for layer in layers:
        entry: dict = {"values": {_lookup_key(k): v for k, v in layer["values"].items()}}
        if layer.get("path") is not None:
            entry["path"] = layer["path"]
        by_source[layer["source"]] = entry

    def get_from(name: str, sources: tuple[str, ...]) -> Optional[dict]:
        key = _lookup_key(name)
        for source in SOURCE_ORDER:
            if source not in sources:
                continue
            layer = by_source.get(source)
            if layer is None:
                continue
            value = layer["values"].get(key)
            if value is None:
                continue
            entry: dict = {"value": value, "source": source}
            if "path" in layer:
                entry["path"] = layer["path"]
            return entry
        return None

    def get(name: str) -> Optional[dict]:
        return get_from(name, SOURCE_ORDER)

    return {"get": get, "getFrom": get_from}


def launch_environment_of(ctx: AppContext) -> dict:
    """取 launcher 的快照；宿主未提供时以继承环境为唯一层。"""
    snapshot = getattr(ctx, DSH_LAUNCH_ENVIRONMENT_KEY, None)
    if snapshot is not None:
        return snapshot
    return create_launch_environment_snapshot([
        {"source": "process", "values": dict(os.environ)},
    ])


def _load_env_file(path: str) -> dict:
    """读取一份 .env 文件为值映射；缺失返回空。"""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return parse_env(f.read())


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：在配置条目挂载前填充 ``ctx.launchEnvironment`` 快照。"""
    cfg = config or {}
    cwd = cfg.get("cwd") or os.getcwd()
    home = cfg.get("home") or resolve_dsh_home()
    layers = [
        {"source": "process", "values": dict(os.environ)},
    ]
    project_env = os.path.join(cwd, ".env")
    if os.path.exists(project_env):
        layers.append({"source": "project-env", "path": project_env,
                       "values": _load_env_file(project_env)})
    user_env = os.path.join(home, ".env")
    if os.path.exists(user_env):
        layers.append({"source": "user-env", "path": user_env,
                       "values": _load_env_file(user_env)})
    ctx.provide(DSH_LAUNCH_ENVIRONMENT_KEY, create_launch_environment_snapshot(layers))


apply.name = "launch-environment"
apply.provides = ["launchEnvironment"]

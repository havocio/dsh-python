"""统一运行配置（替代环境变量散配）。

dsh 原版把 api key / 连接参数 / 模型参数全部塞进环境变量；本项目提供一个
**配置文件中心**（``dsh_config.py``），把 key、数据库、模型参数、工作目录等
集中管理。加载顺序（后者覆盖前者，逐项深合并）：

1. ``--config`` 显式指定的文件（不存在则 fail loud）；
2. 项目默认 ``dsh_py/configs/dsh_config.py``（唯一装配点，随仓库走）；
3. 机器级 ``~/.dsh/dsh_config.py``（个人覆盖，不进仓库）。

配置内容是一个 Python dict（``CONFIG`` 常量），理由与 profile 相同：零依赖、
可写注释、可做简单计算。加载后所有字符串值会做 ``${VAR}`` 插值——**默认写
明文即可**，``${VAR}`` 只是可选的兜底手段（例如想从 CI 注入时）。

装配方式：调用方（CLI / SDK）在 ``boot`` 后执行
``ctx.provide("appConfig", config)``，任何插件可用
``ctx.appConfig.get("llm.api_key")`` 读取（``__getattr__`` 自动解析服务）。
适配器的 api key 解析优先级：**配置文件 > credentials 服务 > 环境变量**。
"""

from __future__ import annotations

import importlib.util
import os
from typing import Any, Optional

from dsh_py.env import interpolate_env

# 项目默认配置文件（唯一装配点，与 profile.py 同目录）
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "configs", "dsh_config.py")
# 机器级配置文件（个人覆盖，不进仓库）
HOME_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".dsh", "dsh_config.py")


class AppConfig:
    """运行配置视图：包装 ``CONFIG`` dict，支持点路径读取。

    ::

        config = AppConfig({"llm": {"api_key": "sk-1"}})
        config.get("llm.api_key")          # "sk-1"
        config.get("llm.temperature", 0.7) # 缺省兜底
        "llm.api_key" in config            # True
    """

    def __init__(self, data: Optional[dict] = None) -> None:
        self._data: dict = dict(data or {})

    @property
    def data(self) -> dict:
        """底层配置 dict（只读约定，调用方不要原地修改）。"""
        return self._data

    def get(self, path: str, default: Any = None) -> Any:
        """按点路径取值：``"llm.api_key"`` → ``data["llm"]["api_key"]``。

        中间节点不是 dict 或路径缺失时返回 ``default``（不抛错）。
        """
        node: Any = self._data
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def __getitem__(self, path: str) -> Any:
        value = self.get(path, _MISSING)
        if value is _MISSING:
            raise KeyError(f"配置中没有 {path!r}")
        return value

    def __contains__(self, path: str) -> bool:
        return self.get(path, _MISSING) is not _MISSING

    def __repr__(self) -> str:
        return f"AppConfig({self._data!r})"


class _Missing:
    pass


_MISSING = _Missing()


def _deep_merge(base: dict, overlay: dict) -> dict:
    """递归深合并：同名 dict 递归合并，其余值以 overlay 覆盖。"""
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_py_config(path: str) -> dict:
    """从 .py 文件加载 ``CONFIG`` 常量（对齐 profile 的加载方式）。"""
    spec = importlib.util.spec_from_file_location("dsh_config_mod", path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return dict(getattr(module, "CONFIG", {}) or {})


def load_app_config(explicit: Optional[str] = None, env: Optional[dict] = None) -> AppConfig:
    """加载并合并运行配置。

    :param explicit: ``--config`` 显式路径；给定则只加载它（不存在 fail loud）。
    :param env: 插值使用的环境快照；缺省为当前 ``os.environ``。
    :returns: :class:`AppConfig`。没有配置文件时返回空配置（向后兼容：
        不配也能跑，key 走 credentials / 环境变量兜底）。
    """
    if explicit is not None:
        if not os.path.exists(explicit):
            raise FileNotFoundError(f"配置文件不存在：{explicit}")
        return AppConfig(interpolate_env(_load_py_config(explicit), env))

    merged: dict = {}
    for path in (DEFAULT_CONFIG_PATH, HOME_CONFIG_PATH):
        if os.path.exists(path):
            merged = _deep_merge(merged, _load_py_config(path))
    return AppConfig(interpolate_env(merged, env))


def get_app_config(ctx: Any) -> Optional[AppConfig]:
    """从上下文取已注入的配置服务；未注入返回 ``None``（适配器兜底用）。"""
    if ctx is not None and ctx.has_service("appConfig"):
        return ctx.appConfig
    return None

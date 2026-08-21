"""DSH 数据根目录与共享路径（util/home-paths，对标 dsh 的 ``dsh-home-paths``）。

统一「Harness home」的解析：显式配置 > ``$DSH_HOME`` > ``~/.dsh``。本模块是
home 路径的唯一权威——既有分散实现（attachment-local、command-feedback 的内联
解析）应迁移到此。

- :func:`expand_home_path` —— 展开 ``~`` / ``~/`` / ``~\\`` 前缀；
- :func:`resolve_dsh_home` —— 解析单根 DSH home（空/全空白 ``$DSH_HOME``
  视为未设置，空白覆盖绝不把 home 解析到当前工作目录）；
- :func:`dsh_home_path` —— 把段接到 DSH home 之下；
- :func:`dsh_home_display` —— 面向用户的符号化展示（绝不返回绝对机器路径）；
- :func:`canonicalize_watch_path` —— 给原生文件监视器一个路径的规范拼写
  （最深存在祖先经 realpath，缺失后缀还原；防止 Windows 把常规文件祖先当作
  普通缺失、防止短名别名与长路径混用）。
"""

from __future__ import annotations

import os
from typing import Optional

# 默认 DSH home 的目录名（OS home 之下）
DSH_HOME_DIR_NAME = ".dsh"
# 默认 DSH home 的稳定用户面展示形式
DEFAULT_DSH_HOME_DISPLAY = f"~/{DSH_HOME_DIR_NAME}"
# 覆盖默认 DSH home 的环境变量
DSH_HOME_ENV = "DSH_HOME"


def expand_home_path(path: str) -> str:
    """展开受支持的波浪号前缀（``~``、``~/``、``~\\``）到操作系统 home。"""
    if path == "~":
        return os.path.expanduser("~")
    if path.startswith("~/") or path.startswith("~\\"):
        return os.path.join(os.path.expanduser("~"), path[2:])
    return path


def resolve_dsh_home(
    configured: Optional[str] = None,
    env: Optional[dict] = None,
) -> str:
    """解析单根 DSH home：显式配置 > ``$DSH_HOME`` > ``~/.dsh``。

    :param configured: 显式 home 覆盖（最高优先级）。
    :param env: 读取 ``DSH_HOME`` 的环境映射；缺省 ``os.environ``。
    :returns: 归一化的绝对 DSH home 路径。
    """
    env = env if env is not None else os.environ
    from_env = env.get(DSH_HOME_ENV)
    selected = configured
    if selected is None:
        if from_env is not None and from_env.strip() != "":
            selected = from_env
        else:
            selected = os.path.join(os.path.expanduser("~"), DSH_HOME_DIR_NAME)
    return os.path.abspath(expand_home_path(selected))


def dsh_home_path(*segments: str) -> str:
    """把路径段接到解析出的 DSH home 之下；空段返回 home 本身。"""
    return os.path.join(resolve_dsh_home(), *segments)


def dsh_home_display(resolved_home: str) -> str:
    """符号化描述一个解析出的 DSH home（绝不返回绝对机器路径）。

    默认 home 标为 ``~/.dsh``；任何配置的 home 标为 ``$DSH_HOME``。
    """
    default = os.path.abspath(os.path.join(os.path.expanduser("~"), DSH_HOME_DIR_NAME))
    if os.path.abspath(resolved_home) == default:
        return DEFAULT_DSH_HOME_DISPLAY
    return f"${DSH_HOME_ENV}"


def canonicalize_watch_path(path: str) -> str:
    """给原生文件监视器一个路径的规范拼写（缺失最终组件时也成立）。

    最深存在祖先经 ``realpath`` 解析；后缀缺失时，该祖先还须被证明是可枚举
    目录，再把后缀还原拼接。遍历遇到缺失以外的错误时抛错。
    """
    current = os.path.abspath(path)
    missing: list[str] = []
    while True:
        try:
            os.stat(current)  # 存在性权威：FileNotFoundError 之外一律上抛
            if missing and not os.path.isdir(current):
                raise NotADirectoryError(
                    f"canonicalize_watch_path: {current!r} 不是目录（但它是 "
                    f"{path!r} 缺失后缀的既有祖先）",
                )
            return os.path.join(os.path.realpath(current), *reversed(missing))
        except FileNotFoundError:
            parent = os.path.dirname(current)
            if parent == current:  # 已到文件系统根
                return os.path.realpath(current)
            missing.append(os.path.basename(current))
            current = parent

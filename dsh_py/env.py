"""环境分层加载与插值（对标 dsh 的 ``loadEnv`` / ``loadLayeredEnv``）。

- :func:`parse_env` —— 解析一份 ``KEY=VALUE`` 文本（.env 格式）。
- :func:`load_layered_env` —— 继承环境 > 调用目录 ``.env`` > Harness home ``.env``
  三层合并（已有变量不被覆盖，对齐 dsh 的「accepted values are materialized
  without replacing inherited ones」）；拒绝由 .env 设置 bootstrap-only 变量
  （``DSH_`` / ``XDG_`` / ``DYLD_`` 前缀），因为它决定进程如何启动。
- :func:`interpolate_env` —— 递归替换配置值字符串中的 ``${VAR}``（对标
  cordis.yml 的环境变量插值），用于 profile 条目加载前。
"""

from __future__ import annotations

import os
import re
from typing import Any

# .env 文件禁止设置的变量名前缀（对齐 dsh 的 BOOTSTRAP_PREFIXES 核心项）
BOOTSTRAP_PREFIXES = ("DSH_", "XDG_", "DYLD_")

# 匹配 ${VAR} 或 $VAR
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def parse_env(text: str) -> dict[str, str]:
    """解析 ``KEY=VALUE`` 文本（.env 格式）：忽略空行与 # 注释，去除引号。"""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def _apply_layer(values: dict[str, str], layer: dict[str, str], path: str) -> None:
    """把一层 .env 并入结果；bootstrap-only 变量由 .env 设置时拒绝（fail loud）。"""
    for name in layer:
        if name.upper().startswith(BOOTSTRAP_PREFIXES):
            raise ValueError(
                f"{path} 设置了 {name!r}，该变量只能由启动环境提供"
                "（它决定进程如何启动 / 代码与指令从哪里加载）；请改为 export"
            )
    for name, value in layer.items():
        # 已有变量（继承环境或更早的层）不被覆盖
        values.setdefault(name, value)


def load_layered_env(
    cwd: str | None = None,
    home: str | None = None,
) -> dict[str, str]:
    """加载三层环境：继承环境 > 调用目录 ``.env`` > Harness home ``.env``。

    :param cwd: 调用目录（其 ``.env`` 是项目层）；缺省取当前工作目录。
    :param home: Harness home（其 ``.env`` 是机器层）；缺省取 ``~/.dsh``。
    :returns: 合并后的环境快照（继承变量优先，未被任何 .env 覆盖）。
    """
    cwd = cwd or os.getcwd()
    home = home or os.path.join(os.path.expanduser("~"), ".dsh")
    merged = dict(os.environ)
    for base in (cwd, home):
        path = os.path.join(base, ".env")
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except OSError as exc:
            raise ValueError(f"读取 {path} 失败：{exc}") from exc
        _apply_layer(merged, parse_env(content), path)
    return merged


def _interpolate_scalar(value: Any, env: dict[str, str]) -> Any:
    """对单个值做插值：字符串替换 ``${VAR}`` / ``$VAR``，其余类型原样返回。"""
    if not isinstance(value, str):
        return value

    def repl(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        return env.get(name, match.group(0))  # 未定义的变量保留原文

    return _ENV_PATTERN.sub(repl, value)


def interpolate_env(value: Any, env: dict[str, str] | None = None) -> Any:
    """递归替换配置值字符串中的 ``${VAR}``（dict / list 逐项处理）。

    :param value: 配置文件里的任意值（字符串 / dict / list / 标量）。
    :param env: 插值使用的环境快照；缺省为当前 ``os.environ``。
    :returns: 插值后的新结构（不修改原对象）。
    """
    env = env if env is not None else os.environ
    if isinstance(value, dict):
        return {k: interpolate_env(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate_env(item, env) for item in value]
    return _interpolate_scalar(value, env)

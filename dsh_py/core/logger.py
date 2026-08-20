"""内置日志服务（对标 cordis 的 ``LoggerService``）。

框架内核零依赖，日志输出到标准错误（``sys.stderr``），可由 ``set_level``
动态调整级别。支持按级别过滤与结构化字段，所有组件通过 ``ctx.logger``
统一记录，便于在长会话 / 多作用域场景下做结构化排查。

级别（与标准库 ``logging`` 一致）：``DEBUG(10) < INFO(20) < WARN(30) < ERROR(40)``。
初始化时若已存在 ``appConfig`` 服务，则读取其 ``log_level`` 字段作为默认级别。
"""
from __future__ import annotations

import sys
from typing import Any

from dsh_py.core.service import Service

# 级别名 -> 数值（越小越详细）
_LEVELS: dict[str, int] = {
    "DEBUG": 10,
    "INFO": 20,
    "WARN": 30,
    "WARNING": 30,
    "ERROR": 40,
}


class LoggerService(Service):
    """结构化日志服务（``name='logger'``）。"""

    def __init__(self, ctx: Any, name: str = "logger") -> None:
        super().__init__(ctx, name)
        self._level = self._resolve_level(ctx)
        self._scope = name

    @staticmethod
    def _resolve_level(ctx: Any) -> str:
        """从 ``appConfig.log_level`` 读取默认级别；缺失或异常时回退 INFO。"""
        try:
            if ctx.has_service("appConfig"):
                return str(ctx.appConfig.get("log_level", "INFO")).upper()
        except Exception:
            pass
        return "INFO"

    def set_level(self, level: str) -> None:
        """动态调整日志级别（如 ``"DEBUG"`` / ``"WARN"``）。"""
        self._level = str(level).upper()

    def _emit(self, level: str, msg: str, **fields: Any) -> None:
        if _LEVELS.get(level, 20) < _LEVELS.get(self._level, 20):
            return
        extra = ""
        if fields:
            extra = " " + " ".join(f"{k}={fields[k]!r}" for k in fields)
        # 框架零依赖：直接写入 stderr，不引入 logging 模块
        print(f"[{level}] [{self._scope}] {msg}{extra}", file=sys.stderr)

    def debug(self, msg: str, **fields: Any) -> None:
        self._emit("DEBUG", msg, **fields)

    def info(self, msg: str, **fields: Any) -> None:
        self._emit("INFO", msg, **fields)

    def warn(self, msg: str, **fields: Any) -> None:
        self._emit("WARN", msg, **fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._emit("WARN", msg, **fields)

    def error(self, msg: str, **fields: Any) -> None:
        self._emit("ERROR", msg, **fields)

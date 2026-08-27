"""会话实际运行预设的日志记录（dsh ``session.ts``）。

创建头部命名会话**起始**的预设，且被深冻结（创建事实）。空白窗口内会话仍可能
换预设，换的效果延续到第一个回合及其后——记录换正是保持日志诚实所必需（模型
可见 ⟺ 已记录的仓库规则：预设决定模型看到的工具 schema 与提示片段）。

重建读 :func:`resolve_session_preset`，绝不只读头部。

适配：dsh_py 的 ``SessionHeader`` 无 ``agentPreset`` 字段（dsh 有）——用
``getattr(header, "agentPreset", None)`` 兼容；事件以 ``event.type`` / ``event.data``
访问（与 dsh_py 会话事件形状一致）。
"""

from __future__ import annotations

from typing import Any, Optional


def resolve_session_preset(session: Any) -> Optional[str]:
    """会话实际运行的预设，最新一次选择胜出；无则回退头部创建值。

    :param session: 提供 ``header``（创建头部）与 ``events``（事件日志，旧→新）。
    :returns: 预设 id；部署不组合任何预设时返回 None。
    """
    for event in reversed(session.events):
        if getattr(event, "type", None) == "agent-preset/selected":
            data = getattr(event, "data", None)
            value = data.get("agentPreset") if isinstance(data, dict) else None
            if isinstance(value, str):
                return value
    header = getattr(session, "header", None)
    if header is not None:
        value = getattr(header, "agentPreset", None)
        if isinstance(value, str):
            return value
    return None


__all__ = ["resolve_session_preset"]

"""首条人类消息模型 provider（session-title-first-prompt-llm）：仅用首条人类消息生成标题。

对标 dsh 的 ``dsh-session-title-first-prompt-llm`` 插件：注册一个 ``first-prompt``
节奏的模型 provider——仅当会话出现第一条合格人类消息时生成一次标题。
"""

from __future__ import annotations

from typing import Any

from dsh_py.core.context import AppContext
from dsh_py.core import schema as z
from dsh_py.services.session_title import SessionTitleUserMessage
from dsh_py.services.session_title_llm import (
    SessionTitleLlmConfigFields,
    register_session_title_llm_provider,
)

#: 插件名（loader 诊断）
name = "session-title-first-prompt-llm"
inject = ["sessionTitle", "llm", "sessions"]

#: 配置 schema（与 all-prompts 共享字段校验器）
Config = z.object({
    "targetWords": SessionTitleLlmConfigFields["targetWords"],
    "targetCjkCharacters": SessionTitleLlmConfigFields["targetCjkCharacters"],
    "maxInputBytes": SessionTitleLlmConfigFields["maxInputBytes"],
    "maxOutputTokens": SessionTitleLlmConfigFields["maxOutputTokens"],
    "timeoutMs": SessionTitleLlmConfigFields["timeoutMs"],
    "provider": SessionTitleLlmConfigFields["provider"],
    "model": SessionTitleLlmConfigFields["model"],
}, extra="strip")


def apply(ctx: AppContext, config: Any = None) -> None:
    """注册首条消息模型 provider。"""
    register_session_title_llm_provider(
        ctx, config, name, "first-prompt",
        lambda messages: [messages[0]] if messages else [],
    )

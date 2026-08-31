"""全部人类消息模型 provider（session-title-all-prompts-llm）：每条人类消息都重新生成标题。

对标 dsh 的 ``dsh-session-title-all-prompts-llm`` 插件：注册一个 ``all-prompts``
节奏的模型 provider——每条新的人类消息到达都会触发一次标题修订。
"""

from __future__ import annotations

from typing import Any

from dsh_py.core.context import AppContext
from dsh_py.core import schema as z
from dsh_py.services.session_title_llm import (
    SessionTitleLlmConfigFields,
    register_session_title_llm_provider,
)

#: 插件名（loader 诊断）
name = "session-title-all-prompts-llm"
inject = ["sessionTitle", "llm", "sessions"]

#: 配置 schema（与 first-prompt 共享字段校验器）
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
    """注册全消息模型 provider。"""
    register_session_title_llm_provider(
        ctx, config, name, "all-prompts", lambda messages: list(messages),
    )

"""会话标题模型 provider 共享策略（session-title-llm）：路由 / 框架 / 超时 / 调用策略（对标 dsh 的 ``dsh-session-title-llm``）。

本模块是 helper（非插件）：两个 provider 插件（first-prompt / all-prompts）共用这里的
:func:`register_session_title_llm_provider` 与 :func:`generate_session_title_with_llm`，
把配置校验、系统提示、JSON 框架、超时熔断与模型调用折叠封装起来。
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

from dsh_py.core import schema as z
from dsh_py.core.signal import CancelSignal, SignalCancelledError
from dsh_py.services.llm import GenerateOptions
from dsh_py.services.agent import BlockAssembler
from dsh_py.services.message import MessageSource, TextBlock, create_user_message
from dsh_py.services.session_title import (
    SessionTitleAutomaticMode,
    SessionTitleModelProvenance,
    SessionTitleProvider,
    SessionTitleProviderId,
    SessionTitleProviderRequest,
    SessionTitleProviderResult,
    SessionTitleUserMessage,
    normalize_session_title,
    session_title_provider_id,
)

# 辅助标题请求的超时原因码
SESSION_TITLE_TIMEOUT_CODE = "SESSION_TITLE_TIMEOUT"
# 超时上限（ms）：避免配置一个超过计时器上限的截止时间
MAX_TIMER_DELAY_MS = 600_000


@dataclass(frozen=True)
class SessionTitleLlmConfig:
    """一个模型 provider 的部署策略（无库默认值）。"""
    target_words: int
    target_cjk_characters: int
    max_input_bytes: int
    max_output_tokens: int
    timeout_ms: int
    provider: Optional[str] = None
    model: Optional[str] = None


ResolvedSessionTitleLlmConfig = SessionTitleLlmConfig

# 共享 Loader 字段 schema（无默认值）
SessionTitleLlmConfigFields = {
    "targetWords": z.integer(minimum=1),
    "targetCjkCharacters": z.integer(minimum=1),
    "maxInputBytes": z.integer(minimum=1),
    "maxOutputTokens": z.integer(minimum=1),
    "timeoutMs": z.integer(minimum=1, maximum=MAX_TIMER_DELAY_MS),
    "provider": z.string().optional(),
    "model": z.string().optional(),
}
SessionTitleLlmConfigSchema = z.object(SessionTitleLlmConfigFields, extra="strip")

#: 从一个固定修订中选中 provider 使用的消息子集。
SessionTitleLlmMessageSelector = Callable[[tuple[SessionTitleUserMessage, ...]], list[SessionTitleUserMessage]]


def resolve_session_title_llm_config(config: Any) -> ResolvedSessionTitleLlmConfig:
    """校验并分离所需的模型 provider 配置。"""
    if not isinstance(config, dict):
        raise ValueError("session-title-llm: 配置为必填项")
    for key in config:
        if key not in SessionTitleLlmConfigFields:
            raise ValueError(f"session-title-llm: 未知配置键 {key!r}")
    resolved = SessionTitleLlmConfigSchema.validate(config)
    has_provider = resolved.get("provider") is not None
    has_model = resolved.get("model") is not None
    if has_provider != has_model:
        raise ValueError("session-title-llm: provider 与 model 必须同时提供")
    if has_provider and (not resolved["provider"] or not resolved["model"]):
        raise ValueError("session-title-llm: provider 与 model 覆盖必须是非空字符串")
    return SessionTitleLlmConfig(
        target_words=int(resolved["targetWords"]),
        target_cjk_characters=int(resolved["targetCjkCharacters"]),
        max_input_bytes=int(resolved["maxInputBytes"]),
        max_output_tokens=int(resolved["maxOutputTokens"]),
        timeout_ms=int(resolved["timeoutMs"]),
        provider=resolved.get("provider"),
        model=resolved.get("model"),
    )


def _resolve_route(
    config: ResolvedSessionTitleLlmConfig, request: SessionTitleProviderRequest
) -> SessionTitleModelProvenance:
    if config.provider is not None and config.model is not None:
        return SessionTitleModelProvenance(provider=config.provider, model=config.model)
    if request.route is None:
        raise ValueError("session-title-llm: 无可用的请求路由；请同时配置 provider 与 model")
    return request.route


def _system_prompt(config: ResolvedSessionTitleLlmConfig) -> str:
    return "\n".join([
        "为一段 AI 编程助手会话，从提供的人类消息中生成一句简洁的标题。",
        "仅返回一行纯文本标题，**不要**引号、前缀、解释、Markdown、XML 或终端控制码。不允许出现代码。",
        "使用消息所用语言。",
        f"非 CJK 语言约 {config.target_words} 个词；中文 / 日文 / 韩文约 {config.target_cjk_characters} 个字。",
    ])


def _frame_messages(messages: list[SessionTitleUserMessage]) -> str:
    payload = [{"seq": m.seq, "text": m.text} for m in messages]
    return "从该人类消息 JSON 数组生成会话标题：\n" + json.dumps(payload, ensure_ascii=False)


def _make_deadline(
    upstream: Optional[CancelSignal], timeout_ms: int, code: str
) -> tuple[CancelSignal, CancelSignal]:
    """构造一个超时熔断信号，与上游信号融合；到期即取消。"""
    timeout_sig = CancelSignal()
    loop = asyncio.get_running_loop()
    handle = loop.call_later(timeout_ms / 1000.0, lambda: timeout_sig.abort(f"{code}"))
    fused = CancelSignal.any([upstream, timeout_sig] if upstream is not None else [timeout_sig])

    def cancel_timer() -> None:
        handle.cancel()

    fused.add_listener(cancel_timer)
    return fused, timeout_sig


async def generate_session_title_with_llm(
    ctx: Any,
    config: ResolvedSessionTitleLlmConfig,
    request: SessionTitleProviderRequest,
    selected_messages: list[SessionTitleUserMessage],
    title_provider: SessionTitleProviderId,
) -> SessionTitleProviderResult:
    """通过共享的辅助 LLM 调用生成一次标题。

    校验输入与路由、记录 ``session/title-llm-request`` 事件、流过模型、折叠文本、
    归一化并返回来源 seq 与所用模型路由。
    """
    if request.signal is not None:
        request.signal.throw_if_aborted()
    if not selected_messages:
        raise ValueError("session-title-llm: 至少需要一条来源消息")
    framed = _frame_messages(selected_messages)
    input_bytes = len(framed.encode("utf-8"))
    if input_bytes > config.max_input_bytes:
        raise ValueError(
            f"session-title-llm: 输入 {input_bytes} 字节，超过 maxInputBytes {config.max_input_bytes}"
        )
    route = _resolve_route(config, request)
    messages = [create_user_message(
        [TextBlock(framed)], MessageSource("plugin", plugin="dsh-session-title-llm")
    )]
    system = _system_prompt(config)
    deadline_sig, _ = _make_deadline(request.signal, config.timeout_ms, SESSION_TITLE_TIMEOUT_CODE)
    options = GenerateOptions(
        provider=route.provider,
        model=route.model,
        messages=messages,
        system=system,
        max_tokens=config.max_output_tokens,
        session_id=request.session.header.id,
        purpose="session-title",
        signal=deadline_sig,
    )
    request.session.append("session/title-llm-request", {
        "titleProvider": title_provider,
        "messageSeqs": [m.seq for m in selected_messages],
        "route": {"provider": route.provider, "model": route.model},
        "system": system,
        "messages": messages,
        "maxTokens": config.max_output_tokens,
    })
    deadline_sig.throw_if_aborted()

    assembler = BlockAssembler()
    async for chunk in ctx.llm.stream(options):
        deadline_sig.throw_if_aborted()
        assembler.push(chunk)
    deadline_sig.throw_if_aborted()

    finish = assembler.finish or {"kind": "stop"}
    kind = finish.get("kind", "stop")
    if kind == "stop":
        pass
    elif kind == "aborted":
        raise SignalCancelledError(finish.get("message"))
    elif kind == "max-tokens":
        raise ValueError("session-title-llm: 标题输出达到 maxOutputTokens 上限")
    elif kind == "tool-calls":
        raise ValueError("session-title-llm: 标题模型意外请求了工具")
    else:
        raise ValueError(f"session-title-llm: 不支持的结束原因 {kind!r}")

    blocks = assembler.blocks
    from dsh_py.services.message import ToolCallBlock

    if any(isinstance(b, ToolCallBlock) for b in blocks):
        raise ValueError("session-title-llm: 标题模型意外请求了工具")
    text = "".join(b.text for b in blocks if isinstance(b, TextBlock))
    title = normalize_session_title(text, sys.maxsize)
    if title == "":
        raise ValueError("session-title-llm: 标题模型未产出文本")
    return SessionTitleProviderResult(
        title=title,
        message_seqs=tuple(m.seq for m in selected_messages),
        model=route,
    )


def register_session_title_llm_provider(
    ctx: Any,
    config: Any,
    id: str,
    automatic: SessionTitleAutomaticMode,
    select_messages: SessionTitleLlmMessageSelector,
) -> None:
    """通过共享配置与调用策略，注册一个模型 provider。"""
    resolved = resolve_session_title_llm_config(config)
    title_provider = session_title_provider_id(id)

    def generate(request: SessionTitleProviderRequest) -> Any:
        return generate_session_title_with_llm(
            ctx, resolved, request, select_messages(request.messages), title_provider
        )

    ctx.sessionTitle.register(SessionTitleProvider(
        id=title_provider, automatic=automatic, generate=generate
    ))

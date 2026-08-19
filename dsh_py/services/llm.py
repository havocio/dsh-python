"""LLM 接口层（seam）：适配器注册表 + 流式调用 API。

对标 dsh 的 ``@deepseek-ai/dsh-llm``：
- :class:`LlmAdapter` —— 具体供应商后端，实现 ``stream(options)``。
- :class:`LlmService` —— 即 ``ctx.llm``；提供 ``register_adapter`` 与
  ``stream``（后者会经过 ``llm/stream`` 瀑布流，使中间件能够包裹原始适配器
  流，与 dsh 完全一致）。
- :class:`StreamChunk` / :class:`ChunkType` —— 令牌级的流式分块协议。
- :class:`GenerateOptions` —— 传给适配器的请求结构。
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterable, AsyncIterator, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.call_config import call_config_from_options, merge_call_config
from dsh_py.services.retry_policy import ResolvedRetryPolicy


class LlmError(Exception):
    """LLM 适配层统一异常，携带稳定错误码（对标 dsh 的 LlmError）。"""

    def __init__(self, message: str, code: str = "UNKNOWN", cause: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.cause = cause

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


# 合法 API key 字符集：可打印 ASCII、不含空格（对齐 dsh 的 LEGAL_API_KEY）
_LEGAL_API_KEY = re.compile(r"^[\x21-\x7E]+$")


def normalize_api_key(raw: str) -> tuple[str, str]:
    """校验一个提供的 API key（先 trim 空白）。

    返回 ``(verdict, value)``：``verdict`` 为 ``ok`` / ``empty`` / ``illegal``；
    仅 ``ok`` 时 ``value`` 是去空白后的 key（对齐 dsh 的 normalizeApiKey）。
    """
    value = raw.strip()
    if not value:
        return "empty", ""
    if not _LEGAL_API_KEY.fullmatch(value):
        return "illegal", ""
    return "ok", value


class ChunkType(str, Enum):
    """流式分块的类型枚举。"""
    BLOCK_START = "block-start"        # 一个内容块（文本/推理/工具调用）开始
    TEXT_DELTA = "text-delta"          # 文本增量
    REASONING_DELTA = "reasoning-delta"  # 推理过程增量
    TOOL_CALL_DELTA = "tool-call-delta"  # 工具调用增量
    BLOCK_END = "block-end"            # 内容块结束
    USAGE = "usage"                    # token 用量
    FINISH = "finish"                  # 本轮生成结束


@dataclass
class StreamChunk:
    """LLM 流上的一帧令牌级事件（以 ``type`` 区分的标签联合）。"""

    type: ChunkType
    index: Optional[int] = None         # BLOCK_START / *_DELTA / BLOCK_END：块序号
    block_type: Optional[str] = None    # BLOCK_START：块类型（text/reasoning/tool-call）
    text: Optional[str] = None          # TEXT_DELTA：文本增量
    reasoning: Optional[str] = None     # REASONING_DELTA：推理增量
    tool_call_id: Optional[str] = None  # TOOL_CALL_DELTA：工具调用 id
    tool_call_name: Optional[str] = None  # TOOL_CALL_DELTA：工具名
    arguments_delta: Optional[str] = None  # TOOL_CALL_DELTA：参数 JSON 增量
    block: Optional[Any] = None         # BLOCK_END：已组装完成的内容块
    usage: Optional[dict] = None        # USAGE：用量信息
    finish: Optional[dict] = None       # FINISH：结束原因（{"kind": ...}）


@dataclass
class GenerateOptions:
    """传给 :class:`LlmAdapter` 的请求结构。"""

    provider: str                      # 供应商标识
    model: str                         # 模型标识
    messages: list[Any]                # 消息列表（消息字典或 Message 对象）
    system: Optional[str] = None       # 系统提示词
    tools: Optional[list[dict]] = None  # 工具定义
    temperature: Optional[float] = None  # 采样温度
    max_tokens: Optional[int] = None   # 最大生成长度
    stop: Optional[list[str]] = None   # 停止词
    reasoning_effort: Optional[str] = None  # 推理强度（llm-deepseek 专用：off/high/max）
    signal: Optional[Any] = None       # 取消信号（对标 AbortSignal）
    session_id: Optional[str] = None   # 会话标识
    purpose: Optional[str] = None      # 调用用途


@dataclass
class LlmProviderInfo:
    """供应商元信息。"""
    id: str
    name: str


class LlmAdapter(ABC):
    """供应商接线后端。只需实现 ``stream()``，其余方法均可选重写。"""

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)

    async def list_models(self, provider: str) -> list[dict]:
        return []

    async def resolve_model(self, provider: str, model: str) -> dict:
        return {"provider": provider, "id": model, "name": model}

    @abstractmethod
    def stream(self, options: GenerateOptions) -> AsyncIterable[StreamChunk]:
        """为一次模型调用产出原始分块。必须尊重 ``options.signal``。"""
        raise NotImplementedError


class LlmService(Service):
    """``llm`` 服务：适配器注册表 + 可被瀑布流拦截的流式调用。"""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "llm")
        self._adapters: dict[str, LlmAdapter] = {}
        # 每个供应商路由的重试策略（register_adapter 时可选携带）
        self._provider_retry: dict[str, ResolvedRetryPolicy] = {}
        # 每个供应商路由的默认调用配置（call-config 最低层，可选）
        self._provider_defaults: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    # 注册
    # ------------------------------------------------------------------ #
    def register_adapter(
        self,
        providers: list[str],
        adapter: LlmAdapter,
        replace: bool = False,
        retry: Optional[ResolvedRetryPolicy] = None,
        defaults: Optional[dict] = None,
    ) -> None:
        """为一组供应商路由注册同一适配器（要么全部成功，要么整体失败）。

        - ``replace=True`` 允许覆盖已存在的映射（例如 CLI 的 ``--mock``）；
        - ``retry`` 是该路由的重试策略（缺省不重试）；
        - ``defaults`` 是该路由的默认调用配置（call-config 最低层，如默认 model）。
        """
        for provider in providers:
            if not provider:
                raise ValueError("适配器供应商名称不能为空")
            if provider in self._adapters and not replace:
                raise RuntimeError(f"供应商 {provider!r} 已有适配器注册")
        for provider in providers:
            self._adapters[provider] = adapter
            if retry is not None:
                self._provider_retry[provider] = retry
            if defaults is not None:
                self._provider_defaults[provider] = defaults

    def list_providers(self) -> list[LlmProviderInfo]:
        """列出当前所有已注册的供应商。"""
        return [self._adapters[p].provider_info(p) for p in self._adapters]

    def _adapter(self, provider: str) -> LlmAdapter:
        """按供应商名取出适配器，不存在则抛错。"""
        adapter = self._adapters.get(provider)
        if adapter is None:
            raise RuntimeError(f"尚未为供应商 {provider!r} 注册任何适配器")
        return adapter

    # ------------------------------------------------------------------ #
    # 流式调用
    # ------------------------------------------------------------------ #
    def _resolve_call_config(self, options: GenerateOptions) -> dict:
        """合并 provider 默认与本次请求，得到生效的调用配置。"""
        config = merge_call_config(
            self._provider_defaults.get(options.provider),
            None,
            call_config_from_options(options),
        )
        out = {"provider": config.provider, "model": config.model}
        if config.temperature is not None:
            out["temperature"] = config.temperature
        if config.max_tokens is not None:
            out["max_tokens"] = config.max_tokens
        if config.stop is not None:
            out["stop"] = list(config.stop)
        return out

    def _apply_config(self, options: GenerateOptions, config: dict) -> GenerateOptions:
        """用合并后的配置覆盖请求选项（返回新实例）。"""
        from dataclasses import replace
        return replace(
            options,
            provider=config.get("provider", options.provider),
            model=config.get("model", options.model),
            temperature=config.get("temperature", options.temperature),
            max_tokens=config.get("max_tokens", options.max_tokens),
            stop=config.get("stop", options.stop),
        )

    async def _adapter_stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        """直接转发已解析适配器的流（带重试策略）。"""
        adapter = self._adapter(options.provider)
        policy = self._provider_retry.get(options.provider)
        attempts = 0
        while True:
            try:
                async for chunk in adapter.stream(options):
                    yield chunk
                return
            except LlmError as exc:
                if policy is None or not policy.should_retry(exc.code, attempts):
                    raise
                attempts += 1
                if options.signal is not None and hasattr(options.signal, "throw_if_aborted"):
                    options.signal.throw_if_aborted()
                await asyncio.sleep(policy.delay_for(attempts))

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        """发起一次流式调用，并经过 ``llm/stream`` 瀑布流。

        最内层的 ``next`` 即已解析适配器的流；中间件监听器通过迭代 ``next()``
        来包裹它，与 dsh 的中间件机制一致。调用前先按 call-config 三层合并
        生效配置（provider 默认 → 本次请求），并按路由重试策略自动重试
        可重试错误码。
        """

        effective = self._apply_config(options, self._resolve_call_config(options))

        async def inner() -> AsyncIterator[StreamChunk]:
            async for chunk in self._adapter_stream(effective):
                yield chunk

        async for chunk in self.ctx.waterfall("llm/stream", effective, inner=inner):
            yield chunk


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``llm`` 服务（适配器注册表 + 流式调用 seam）。

    本服务本身也是「一切皆插件」的一等公民：想替换 LLM seam，提供另一个注册
    了同名 ``llm`` 服务的插件即可，调用方（agent 循环、CLI）不感知实现差异。
    """
    LlmService(ctx)


apply.provides = ["llm"]  # 声明：本插件提供 llm 服务（供 loader 拓扑排序）

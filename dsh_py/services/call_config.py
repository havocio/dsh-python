"""调用配置（call config，对标 dsh 的 ``dsh-llm/call-config``）。

:class:`LlmCallConfig` 是一次对话「epoch 级」的请求配置（provider / model /
采样参数），与 dsh 的字段一一对应。它比单次 ``GenerateOptions`` 更稳定：
模型路由与采样是会话级的请求头状态（影响缓存复用），不应允许每次调用静默漂移。

**三层合并**（:func:`merge_call_config`）：

    provider 默认（适配器解析） < session header 持久化 < 本次请求

优先级从低到高，缺失字段逐层填充。``call_config_equals`` 做逐字段比较
（含 ``stop`` 列表逐元素），供判断配置是否真的变化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class LlmCallConfig:
    """一次对话的请求头配置（字段与 GenerateOptions 同名项一一对应）。"""
    provider: str = ""
    model: str = ""
    reasoning_effort: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stop: Optional[tuple[str, ...]] = None


def call_config_equals(a: LlmCallConfig, b: LlmCallConfig) -> bool:
    """逐字段比较两个配置是否相等（stop 列表逐元素比较）。"""
    if (a.provider, a.model, a.reasoning_effort, a.temperature, a.max_tokens) != \
       (b.provider, b.model, b.reasoning_effort, b.temperature, b.max_tokens):
        return False
    if a.stop is None or b.stop is None:
        return a.stop == b.stop
    return a.stop == b.stop


def _pick(base: dict, header: Optional[dict], request: Optional[dict]) -> LlmCallConfig:
    """按 provider 默认 < header < request 的优先级合并三层字典。"""
    merged: dict[str, Any] = {}
    for layer in (base, header or {}, request or {}):
        for key, value in layer.items():
            if value is not None:
                merged[key] = value
    stop = merged.get("stop")
    return LlmCallConfig(
        provider=merged.get("provider", ""),
        model=merged.get("model", ""),
        reasoning_effort=merged.get("reasoning_effort"),
        temperature=merged.get("temperature"),
        max_tokens=merged.get("max_tokens"),
        stop=tuple(stop) if isinstance(stop, (list, tuple)) else stop,
    )


def merge_call_config(
    provider_defaults: Optional[dict] = None,
    header: Optional[dict] = None,
    request: Optional[dict] = None,
) -> LlmCallConfig:
    """合并三层调用配置（缺失字段逐层填充，高层优先）。

    :param provider_defaults: 适配器 / provider 解析出的默认（最低优先）。
    :param header: 会话日志持久化的 epoch 级配置（次低优先）。
    :param request: 本次请求的选项（最高优先）。
    """
    return _pick(provider_defaults or {}, header, request)


def call_config_to_dict(config: LlmCallConfig) -> dict:
    """LlmCallConfig → dict（供 session header 持久化）。"""
    out: dict[str, Any] = {"provider": config.provider, "model": config.model}
    if config.reasoning_effort is not None:
        out["reasoning_effort"] = config.reasoning_effort
    if config.temperature is not None:
        out["temperature"] = config.temperature
    if config.max_tokens is not None:
        out["max_tokens"] = config.max_tokens
    if config.stop is not None:
        out["stop"] = list(config.stop)
    return out


def call_config_from_options(options: Any) -> dict:
    """从 GenerateOptions 提取可持久化的 call config 字段。"""
    out: dict[str, Any] = {"provider": options.provider, "model": options.model}
    if getattr(options, "temperature", None) is not None:
        out["temperature"] = options.temperature
    if getattr(options, "max_tokens", None) is not None:
        out["max_tokens"] = options.max_tokens
    if getattr(options, "stop", None) is not None:
        out["stop"] = list(options.stop)
    return out

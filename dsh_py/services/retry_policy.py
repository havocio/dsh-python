"""模型请求重试策略（对标 dsh 的 ``dsh-llm/retry-policy``）。

- :func:`resolve_retry_policy` —— 把供应商配置解析成不可变策略
  （``normal``：仅重试配置的可重试错误码，上限 ``maxRetries`` 次；
  ``always``：对每次模型请求失败无限重试，直到成功 / 取消 / 卸载）；
- :class:`RetryPolicy` —— 提供 ``should_retry``（按错误码与已重试次数判定）
  与 ``delay_for``（有界指数退避 + 对称抖动）。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# 默认可重试错误码（对齐 dsh 的 DEFAULT_RETRYABLE_CODES）
DEFAULT_RETRYABLE_CODES = (
    "EMPTY_RESPONSE", "RATE_LIMIT", "SERVER", "TIMEOUT", "TRANSPORT",
)

DEFAULT_MAX_RETRIES = 2
DEFAULT_INITIAL_DELAY_MS = 500
DEFAULT_MAX_DELAY_MS = 10_000
DEFAULT_JITTER_RATIO = 0.1


class RetryPolicyError(ValueError):
    """重试策略配置非法。"""


@dataclass(frozen=True)
class ResolvedRetryPolicy:
    """不可变的重试策略（注册 provider 路由时捕获）。"""
    mode: str                       # 'normal' | 'always'
    max_retries: int = DEFAULT_MAX_RETRIES
    retryable_codes: tuple[str, ...] = DEFAULT_RETRYABLE_CODES
    initial_delay_ms: int = DEFAULT_INITIAL_DELAY_MS
    max_delay_ms: int = DEFAULT_MAX_DELAY_MS
    jitter_ratio: float = DEFAULT_JITTER_RATIO

    # -- 判定 ---------------------------------------------------------------- #
    def should_retry(self, error_code: str, attempts: int) -> bool:
        """本次失败是否应重试（attempts = 已失败并重试的次数）。"""
        if self.mode == "always":
            return True
        return attempts < self.max_retries and error_code in self.retryable_codes

    def delay_for(self, attempt: int) -> float:
        """第 ``attempt`` 次重试前的等待秒数（有界指数退避 + 对称抖动）。"""
        # 指数退避：initial * 2^(attempt-1)，封顶 max
        base = min(self.initial_delay_ms * (2 ** (attempt - 1)), self.max_delay_ms)
        # 对称抖动：乘以 [1-jitter, 1+jitter]
        jitter = 1.0 + random.uniform(-self.jitter_ratio, self.jitter_ratio)
        return (base * jitter) / 1000.0


def _validate_backoff(initial: int, maximum: int, jitter: float) -> None:
    if not isinstance(initial, int) or initial <= 0:
        raise RetryPolicyError("initialDelayMs 必须为正整数")
    if not isinstance(maximum, int) or maximum <= 0:
        raise RetryPolicyError("maxDelayMs 必须为正整数")
    if initial > maximum:
        raise RetryPolicyError("initialDelayMs 不得大于 maxDelayMs")
    if not isinstance(jitter, (int, float)) or not (0 <= jitter <= 1):
        raise RetryPolicyError("jitterRatio 必须在 [0, 1] 区间")


def resolve_retry_policy(config: Optional[dict] = None, path: str = "retry") -> ResolvedRetryPolicy:
    """把供应商配置解析成不可变重试策略（缺省为 normal 默认值）。

    配置形态（对齐 dsh）：``{"mode": "normal"|"always", "maxRetries": n,
    "retryableCodes": [...], "backoff": {"initialDelayMs","maxDelayMs","jitterRatio"}}``。
    非法字段 / 值即抛错（fail loud）。
    """
    if config is None:
        return ResolvedRetryPolicy(mode="normal")

    allowed = {"mode", "maxRetries", "retryableCodes", "backoff"}
    unknown = set(config) - allowed
    if unknown:
        raise RetryPolicyError(f"{path}: 未知字段 {sorted(unknown)}")

    mode = config.get("mode", "normal")
    if mode not in ("normal", "always"):
        raise RetryPolicyError(f"{path}.mode 必须是 normal 或 always")

    backoff = config.get("backoff") or {}
    initial = backoff.get("initialDelayMs", DEFAULT_INITIAL_DELAY_MS)
    maximum = backoff.get("maxDelayMs", DEFAULT_MAX_DELAY_MS)
    jitter = backoff.get("jitterRatio", DEFAULT_JITTER_RATIO)
    _validate_backoff(initial, maximum, jitter)

    retryable = config.get("retryableCodes")
    if retryable is not None:
        if not isinstance(retryable, (list, tuple)) or not retryable:
            raise RetryPolicyError(f"{path}.retryableCodes 必须是非空数组")
        if len(set(retryable)) != len(retryable):
            raise RetryPolicyError(f"{path}.retryableCodes 不能含重复项")
        retryable = tuple(retryable)
    else:
        retryable = DEFAULT_RETRYABLE_CODES

    max_retries = config.get("maxRetries", DEFAULT_MAX_RETRIES)
    if not isinstance(max_retries, int) or max_retries < 0:
        raise RetryPolicyError(f"{path}.maxRetries 必须是非负整数")

    return ResolvedRetryPolicy(
        mode=mode, max_retries=max_retries, retryable_codes=retryable,
        initial_delay_ms=initial, max_delay_ms=maximum, jitter_ratio=jitter,
    )

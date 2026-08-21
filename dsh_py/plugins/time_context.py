"""请求时钟上下文（context/time-context，第 3 层）。

选择加入的请求时钟上下文：合格步骤向请求历史追加耐久的、来源归因的时间读数
（当前时间 + 浏览器时区策略 + 距上一步的耗时），让模型感知「现在」与节奏。

- 每个合格 pre-step 注入一条 plugin 来源（``form='snapshot'``）user 消息；
- ``refreshIntervalMs`` 节流：同一会话内距上次注入不足间隔则跳过；
- 浏览器时区（user-rpc 的 ``clientTimeZone``）：dsh_py 的 MessageSource 无该
  字段，恒为「unavailable」策略（差异已注明）；``timeZone`` 配置或进程时区
  作为回退显示区。

**与 dsh 的差异（已注明）**：dsh 用 ``Intl.DateTimeFormat`` 的 longOffset
（含 GMT+00:00 归一）；dsh_py 用 ``datetime + zoneinfo`` 的 ``%z`` 偏移
（等价 ISO 形状 ``YYYY-MM-DDTHH:MM:SS+HH:MM[Zone]``）。
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.services.message import MessageSource, TextBlock, create_user_message

logger = logging.getLogger("dsh_py.time_context")

PLUGIN_NAME = "time-context"

Config = z.object({
    "timeZone": z.string().optional(),
    "refreshIntervalMs": z.integer().optional(),
})


def _system_time_zone() -> str:
    """解析进程时区（zoneinfo key）；解析失败回退 'UTC'。"""
    local = datetime.now().astimezone()
    key = getattr(local.tzinfo, "key", None)
    return key if isinstance(key, str) and key else "UTC"


def format_timestamp(now_ms: float, time_zone: str) -> str:
    """把 epoch 毫秒格式化为 ISO 形状时间戳（含偏移 + IANA zone）。"""
    tz = ZoneInfo(time_zone)
    dt = datetime.fromtimestamp(now_ms / 1000.0, tz=tz)
    offset = dt.strftime("%z")
    if len(offset) == 5:  # +0800 → +08:00
        offset = f"{offset[:3]}:{offset[3:]}"
    else:
        offset = "+00:00"
    return f"{dt:%Y-%m-%dT%H:%M:%S}{offset}[{time_zone}]"


def format_duration(elapsed_ms: float) -> str:
    """把非负毫秒计数格式化为紧凑整秒单位。"""
    seconds = int(max(0, elapsed_ms) // 1000)
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts: list[str] = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def render_browser_time_zone_context() -> str:
    """dsh_py 的 MessageSource 无浏览器时区字段 → 恒 unavailable 策略。"""
    return (
        "Browser time zone for this request: unavailable. "
        "Ask the user to clarify otherwise-unqualified dates and times."
    )


def _preceding_message_time(agent: Any) -> Optional[float]:
    """最近的模型可见事件时间（排除本插件待追加项）。"""
    for event in reversed(agent.session.events):
        if event.type in ("user/message", "assistant/message", "tool/result"):
            return event.time
    return None


def _preceding_step_context_time(agent: Any, turn: int) -> Optional[float]:
    """本打开回合内上一条 time-context 事件时间。"""
    for event in reversed(agent.session.events):
        if event.type == "turn/start" and event.data.get("turn") == turn:
            return None
        source = getattr(event.data, "source", None)
        if (event.type == "user/message" and source is not None
                and source.kind == "plugin" and source.plugin == PLUGIN_NAME):
            return event.time
    return None


def _latest_injection_time(agent: Any) -> Optional[float]:
    """本插件最近一次耐久注入（含被遮蔽的表面事件）。"""
    for event in reversed(agent.session.events):
        source = getattr(event.data, "source", None)
        if (event.type == "user/message" and source is not None
                and source.kind == "plugin" and source.plugin == PLUGIN_NAME):
            return event.time
    return None


def _render_text(now: float, turn: int, step: int, previous: Optional[float],
                 time_zone: str) -> str:
    elapsed = "unavailable" if previous is None else format_duration(now - previous)
    baseline = "model-visible message" if step == 1 else "step context"
    return (
        f"Time sampled while preparing turn {turn}, step {step}: {format_timestamp(now, time_zone)}\n"
        f"{render_browser_time_zone_context()}\n"
        f"Elapsed since the preceding {baseline}: {elapsed}."
    )


def _validate_refresh_interval(value: Optional[int]) -> None:
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
        raise TypeError(f"time-context: refreshIntervalMs 必须是非负安全整数，得到 {value!r}")


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：挂 pre-step 时钟上下文注入器。"""
    cfg = config or {}
    time_zone = cfg.get("timeZone")
    refresh_interval_ms = cfg.get("refreshIntervalMs")
    _validate_refresh_interval(refresh_interval_ms)

    selected = time_zone or _system_time_zone()
    try:
        ZoneInfo(selected)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"time-context: 无效 IANA 时区 {selected!r}" if time_zone
            else "time-context: 无法解析系统时区",
        ) from exc

    @ctx.on("agent/pre-step")
    async def on_pre_step(event: dict, next):
        decision = await next()
        if decision.get("kind") == "reject" or getattr(event.get("signal"), "aborted", False):
            return decision
        agent = event["agent"]
        turn = event["turn"]
        step = event["step"]
        now = _time.time() * 1000
        if refresh_interval_ms is not None and refresh_interval_ms > 0:
            last = _latest_injection_time(agent)
            # event.time 是秒、now 是毫秒：统一到毫秒比较
            if last is not None and (now - last * 1000.0) < refresh_interval_ms:
                return decision

        previous = (_preceding_message_time(agent) if step == 1
                    else _preceding_step_context_time(agent, turn))
        text = _render_text(now, turn, step, previous, selected)
        message = create_user_message(
            [TextBlock(text)],
            source=MessageSource("plugin", plugin=PLUGIN_NAME, form="snapshot"),
        )
        return {"kind": "enter", "messages": [*decision.get("messages", []), message]}


apply.Config = Config
apply.name = "time-context"
apply.inject = ["agents"]

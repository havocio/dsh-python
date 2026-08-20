"""会话统计（session stats）：整日志的轮/步计数与墙钟时长（对标 dsh 的 ``dsh-session-stats``）。

本插件注册 ``sessionStats`` 投影单位，经 :mod:`dsh_py.services.projection` 的能力
seam 提供（注册表快照、变更流与每个投影载体），使客户端渲染出翻页与压缩都
无法改变的整会话数据。插件只拥有折叠；投递是 seam 的。

**计步基准**：以 ``step/end``（而非 ``assistant/message``）为被计步事件——它是
步生命周期的权威：循环在 ``finally`` 中每条进入的步恰好追加一条，完成、失败、
取消、max-tokens 的步都落地一条。统计已组装 assistant 消息则会多算 max-tokens
用量宿主消息、漏算取消步。

**墙钟折叠**：模型时长为 ``step/start`` → ``assistant/message``；首令牌是第一个
非空增量分块且经受得住步内重试；解码段为首令牌 → 已组装消息（仅统计同样上报
输出 token 的步）；工具时长按 callId 配对 ``tool/call`` → ``tool/result``。
"""

from __future__ import annotations

from typing import Any, Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.services.llm import ChunkType, StreamChunk
from dsh_py.services.message import ToolResultBlock
from dsh_py.services.projection import ProjectionDefinition


def is_token_delta(chunk: StreamChunk) -> bool:
    """是否一段承载令牌的增量（首个非空文本/推理增量）。"""
    if chunk.type == ChunkType.TEXT_DELTA:
        return bool(chunk.text)
    if chunk.type == ChunkType.REASONING_DELTA:
        return bool(chunk.reasoning)
    return False


def _usage_output_tokens(usage: Any) -> Optional[int]:
    """提供方上报的完成 token 数；未上报或非法返回 None。"""
    if not isinstance(usage, dict):
        return None
    value = usage.get("outputTokens")
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return None


def _result_call_id(message: Any) -> Optional[str]:
    """从 tool/result 消息的内容块提取工具调用 id（对齐 dsh 的 source.callId）。"""
    for block in getattr(message, "content", ()) or ():
        if isinstance(block, ToolResultBlock):
            return block.tool_call_id
    return None


#: ``sessionStats`` 投影单位（视图恰为这些总计）。
session_stats_schema = z.object({
    "turns": z.integer(),
    "steps": z.integer(),
    "llmMs": z.number(),
    "toolMs": z.number(),
    "ttftMs": z.number(),
    "ttftSteps": z.integer(),
    "decodeMs": z.number(),
    "decodeTokens": z.number(),
})


def _init() -> dict:
    return {
        "turns": 0,
        "steps": 0,
        "llmMs": 0,
        "toolMs": 0,
        "ttftMs": 0,
        "ttftSteps": 0,
        "decodeMs": 0,
        "decodeTokens": 0,
        "lastTurn": None,
        "openStep": None,
        "pendingCalls": {},
    }


def _apply(state: dict, event: Any) -> dict:
    """纯转移：每个不关心的事件返回同一引用（``is`` 门控变更流）。"""
    t = event.type
    if t == "step/start":
        return {**state, "openStep": {
            "turn": event.data["turn"], "step": event.data["step"],
            "startTime": event.time, "firstTokenTime": None,
        }}
    if t == "assistant/chunk":
        open_step = state["openStep"]
        if open_step is None or open_step["turn"] != event.data["turn"] or open_step["step"] != event.data["step"]:
            return state
        if open_step["firstTokenTime"] is not None or not is_token_delta(event.data["chunk"]):
            return state
        return {**state, "openStep": {**open_step, "firstTokenTime": event.time}}
    if t == "assistant/message":
        open_step = state["openStep"]
        if open_step is None or open_step["turn"] != event.data["turn"] or open_step["step"] != event.data["step"]:
            return state
        # 每条 step 恰好一条已组装消息：关闭边界使防御性重复不会二次累加。
        nxt: dict = {**state, "llmMs": state["llmMs"] + max(0, event.time - open_step["startTime"]), "openStep": None}
        if open_step["firstTokenTime"] is not None:
            nxt["ttftMs"] += max(0, open_step["firstTokenTime"] - open_step["startTime"])
            nxt["ttftSteps"] += 1
            output_tokens = _usage_output_tokens(event.data.get("usage"))
            if output_tokens is not None:
                nxt["decodeMs"] += max(0, event.time - open_step["firstTokenTime"])
                nxt["decodeTokens"] += output_tokens
        return nxt
    if t == "tool/call":
        return {**state, "pendingCalls": {**state["pendingCalls"], event.data["callId"]: event.time}}
    if t == "tool/result":
        call_id = _result_call_id(event.data.get("message"))
        dispatched = state["pendingCalls"].get(call_id) if call_id is not None else None
        if dispatched is None:
            return state
        remaining = {k: v for k, v in state["pendingCalls"].items() if k != call_id}
        return {**state, "toolMs": state["toolMs"] + max(0, event.time - dispatched), "pendingCalls": remaining}
    if t == "step/end":
        return {
            **state,
            "turns": state["turns"] if state["lastTurn"] == event.data["turn"] else state["turns"] + 1,
            "steps": state["steps"] + 1,
            "lastTurn": event.data["turn"],
            "openStep": None,
        }
    if t == "turn/end":
        # 结果永远落在其轮内；取消/失败轮中未落地的调用在此清空，而非永久增长。
        return state if not state["pendingCalls"] else {**state, "pendingCalls": {}}
    return state


def _view(state: dict) -> dict:
    return {k: state[k] for k in (
        "turns", "steps", "llmMs", "toolMs", "ttftMs", "ttftSteps", "decodeMs", "decodeTokens",
    )}


#: 注册到 ``ctx.sessionProjections`` 的 ``sessionStats`` 单位。
session_stats_definition = ProjectionDefinition(
    key="sessionStats",
    schema=session_stats_schema,
    init=_init,
    apply=_apply,
    view=_view,
    state_version=1,
)


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``sessionStats`` 投影单位（注册随调用 fiber 卸载）。"""
    ctx.sessionProjections.register(session_stats_definition)


apply.provides = ["sessionStats"]      # 声明：本插件提供 sessionStats 投影
apply.inject = ["sessionProjections"]  # 依赖：投影注册表必须先就绪（拓扑自动排序）

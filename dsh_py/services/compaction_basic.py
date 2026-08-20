"""重放感知的基础压缩后端（对标 dsh 的 ``dsh-compaction-basic``）。

后端拥有触发策略（压力 / 上下文溢出）、保留预算与 LLM 摘要。核心不变式：

- **区域选择**（:func:`select_compactable_range`）：从尾部累计保留预算，切到第一个
  工具配对平衡边界，绝不切开 assistant 工具调用/结果对。
- **事务**（:func:`compact_surface_region`）：``compaction/start`` 是持久锁 →
  摘要 → 稳定性校验（whole-surface / selected-span）→ ``compaction/summary`` +
  表面替换 user 消息（``surface_op=replace``）→ ``compaction/end``。失败路径恰好
  一次 ``compaction/end``（带 error），未闭合的 start 保持可检测。
- **摘要收敛**：摘要帧估算必须**小于**被遮蔽内容，否则事务失败。
- **前缀缓存复用**：摘要调用重放会话自己的 system/tools/消息前缀，仅追加压缩
  指令，使辅助调用成为最后路由请求的真前缀（提供方 KV 缓存不被击穿）。
"""

from __future__ import annotations

import asyncio
import math
import uuid
from typing import Any, Awaitable, Callable, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.signal import CancelSignal
from dsh_py.services.agent import BlockAssembler
from dsh_py.services.compaction import (
    CompactionEngine,
    CompactionId,
    CompactionResult,
    ManualCompactionError,
    compact_checkpoint_source,
    tool_pairing_balanced_after,
    tool_pairing_balanced_before,
)
from dsh_py.services.llm import ChunkType, GenerateOptions, StreamChunk
from dsh_py.services.message import MessageSource, TextBlock, create_user_message
from dsh_py.services.session import Session, SessionEvent

# 上下文窗口溢出错误码（适配器失败分类；对齐 dsh 的 CONTEXT_WINDOW_EXCEEDED_CODE）
CONTEXT_WINDOW_EXCEEDED_CODE = "CONTEXT_WINDOW_EXCEEDED"

# --------------------------------------------------------------------------- #
# 配置解析
# --------------------------------------------------------------------------- #
DEFAULT_THRESHOLD_RATIO = 0.8
DEFAULT_RETAIN_RATIO = 0.16
DEFAULT_MAX_TOKENS = 8192
DEFAULT_COMPACTION_RETRIES = 1
DEFAULT_MAX_OVERFLOW_RETRIES = 1


class TargetPressureConfigError(RuntimeError):
    """目标模型缺少可解析的压缩压力配置。"""

    def __init__(self, target_key: str, message: str) -> None:
        super().__init__(message)
        self.target_key = target_key


def resolve_config(config: Optional[dict] = None) -> dict:
    """解析并校验压缩配置（默认：阈值 0.8 / 保留 0.16 / 摘要上限 8192）。"""
    config = config or {}
    threshold_ratio = config.get("thresholdRatio", DEFAULT_THRESHOLD_RATIO)
    retain_tokens = config.get("retainTokens")
    retain_ratio = config.get("retainRatio")
    if retain_tokens is not None and retain_ratio is not None:
        raise ValueError("BasicCompactionConfig: retainRatio 与 retainTokens 互斥")
    if retain_tokens is None:
        retain_ratio = retain_ratio if retain_ratio is not None else DEFAULT_RETAIN_RATIO
    return {
        "thresholdRatio": threshold_ratio,
        **({"retainTokens": retain_tokens} if retain_tokens is not None else {"retainRatio": retain_ratio}),
        "summarizationProvider": config.get("summarizationProvider", ""),
        "summarizationModel": config.get("summarizationModel", ""),
        "maxTokens": config.get("maxTokens", DEFAULT_MAX_TOKENS),
        "compactionRetries": config.get("compactionRetries", DEFAULT_COMPACTION_RETRIES),
        "maxOverflowRetries": config.get("maxOverflowRetries", DEFAULT_MAX_OVERFLOW_RETRIES),
        "modelPolicies": config.get("modelPolicies", []),
        "auto": config.get("auto", True),
    }


def resolve_target_policy(config: dict, target: dict) -> dict:
    """把精确 provider/model 覆盖合并到默认策略之上。"""
    override = next(
        (p for p in config["modelPolicies"]
         if p.get("provider") == target["provider"] and p.get("model") == target["model"]),
        None,
    )
    o = override or {}
    inherited: dict = (
        {"retainTokens": config["retainTokens"]} if "retainTokens" in config
        else {"retainRatio": config["retainRatio"]}
    )
    if "retainRatio" in o:
        retention: dict = {"retainRatio": o["retainRatio"]}
    elif "retainTokens" in o:
        retention = {"retainTokens": o["retainTokens"]}
    else:
        retention = inherited
    policy = {
        "target": {"provider": target["provider"], "model": target["model"]},
        "thresholdRatio": o.get("thresholdRatio", config["thresholdRatio"]),
        **retention,
        "summarizationProvider": o.get("summarizationProvider", config["summarizationProvider"]),
        "summarizationModel": o.get("summarizationModel", config["summarizationModel"]),
        "maxTokens": o.get("maxTokens", config["maxTokens"]),
        "compactionRetries": o.get("compactionRetries", config["compactionRetries"]),
        "maxOverflowRetries": o.get("maxOverflowRetries", config["maxOverflowRetries"]),
    }
    return policy


def resolve_compact_spec(policy: dict, context_window: int) -> dict:
    """把一个策略换算成具体模型的压力阈值与保留预算。"""
    target_key = f"{policy['target']['provider']}/{policy['target']['model']}"
    if not isinstance(context_window, int) or context_window <= 0:
        raise TargetPressureConfigError(target_key, f"无效上下文窗口 {context_window!r}")
    threshold_tokens = math.floor(context_window * policy["thresholdRatio"])
    retain_tokens = (
        policy["retainTokens"] if "retainTokens" in policy
        else math.floor(context_window * policy["retainRatio"])
    )
    if retain_tokens >= threshold_tokens:
        raise TargetPressureConfigError(
            target_key,
            f"BasicCompactionConfig: {target_key} retainTokens ({retain_tokens}) 必须小于阈值 {threshold_tokens}",
        )
    return {**policy, "contextWindow": context_window,
            "thresholdTokens": threshold_tokens, "retainTokens": retain_tokens}


# --------------------------------------------------------------------------- #
# 区域选择
# --------------------------------------------------------------------------- #
def select_compactable_range(session: Session, measurement: dict, retain_tokens: int) -> Optional[dict]:
    """解析下一个头部锚定的压缩区间，同时保留定价的近期尾部且不切开工具对。

    返回 ``{"start": seq, "end": seq}``（含首尾的表面节点 seq），无安全区间时 None。
    """
    priced_nodes = measurement["nodes"]
    if not priced_nodes:
        return None
    surface_nodes = session.surface["nodes"]
    if len(surface_nodes) != len(priced_nodes) or any(
        s != n["seq"] for s, n in zip(surface_nodes, priced_nodes)
    ):
        raise RuntimeError("compaction: token-meter surface 与会话当前表面不匹配")
    accumulated = 0
    keep_from_idx = len(priced_nodes)
    for index in range(len(priced_nodes) - 1, -1, -1):
        accumulated += priced_nodes[index]["tokens"]
        keep_from_idx = index
        if accumulated >= retain_tokens:
            break
    if keep_from_idx == 0:
        return None
    while keep_from_idx > 0:
        if tool_pairing_balanced_before(session, surface_nodes[keep_from_idx]):
            break
        keep_from_idx -= 1
    if keep_from_idx == 0:
        return None
    return {"start": surface_nodes[0], "end": surface_nodes[keep_from_idx - 1]}


# --------------------------------------------------------------------------- #
# 压缩事务
# --------------------------------------------------------------------------- #
def _signal_throw(signal: Any) -> None:
    if signal is not None and hasattr(signal, "throw_if_aborted"):
        signal.throw_if_aborted()


def _error_chain(error: Any) -> str:
    """错误链摘要（对齐 dsh 的 errorChain）。"""
    parts: list[str] = []
    seen: set[int] = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current))
        current = getattr(current, "__cause__", None)
    return " <- ".join(parts) if parts else str(error)


def _inspect_compaction_entry_state(events: list[SessionEvent]) -> dict:
    """独立检查打开中的轮 / 未闭合的 compaction/start / 最新 seed 边界。"""
    open_turn: Optional[int] = None
    open_turn_known = False
    unmatched_start: Optional[SessionEvent] = None
    compaction_known = False
    latest_end_seed: Optional[int] = None
    for event in reversed(events):
        if latest_end_seed is None and event.type == "session/end-seed":
            latest_end_seed = event.seq
        if not compaction_known:
            if event.type == "compaction/start":
                unmatched_start = event
                compaction_known = True
            elif event.type == "compaction/end":
                compaction_known = True
        if not open_turn_known:
            if event.type == "turn/start":
                open_turn = event.data["turn"]
                open_turn_known = True
            elif event.type == "turn/end":
                open_turn_known = True
        if open_turn_known and compaction_known and latest_end_seed is not None:
            break
    return {"open_turn": open_turn, "unmatched_start": unmatched_start, "latest_end_seed": latest_end_seed}


def _assert_compaction_inactive(entry_state: dict, stage: str) -> None:
    """拒绝持久化的未闭合压缩标记（除非 seed 边界证明它属于更早生命周期）。"""
    unmatched = entry_state["unmatched_start"]
    latest_end_seed = entry_state["latest_end_seed"]
    if unmatched is None or (latest_end_seed is not None and latest_end_seed > unmatched.seq):
        return
    raise ManualCompactionError("busy", f"{stage}: 压缩已在进程中；会话压缩锁已激活")


def assert_no_active_compaction(session: Session, stage: str) -> None:
    """异步策略决策后重查持久压缩锁。"""
    _assert_compaction_inactive(_inspect_compaction_entry_state(session.events), stage)


def _validate_surface_region(session: Session, start: int, end: int) -> dict:
    """校验一个请求的表面位置区间（含首尾，必须工具配对平衡）。"""
    nodes = session.surface["nodes"]
    start_idx = nodes.index(start) if start in nodes else -1
    end_idx = nodes.index(end) if end in nodes else -1
    if start_idx == -1:
        raise RuntimeError(f"compactRegion: surface 中未找到 start seq {start}")
    if end_idx == -1:
        raise RuntimeError(f"compactRegion: surface 中未找到 end seq {end}")
    if start_idx > end_idx:
        raise RuntimeError(
            f"compactRegion: start seq {start}（位置 {start_idx}）在 end seq {end}（位置 {end_idx}）之后"
        )
    if not tool_pairing_balanced_before(session, nodes[start_idx]):
        raise RuntimeError(f"compactRegion: start seq {start} 不是平衡边界（会切开工具调用/结果对）")
    if not tool_pairing_balanced_after(session, nodes[end_idx]):
        raise RuntimeError(f"compactRegion: end seq {end} 不是平衡边界（会切开一步，或该步仍在打开）")
    return {"start": start, "end": end, "start_idx": start_idx, "end_idx": end_idx,
            "shadowed_seqs": nodes[start_idx:end_idx + 1]}


def _prepare_compaction(dependencies: dict, session: Session, selection: dict) -> dict:
    """对已校验区间做定价快照与重放输入。"""
    measurement = dependencies["meter"].measure(session)
    selected = measurement["nodes"][selection["start_idx"]:selection["end_idx"] + 1]
    if len(selected) != len(selection["shadowed_seqs"]) or any(
        n["seq"] != s for n, s in zip(selected, selection["shadowed_seqs"])
    ):
        raise RuntimeError("compaction: 选中的表面在摘要开始前已变化")
    shadowed_tokens = sum(n["tokens"] for n in selected)
    return {
        **selection,
        "measurement": measurement,
        "selected_nodes": selected,
        "shadowed_token_count": shadowed_tokens,
        "input": _build_summarization_input(session, selection["shadowed_seqs"]),
    }


def _build_summarization_input(session: Session, shadowed_seqs: list[int]) -> dict:
    """重放被遮蔽区域：最近请求的 system/tools + 区域内按表面顺序的消息。"""
    header = session.request_header or {}
    messages = []
    for seq in shadowed_seqs:
        # 表面 seq 从 1 起（日志索引 = seq-1）
        if 1 <= seq <= len(session.events):
            message = session.derive_event_message(session.events[seq - 1])
            if message is not None:
                messages.append(message)
    result: dict = {"messages": messages}
    if header.get("system"):
        result["system"] = header["system"]
    if header.get("tools"):
        result["tools"] = header["tools"]
    return result


def _assert_whole_surface_unchanged(dependencies: dict, session: Session, prepared: dict) -> None:
    current = dependencies["meter"].measure(session)
    if current["nodes"] != prepared["measurement"]["nodes"]:
        raise RuntimeError("compaction: 会话表面在摘要期间已变化")


def _assert_selected_span_stable(dependencies: dict, session: Session, prepared: dict) -> None:
    try:
        current = _validate_surface_region(session, prepared["start"], prepared["end"])
    except Exception as error:
        raise RuntimeError("compaction: 选中的区间不再有效替换目标", ) from error
    if list(current["shadowed_seqs"]) != list(prepared["shadowed_seqs"]):
        raise RuntimeError("compaction: 选中的区间在摘要期间已变化")
    measured = dependencies["meter"].measure(session)["nodes"][current["start_idx"]:current["end_idx"] + 1]
    if measured != prepared["selected_nodes"]:
        raise RuntimeError("compaction: 选中的区间在摘要期间被重写")


def _commit_compaction_body(session: Session, start_event: SessionEvent, summarized: dict) -> dict:
    """同步追加完成的摘要记录与替换主体（不 yield）。"""
    summary_event = session.append("compaction/summary", {
        "compactionId": start_event.data["compactionId"],
        **({"sourceCommandId": start_event.data["sourceCommandId"]} if "sourceCommandId" in start_event.data else {}),
        "summary": summarized["summary"],
        "llmStreamCall": summarized.get("llm_stream_call", False),
        "rawOutput": summarized.get("raw_output"),
        "shadowedRange": {"start": summarized["start"], "end": summarized["end"]},
        "shadowedSeqs": list(summarized["shadowed_seqs"]),
        "shadowedTokenCount": summarized["shadowed_token_count"],
        "provider": summarized["provider"],
        "model": summarized["model"],
        **({"maxTokens": summarized["max_tokens"]} if summarized.get("max_tokens") is not None else {}),
        **({"usage": summarized["usage"]} if summarized.get("usage") is not None else {}),
    })
    session.append(
        "user/message",
        summarized["checkpoint_message"],
        surface_op={"op": "replace", "start": summarized["start"], "end": summarized["end"]},
        source_event_seqs=[start_event.seq, summary_event.seq, *summarized["shadowed_seqs"]],
    )
    return {
        "compactionId": start_event.data["compactionId"],
        **({"sourceCommandId": start_event.data["sourceCommandId"]} if "sourceCommandId" in start_event.data else {}),
        "startSeq": start_event.seq,
        "summarySeq": summary_event.seq,
        "summary": summarized["summary"],
        "shadowedRange": {"start": summarized["start"], "end": summarized["end"]},
        "shadowedSeqs": list(summarized["shadowed_seqs"]),
        "shadowedTokenCount": summarized["shadowed_token_count"],
    }


async def compact_surface_region(
    dependencies: dict,
    session: Session,
    start: int,
    end: int,
    agent: Any,
    options: dict,
    signal: Any = None,
) -> CompactionResult:
    """在选中的表面区间上运行单次压缩事务（锁 → 摘要 → 提交 → 收尾）。"""
    if options.get("owner") is None:
        _signal_throw(signal)
    selection = _validate_surface_region(session, start, end)
    entry_state = _inspect_compaction_entry_state(session.events)
    _assert_compaction_inactive(entry_state, "compaction")

    if options.get("owner") is None:
        if entry_state["open_turn"] is not None:
            raise ManualCompactionError("busy", "manual compaction: 会话已有打开中的轮")
        owner: Optional[int] = None
    else:
        if entry_state["open_turn"] is None:
            raise RuntimeError("compactRegion: 无打开中的轮——自动压缩事件必须封闭在轮内")
        owner = entry_state["open_turn"]

    compaction_id = CompactionId(uuid.uuid4().hex)
    lifecycle: dict = {"compactionId": compaction_id, "turn": owner}
    if options.get("source_command_id") is not None:
        lifecycle["sourceCommandId"] = options["source_command_id"]
    start_event = session.append("compaction/start", lifecycle)
    stability = options.get("stability", "whole-surface")
    failure: Optional[dict] = None
    flush_failure: Any = None
    result: Optional[CompactionResult] = None
    closed = False
    closing = False
    stage = "summary"

    try:
        prepared = _prepare_compaction(dependencies, session, selection)
        summarized = await _summarize_compaction(
            dependencies, prepared, agent, compaction_id,
            options.get("source_command_id"), signal,
        )
        if options.get("owner") is None:
            _signal_throw(signal)
        if stability == "whole-surface":
            _assert_whole_surface_unchanged(dependencies, session, summarized)
        else:
            _assert_selected_span_stable(dependencies, session, summarized)
        stage = "commit"
        pending = _commit_compaction_body(session, start_event, summarized)
        closing = True
        end_event = session.append("compaction/end", lifecycle)
        closed = True
        result = CompactionResult(**pending, endSeq=end_event.seq)
    except Exception as error:  # noqa: BLE001 - 任何失败都要闭合锁
        failure = {"error": error, "stage": "commit" if closing else stage}
        if not closing:
            closing = True
            try:
                session.append("compaction/end", {**lifecycle, "error": _error_chain(error)})
                closed = True
            except Exception as close_error:  # noqa: BLE001
                failure = {"error": close_error, "stage": "commit"}

    if closed and options.get("flush") is not None:
        try:
            # flush 可以是同步或协程（compact_now 的耐久检查点）
            flush_result = options["flush"]()
            if asyncio.iscoroutine(flush_result):
                await flush_result
        except Exception as error:  # noqa: BLE001
            flush_failure = error

    if options.get("owner") is None:
        _signal_throw(signal)
    if failure is not None:
        if options.get("owner") is None:
            _throw_manual_failure(failure)
        raise failure["error"]
    if flush_failure is not None:
        raise ManualCompactionError("persistence", "manual compaction 耐久检查点失败", flush_failure)
    if result is None:
        raise RuntimeError("compaction 提交后无结果")
    return result


def _throw_manual_failure(failure: dict) -> None:
    """把一次已闭合的手工尝试分类为预期失败（不削弱取消优先级）。"""
    if failure["stage"] == "commit":
        raise ManualCompactionError("commit", "manual compaction 未干净提交", failure["error"])
    raise ManualCompactionError("summary", "manual compaction 无法产出更小的摘要", failure["error"])


# --------------------------------------------------------------------------- #
# 摘要器
# --------------------------------------------------------------------------- #
SUMMARY_OPEN_TAG = "<compacted-summary>"
SUMMARY_CLOSE_TAG = "</compacted-summary>"
CHECKPOINT_PREAMBLE = (
    "This is an automatically generated checkpoint condensing an earlier span of the "
    "conversation to free up context. Treat the captured context as established background "
    "and build on it without restating it. Continue the task directly from the messages "
    "that follow, without acknowledging this checkpoint."
)
COMPACTION_INSTRUCTION = """You are now acting as a compaction engine for this AI coding assistant. Condense the conversation ABOVE into a structured checkpoint that lets another model resume the work with no loss of essential context.

Output EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write "(none)" for an empty section — never drop a section.

## Primary Request and Intent
- [the user's original and evolving goals; quote verbatim where the exact wording matters]

## Key Technical Concepts
- [technologies, frameworks, patterns, and conventions in play]

## Files and Code
- [exact path: why it matters, key changes or snippets]

## Errors and Fixes
- [error: how it was resolved, plus any related user feedback]

## Pending Jobs
- [explicitly requested work not yet completed]

## Current Work
- [precisely what was in progress at this checkpoint]

## Next Step
- [the single next action, directly in line with the most recent request, or "(none)"]

## Critical Context
- [decisions and their rationale, constraints, user preferences, open questions, data needed to continue]

Rules:
- Write concise English engineering prose. Preserve exact file paths, commands, error strings, identifiers, numeric values, function signatures, and syntax fragments.
- Capture user feedback and explicit instructions faithfully, especially corrections.
- Do NOT mention this summarization request or that the context was compacted.
- Output only the checkpoint text: do not call any tool or take any other action.
- If the conversation already contains a <compacted-summary> block, it is a PRIOR checkpoint. Do not copy it forward verbatim: preserve still-true facts, drop stale ones, and merge newer information into a single consolidated summary under the same structure."""


def frame_summary(summary: list) -> list:
    """把安全文本摘要块包进持久检查点帧。"""
    return [TextBlock(f"{CHECKPOINT_PREAMBLE}\n\n{SUMMARY_OPEN_TAG}"), *summary, TextBlock(SUMMARY_CLOSE_TAG)]


async def summarize_with_llm(
    ctx: AppContext, config: dict, input_data: dict, agent: Any, signal: Any = None,
) -> dict:
    """默认缓存复用的 ``ctx.llm.stream()`` 摘要调用（重放前缀 + 追加指令）。"""
    latest = (agent.session.request_header or {}).get("config")
    configured = (
        {"provider": config["summarizationProvider"], "model": config["summarizationModel"]}
        if config["summarizationProvider"] else None
    )
    agent_target = (
        {"provider": agent.options.provider, "model": agent.options.model}
        if agent.options.provider and agent.options.model else None
    )
    target = configured or latest or agent_target
    if target is None:
        raise RuntimeError(
            "无可用摘要 provider/model：请配置 BasicCompactionConfig 的 summarization 字段，"
            "路由一次请求，或设置 AgentOptions 的 provider/model"
        )
    assembler = BlockAssembler()
    messages = [
        *input_data.get("messages", []),
        create_user_message(
            [TextBlock(COMPACTION_INSTRUCTION)],
            source=MessageSource("plugin", plugin="dsh-compaction-basic"),
        ),
    ]
    options = GenerateOptions(
        provider=target["provider"],
        model=target["model"],
        messages=messages,
        system=input_data.get("system"),
        tools=list(input_data["tools"]) if input_data.get("tools") else None,
        max_tokens=config["maxTokens"],
        session_id=agent.session.header.id,
        purpose="compaction",
        signal=signal,
    )
    async for chunk in ctx.llm.stream(options):
        assembler.push(chunk)

    finish = assembler.finish
    if finish.get("kind") == "error":
        raise RuntimeError(f"summarization 失败: {finish.get('failure') or finish}")
    if finish.get("kind") == "aborted":
        raise RuntimeError(f"summarization 被取消: {finish.get('failure') or finish}")
    if finish.get("kind") == "max-tokens":
        raise RuntimeError("summarization 在 token 上限处截断（不完整检查点）")

    raw_output = list(assembler.blocks)
    summary = [b for b in raw_output if isinstance(b, TextBlock)]
    if not any(b.text.strip() for b in summary):
        raise RuntimeError("summarization 未产出文本摘要内容")
    return {
        "summary": summary,
        "raw_output": raw_output,
        "llm_stream_call": True,
        "provider": options.provider,
        "model": options.model,
        "max_tokens": config["maxTokens"],
        "usage": assembler.usage,
    }


async def _summarize_compaction(
    dependencies: dict, prepared: dict, agent: Any, compaction_id: str,
    source_command_id: Optional[str], signal: Any = None,
) -> dict:
    """运行摘要器并帧装其替换检查点（校验摘要必须更小）。"""
    summary_result = await dependencies["summarize"](prepared["input"], agent, signal)
    checkpoint_message = create_user_message(
        frame_summary(summary_result["summary"]),
        source=compact_checkpoint_source(compaction_id, source_command_id),
    )
    framed_tokens = dependencies["meter"].estimate_message(checkpoint_message)
    if framed_tokens >= prepared["shadowed_token_count"]:
        raise RuntimeError(
            f"摘要不小于被遮蔽内容（{framed_tokens} 帧估算 >= {prepared['shadowed_token_count']}）"
        )
    return {**prepared, **summary_result, "checkpoint_message": checkpoint_message}


# --------------------------------------------------------------------------- #
# BasicCompactionEngine
# --------------------------------------------------------------------------- #
def _routed_target(session: Session) -> Optional[dict]:
    """最近一次持久路由请求的 provider/model。"""
    config = (session.request_header or {}).get("config")
    if config is None or not config.get("provider") or not config.get("model"):
        return None
    return {"provider": config["provider"], "model": config["model"]}


class BasicCompactionEngine(CompactionEngine):
    """依赖轻量的压缩后端：``ctx.tokenMeter`` 供压力/保留/收敛定价。

    ``summarize()`` 是唯一的子类定制钩子；重放与持久变更策略保持固定，使每个
    定价决策都使用单例 token meter。
    """

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx)
        self.config = resolve_config(config)
        self._overflow_retries: dict[int, int] = {}
        self._overflow_agents: dict[str, Any] = {}
        if self.config["auto"]:
            self._register_automatic_compaction()

    # -- 自动挂钩 -------------------------------------------------------- #
    def _register_automatic_compaction(self) -> None:
        ctx = self.ctx
        logger = getattr(ctx, "logger", None)

        def log_result(result: CompactionResult, trigger: str) -> None:
            if logger is not None:
                logger.info(
                    f"compaction ({trigger}): shadowed {len(result.shadowedSeqs)} surface nodes "
                    f"(seqs {result.shadowedRange.get('start')}-{result.shadowedRange.get('end')}, "
                    f"~{result.shadowedTokenCount} tokens)"
                )

        # 步边界压力：agent/pre-step 瀑布流，压缩后放行默认决策
        async def pre_step(payload: dict, nxt: Callable) -> Any:
            signal = payload.get("signal")
            if signal is None or not getattr(signal, "aborted", False):
                try:
                    result = await self.compact_if_needed(payload["agent"], "pressure", signal)
                    if result is not None:
                        log_result(result, "step pressure")
                except Exception as error:  # noqa: BLE001
                    if logger is not None:
                        logger.warn(f"step compaction failed: {_error_chain(error)}; continuing the turn")
            return await nxt()

        ctx.on("agent/pre-step", pre_step)

        # 空闲即清除溢出恢复序列
        def on_status(payload: dict) -> None:
            if payload.get("status") == "idle":
                self._overflow_retries.pop(id(payload.get("agent")), None)

        ctx.on("agent/status", on_status)

        # 提供方确认的上下文溢出：agent/request-error → 压缩 → 重试决策
        async def request_error(payload: dict, nxt: Callable) -> Any:
            agent = payload.get("agent")
            failure = payload.get("failure")
            signal = payload.get("signal")
            code = getattr(failure, "code", None)
            if code != CONTEXT_WINDOW_EXCEEDED_CODE or (signal is not None and getattr(signal, "aborted", False)):
                return await nxt()
            self._overflow_agents[agent.session.header.id] = agent
            target = _routed_target(agent.session)
            if target is None:
                return await nxt()
            policy = resolve_target_policy(self.config, target)
            retries = self._overflow_retries.get(id(agent), 0)
            if retries >= policy["maxOverflowRetries"]:
                return await nxt()
            generation = agent.session.surface["replace_generation"]
            try:
                result = await self.compact_if_needed(agent, "context-overflow", signal)
            except Exception as recovery_error:  # noqa: BLE001
                if (signal is None or not getattr(signal, "aborted", False)) \
                        and agent.session.surface["replace_generation"] > generation:
                    self._overflow_retries[id(agent)] = retries + 1
                    if logger is not None:
                        logger.warn(
                            f"context-overflow compaction failed after durable surface progress: "
                            f"{_error_chain(recovery_error)}; retrying from the replacement surface"
                        )
                    return {"kind": "retry"}
                return await nxt()
            if (signal is not None and getattr(signal, "aborted", False)) \
                    or agent.session.surface["replace_generation"] <= generation:
                return await nxt()
            if result is not None:
                log_result(result, "context overflow recovery")
            self._overflow_retries[id(agent)] = retries + 1
            return {"kind": "retry"}

        ctx.on("agent/request-error", request_error)

    # -- 摘要钩子 -------------------------------------------------------- #
    async def summarize(self, input_data: dict, agent: Any, signal: Any = None) -> dict:
        target = _routed_target(agent.session)
        config = self.config if target is None else resolve_target_policy(self.config, target)
        return await summarize_with_llm(self.ctx, config, input_data, agent, signal)

    # -- 引擎入口 -------------------------------------------------------- #
    async def compact_if_needed(self, agent: Any, trigger: str, signal: Any = None) -> Optional[CompactionResult]:
        target = _routed_target(agent.session)
        if target is None:
            return None
        policy = resolve_target_policy(self.config, target)
        meter = self.ctx.tokenMeter
        measurement = meter.measure(agent.session)
        # 修剪是可选的：compaction-basic 保持可独立组合（ctx.toolResultPruner 挂载后生效）
        prune = getattr(self.ctx, "toolResultPruner", None)

        if trigger == "context-overflow":
            # 溢出绕过常规阈值与保留尾部，强制一次有用的平衡缩减
            if prune is not None:
                prune.prune_session(agent.session)
                measurement = meter.measure(agent.session)
            range_ = select_compactable_range(agent.session, measurement, 0)
            if range_ is None:
                return None
            return await self.compact_region(range_["start"], range_["end"], agent, signal)

        # pressure：解析目标模型容量并检查目标特定阈值
        context = await self._context_window(target, signal)
        assert_no_active_compaction(agent.session, "automatic pressure compaction")
        target_key = f"{target['provider']}/{target['model']}"
        if context is None:
            raise TargetPressureConfigError(
                target_key,
                f"compaction-basic: 无 {target_key} 的上下文容量；请在适配器模型上配置 contextWindow",
            )
        spec = resolve_compact_spec(policy, context)
        if measurement["total_tokens"] < spec["thresholdTokens"]:
            return None
        # 一旦压力达标，先落模型无关修剪再重测（同一次折叠内的重新定价）
        if prune is not None:
            prune.prune_session(agent.session)
            measurement = meter.measure(agent.session)
        if measurement["total_tokens"] < spec["thresholdTokens"]:
            return None
            return None
        result: Optional[CompactionResult] = None
        for _attempt in range(spec["compactionRetries"] + 1):
            range_ = select_compactable_range(agent.session, measurement, spec["retainTokens"])
            if range_ is None:
                return None if result is None else result
            result = await self.compact_region(range_["start"], range_["end"], agent, signal)
            measurement = meter.measure(agent.session)
            if measurement["total_tokens"] < spec["thresholdTokens"]:
                return result
        raise RuntimeError(
            f"compaction 经过 {spec['compactionRetries'] + 1} 次尝试仍高于阈值 "
            f"({measurement['total_tokens']} 估算 tokens >= 阈值 {spec['thresholdTokens']})"
        )

    async def compact_region(self, start: int, end: int, agent: Any, signal: Any = None) -> CompactionResult:
        """压缩 agent 会话表面上的一段含首尾区间（当前轮事务，全表面稳定校验）。"""
        return await compact_surface_region(
            self._region_dependencies(),
            agent.session,
            start,
            end,
            agent,
            {"owner": "current-turn", "stability": "whole-surface"},
            signal,
        )

    async def compact_now(self, agent: Any, signal: Any = None,
                          source_command_id: Optional[str] = None) -> Optional[CompactionResult]:
        """在压力阈值之下强制一次有用的空闲会话压缩；仅在独立标记对耐久检查点后返回。"""
        _signal_throw(signal)
        try:
            coro = agent.run_maintenance(
                lambda agent_signal: self._compact_now_inner(agent, agent_signal, signal, source_command_id)
            )
            return await coro
        except ManualCompactionError:
            raise
        except Exception as error:  # noqa: BLE001
            raise ManualCompactionError("busy", "manual compaction 需要空闲 agent 且无排队的唤醒工作", error)

    async def _compact_now_inner(
        self, agent: Any, agent_signal: CancelSignal,
        caller_signal: Any, source_command_id: Optional[str],
    ) -> Optional[CompactionResult]:
        operation_signal = CancelSignal.any([agent_signal, caller_signal])
        operation_signal.throw_if_aborted()
        range_ = select_compactable_range(
            agent.session, self.ctx.tokenMeter.measure(agent.session), 0,
        )
        if range_ is None:
            return None
        try:
            return await compact_surface_region(
                self._region_dependencies(),
                agent.session,
                range_["start"],
                range_["end"],
                agent,
                {
                    "owner": None,
                    "stability": "selected-span",
                    "source_command_id": source_command_id,
                    "flush": lambda: self.ctx.sessions.flush(agent.session),
                },
                operation_signal,
            )
        except Exception as error:  # noqa: BLE001
            if getattr(agent_signal, "aborted", False):
                raise ManualCompactionError("cancelled", "manual compaction 被取消", error)
            operation_signal.throw_if_aborted()
            raise

    # -- 内部 ------------------------------------------------------------ #
    async def _context_window(self, target: dict, signal: Any = None) -> Optional[int]:
        info = await self.ctx.llm.resolve_model_info(target["provider"], target["model"], signal)
        if not isinstance(info, dict):
            return None
        context = info.get("context")
        if not isinstance(context, dict):
            return None
        return context.get("context_window")

    def _region_dependencies(self) -> dict:
        return {
            "meter": self.ctx.tokenMeter,
            "summarize": lambda input_data, owner, abort=None: self.summarize(input_data, owner, abort),
        }


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``compaction`` 服务（基础压缩后端，含自动挂钩）。"""
    BasicCompactionEngine(ctx, config)


apply.provides = ["compaction"]
apply.inject = ["llm", "tokenMeter", "sessions"]

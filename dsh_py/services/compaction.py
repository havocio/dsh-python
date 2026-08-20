"""压缩能力 seam（compaction）：把一段历史表面替换为一个摘要节点
（对标 dsh 的 ``dsh-compaction``）。

后端实现拥有触发策略、保留与摘要，可消费独立的测量服务。一次成功的运行把
选中的表面区间替换为一个摘要节点，并阻止同一会话的并发压缩。替换用户消息
使用 :func:`compact_checkpoint_source`（携带事务身份），使消费方不依赖后端
即可识别并关联它。每个上下文加载一个实现为 ``ctx.compaction``。
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, NewType, Optional, Union

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.brand import MessageId
from dsh_py.services.message import ToolCallBlock
from dsh_py.services.session import Session, SessionEvent

#: 一次 compact start/summary/end 事务的稳定身份。
CompactionId = NewType("CompactionId", str)

#: 为什么自动策略请后端考虑压缩。
CompactionTrigger = Union[str, Any]  # 'pressure' | 'context-overflow'

#: 显式空闲会话压缩请求的预期失败类别。
ManualCompactionErrorCode = Union[str, Any]  # busy/cancelled/changed/summary/commit/persistence


class ManualCompactionError(Exception):
    """预期的手工压缩失败（携带稳定失败类别）。"""

    def __init__(self, code: str, message: str, cause: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.cause = cause

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


@dataclass
class CompactionResult:
    """一次成功压缩操作的完整结果。"""

    compactionId: str                  # 本压缩完整持久生命周期的稳定身份
    sourceCommandId: Optional[str] = None  # 手工发起命令（如有）
    startSeq: int = 0                  # 追加的 compaction/start 事件 seq
    summarySeq: int = 0                # 追加的 compaction/summary 事件 seq
    endSeq: int = 0                    # 追加的 compaction/end 事件 seq
    summary: list = field(default_factory=list)   # 摘要内容块
    shadowedRange: dict = field(default_factory=dict)  # {start, end} 被遮蔽区间（表面位置）
    shadowedSeqs: list = field(default_factory=list)   # 被遮蔽的表面节点 seq（表面顺序）
    shadowedTokenCount: int = 0        # 被遮蔽内容的估算 token 数


# 压缩检查点来源的持久标记（对齐 dsh 的 COMPACT_CHECKPOINT_MARKER）
_COMPACT_MARKER = {"kind": "plugin", "plugin": "compact"}


def compact_checkpoint_source(compaction_id: str, source_command_id: Optional[str] = None) -> dict:
    """构造与一次压缩事务关联的检查点来源（替换用户消息的 provenance）。

    返回 ``{"kind": "plugin", "plugin": "compact", "compactionId": ...}``。
    """
    source = dict(_COMPACT_MARKER)
    source["compactionId"] = compaction_id
    if source_command_id is not None:
        source["sourceCommandId"] = source_command_id
    return source


def is_compact_checkpoint_source(source: Any) -> bool:
    """判定一条持久化消息来源是否标识压缩检查点（后端无关标记）。"""
    return (
        getattr(source, "kind", None) == "plugin"
        and getattr(source, "plugin", None) == _COMPACT_MARKER["plugin"]
    ) or (isinstance(source, dict) and source.get("kind") == "plugin"
          and source.get("plugin") == _COMPACT_MARKER["plugin"])


# --------------------------------------------------------------------------- #
# 工具配对平衡（tool-pairing）：表面切口是否切开未闭合的工具调用/结果对
# --------------------------------------------------------------------------- #
def _event_delta(event: SessionEvent) -> int:
    """一条表面事件对「进行中工具调用数」的增量。"""
    if event.type == "assistant/message":
        content = getattr(event.data["message"], "content", ()) or ()
        return sum(1 for block in content if isinstance(block, ToolCallBlock))
    if event.type == "tool/result":
        return -1
    return 0


def _surface_event(session: Session, seq: int) -> SessionEvent:
    """读取并校验表面节点 seq 对应的事件（表面损坏时抛错）。

    表面 seq 从 1 起（日志索引 = seq-1）。
    """
    event = session.events[seq - 1] if 1 <= seq <= len(session.events) else None
    if event is None or event.seq != seq:
        raise RuntimeError(f"tool-pairing balance: surface seq {seq} 无匹配日志事件（表面损坏）")
    return event


# 会话 → (generation, cut_balanced, index_by_seq) 的弱引用缓存
_BALANCE_CACHE: "weakref.WeakKeyDictionary[Session, tuple]" = weakref.WeakKeyDictionary()


def _balance_cache(session: Session) -> tuple:
    """与当前会话表面同步的平衡状态：每次切口（含首尾）是否工具配对平衡。"""
    surface = session.surface
    nodes = surface["nodes"]
    generation = surface["replace_generation"]
    cached = _BALANCE_CACHE.get(session)
    if cached is None or cached[0] != generation or len(cached[1]) - 1 != len(nodes):
        cut_balanced = [True]  # 表面前的首个切口平凡平衡
        index_by_seq: dict[int, int] = {}
        in_progress = 0
        for index, seq in enumerate(nodes):
            in_progress += _event_delta(_surface_event(session, seq))
            if in_progress < 0:
                raise RuntimeError(
                    f"tool-pairing balance: surface seq {seq} 的 tool/result 无匹配工具调用（表面损坏）"
                )
            cut_balanced.append(in_progress == 0)
            index_by_seq[seq] = index
        cached = (generation, cut_balanced, index_by_seq)
        _BALANCE_CACHE[session] = cached
    return cached


def _cut_balance(session: Session, seq: int, offset: int) -> bool:
    """seq 位置（+offset）的切口平衡；seq 不在当前表面时抛错。"""
    cache = _balance_cache(session)
    index = cache[2].get(seq)
    balanced = cache[1][index + offset] if index is not None else None
    if balanced is None:
        raise RuntimeError(f"tool-pairing balance: surface seq {seq} 不在当前表面")
    return balanced


def tool_pairing_balanced_before(session: Session, seq: int) -> bool:
    """当前表面序列 **前** 的切口是否工具配对平衡（无未答工具调用越过切口）。"""
    return _cut_balance(session, seq, 0)


def tool_pairing_balanced_after(session: Session, seq: int) -> bool:
    """当前表面序列 **后** 的切口是否工具配对平衡。"""
    return _cut_balance(session, seq, 1)


# --------------------------------------------------------------------------- #
# 压缩引擎抽象
# --------------------------------------------------------------------------- #
class CompactionEngine(Service):
    """抽象压缩服务（``ctx.compaction``）；实现类拥有触发策略与摘要。"""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "compaction")

    def compact_if_needed(self, agent: Any, trigger: str, signal: Any = None) -> Optional[CompactionResult]:
        """为一个显式触发考虑自动压缩；无安全区间时返回 None。"""
        raise NotImplementedError

    def compact_now(self, agent: Any, signal: Any = None,
                    source_command_id: Optional[str] = None) -> Optional[CompactionResult]:
        """显式压缩有用历史（即使低于自动阈值）；无安全区间返回 None。"""
        raise NotImplementedError

    def compact_region(self, start: int, end: int, agent: Any, signal: Any = None) -> CompactionResult:
        """强制把一段表面节点压缩为单个摘要节点（须工具配对平衡）。"""
        raise NotImplementedError


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口占位：实际由具体后端（如 ``compaction_basic:apply``）注册。"""
    raise NotImplementedError("请加载具体压缩后端，如 dsh_py.services.compaction_basic:apply")

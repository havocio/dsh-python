"""工具结果修剪器（tool-result-pruner，对标 dsh 的 ``dsh-compaction-tool-result-pruner``）：
重放安全、模型无关的工具结果修剪。

超预算的 ``tool/result`` 文本内容按「头 / 中 / 尾」确定性裁剪（保留头部与尾部，
中间以 :data:`PRUNE_MARKER` 替换），每次替换：

- **保留事件其余字段**（turn/step/source 等），只替换 ``content``；
- **紧邻前置 ``compaction/prune`` shadow-price 事件**：以注入的 token meter 为
  被遮蔽节点定价，纯消费方可直接减去它而无需保留逐节点状态；
- **surface 单节点替换**（``surface_op={"op": "replace", "start": seq, "end": seq}``），
  并携带 ``source_event_seqs`` 供重放恢复替换输入。

修剪按「当前表面快照」遍历（重放安全：候选在遍历前固化）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.message import TextBlock, ToolResultBlock

#: 每个被移除中间段的固定替换标记。
PRUNE_MARKER = "\n\n[... tool result middle pruned ...]\n\n"

#: 编码助手工具输出的低摩擦默认预算（字符）。
DEFAULTS = {"thresholdChars": 8192, "headChars": 4096, "tailChars": 1024}

_CONFIG_KEYS = {"thresholdChars", "headChars", "tailChars"}


def code_point_length(text: str) -> int:
    """Unicode code point 计数（Python ``str`` 长度即 code point 数，不切分代理对）。"""
    return len(text)


def resolve_config(config: Optional[dict] = None) -> dict:
    """解析并校验修剪预算（未知键拒绝；head+marker+tail 必须不超 threshold）。"""
    config = config or {}
    for key in config:
        if key not in _CONFIG_KEYS:
            raise ValueError(f"ToolResultPruneConfig: 未知键 {key!r}（允许: thresholdChars, headChars, tailChars）")
    resolved = {
        "thresholdChars": config.get("thresholdChars", DEFAULTS["thresholdChars"]),
        "headChars": config.get("headChars", DEFAULTS["headChars"]),
        "tailChars": config.get("tailChars", DEFAULTS["tailChars"]),
    }
    for name in ("thresholdChars",):
        if not isinstance(resolved[name], int) or resolved[name] <= 0:
            raise ValueError(f"ToolResultPruneConfig: {name}（{resolved[name]}）必须是正整数")
    for name in ("headChars", "tailChars"):
        if not isinstance(resolved[name], int) or resolved[name] < 0:
            raise ValueError(f"ToolResultPruneConfig: {name}（{resolved[name]}）必须是非负整数")
    emitted = resolved["headChars"] + len(PRUNE_MARKER) + resolved["tailChars"]
    if emitted > resolved["thresholdChars"]:
        raise ValueError(
            f"ToolResultPruneConfig: headChars + marker + tailChars（{emitted}）"
            f" 必须不超过 thresholdChars（{resolved['thresholdChars']}）"
        )
    return resolved


class ToolResultPruner(Service):
    """``toolResultPruner`` 服务：确定性头/中/尾修剪（``ctx.toolResultPruner``）。"""

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx, "toolResultPruner")
        self.config = resolve_config(config)

    # ------------------------------------------------------------------ #
    # 测量 / 裁剪
    # ------------------------------------------------------------------ #
    def measure_content(self, blocks: list) -> int:
        """测量内容块的文本字符数（code points）；非文本块计零。"""
        total = 0
        for block in blocks:
            if isinstance(block, TextBlock):
                total += code_point_length(block.text)
        return total

    def prune_content(self, blocks: list) -> Optional[list]:
        """超预算时替换文本中间段（保留富块顺序；按 code point 切片）。

        返回裁剪后的内容块列表（保留结构），预算内返回 None。
        """
        total_chars = self.measure_content(blocks)
        if total_chars <= self.config["thresholdChars"]:
            return None
        removed_start = self.config["headChars"]
        removed_end = total_chars - self.config["tailChars"]
        pruned: list = []
        consumed = 0
        marker_inserted = False
        for block in blocks:
            if not isinstance(block, TextBlock):
                pruned.append(block)
                continue
            points = list(block.text)
            block_start = consumed
            block_end = block_start + len(points)
            head_end = min(len(points), max(0, removed_start - block_start))
            tail_start = min(len(points), max(0, removed_end - block_start))
            intersects_removed = block_start < removed_end and block_end > removed_start
            marker = PRUNE_MARKER if (intersects_removed and not marker_inserted) else ""
            if marker:
                marker_inserted = True
            text = "".join(points[:head_end]) + marker + "".join(points[tail_start:])
            if text:
                pruned.append(TextBlock(text))
            consumed = block_end
        if not marker_inserted:
            raise RuntimeError("tool-result prune: 未定位到被移除的文本区间")
        chars_after = self.measure_content(pruned)
        if chars_after > self.config["thresholdChars"] or chars_after >= total_chars:
            raise RuntimeError("tool-result prune: 替换必须更小且不超阈值")
        return pruned

    # ------------------------------------------------------------------ #
    # 会话修剪
    # ------------------------------------------------------------------ #
    def prune_session(self, session: Any) -> dict:
        """修剪当前表面每个超预算工具结果（重放安全：候选先固化）。

        返回 ``{"pruned": [...], "charsRemoved": int}``；替换已落地的部分保持
        耐久（后续失败不影响已提交者）。
        """
        candidates: list[tuple[int, Any]] = []
        for seq in list(session.surface["nodes"]):
            event = session.events[seq - 1] if 1 <= seq <= len(session.events) else None
            if event is not None and event.type == "tool/result":
                candidates.append((seq, event))

        pruned: list[dict] = []
        chars_removed = 0
        for seq, event in candidates:
            message = event.data.get("message")
            blocks = list(getattr(message, "content", ()) or ())
            if not blocks or not isinstance(blocks[0], ToolResultBlock):
                continue
            block = blocks[0]
            new_content = self.prune_content(list(block.content))
            if new_content is None:
                continue
            chars_before = self.measure_content(list(block.content))
            chars_after = self.measure_content(new_content)
            new_block = ToolResultBlock(
                tool_call_id=block.tool_call_id,
                content=tuple(new_content),
                is_error=block.is_error,
            )
            new_message = replace(message, content=(new_block,))
            # Shadow-price 协议：计量事件与其替换同步相邻追加
            session.append("compaction/prune", {
                "shadowedRange": {"start": seq, "end": seq},
                "shadowedSeqs": [seq],
                "shadowedTokenCount": self.ctx.tokenMeter.estimate_message(message),
            })
            replacement = session.append(
                "tool/result",
                {**event.data, "message": new_message},
                surface_op={"op": "replace", "start": seq, "end": seq},
                source_event_seqs=[seq],
            )
            pruned.append({
                "originalSeq": seq,
                "replacementSeq": replacement.seq,
                "callId": block.tool_call_id,
                "charsBefore": chars_before,
                "charsAfter": chars_after,
            })
            chars_removed += chars_before - chars_after
        return {"pruned": pruned, "charsRemoved": chars_removed}


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``toolResultPruner`` 服务（模型无关工具结果修剪）。"""
    ToolResultPruner(ctx, config)


apply.provides = ["toolResultPruner"]
apply.inject = ["tokenMeter"]

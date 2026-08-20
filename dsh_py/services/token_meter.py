"""Token 计量（token-meter）：零依赖的 token 估算服务（对标 dsh 的 ``dsh-token-meter`` 子集）。

compaction 需要统一的压力测量、保留预算与摘要收敛定价。dsh 用
``dsh-token-meter``（固定估计器 + 会话表面重放折叠）；本复刻提供零依赖的
启发式版本：

- :meth:`TokenMeter.estimate_text` —— 文本估算（CJK 按字、其余按 4 字符/token）；
- :meth:`TokenMeter.estimate_message` —— 一条消息的估算；
- :meth:`TokenMeter.measure` —— 对一个会话表面做测量：每个表面节点一条
  ``{"seq", "tokens"}``，外加 ``total_tokens``。

测量结果与当前会话表面严格一一对应（顺序与长度一致），否则抛错——compaction
据此检测「token-meter 表面与会话表面不匹配」。
"""

from __future__ import annotations

import math
import re
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.session import Session

# CJK 区间：每字约 1 token
_CJK = re.compile(r"[\u4e00-\u9fff]")
# 非 CJK 字符按 4 字符/token
_CHARS_PER_TOKEN = 4.0


def estimate_text(text: str) -> int:
    """估算一段文本的 token 数（CJK 逐字 + 其余 4 字符/token）。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return max(1, math.ceil(cjk + other / _CHARS_PER_TOKEN))


def _message_text(message: Any) -> str:
    """从 Message 对象/内容块/tuple 提取文本（对齐 message.py 的 as_text 语义）。"""
    if message is None:
        return ""
    if isinstance(message, tuple):
        return "".join(_message_text(block) for block in message)
    # 内容块本身：直接取文本/参数
    if hasattr(message, "text") and message.text:
        return message.text
    if hasattr(message, "arguments") and message.arguments:
        return message.arguments
    content = getattr(message, "content", ()) or ()
    parts: list[str] = []
    for block in content:
        if hasattr(block, "text") and block.text:
            parts.append(block.text)
        elif hasattr(block, "arguments") and block.arguments:
            parts.append(block.arguments)
        elif hasattr(block, "content") and hasattr(block, "tool_call_id"):
            # ToolResultBlock：递归其内部文本块（供工具结果 shadow price）
            parts.append(_message_text(block.content))
    return "".join(parts)


class TokenMeter(Service):
    """``tokenMeter`` 服务：会话表面 token 测量与消息估算（``ctx.tokenMeter``）。"""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "tokenMeter")

    # ------------------------------------------------------------------ #
    # 估算
    # ------------------------------------------------------------------ #
    def estimate_text(self, text: str) -> int:
        return estimate_text(text)

    def estimate_message(self, message: Any) -> int:
        """估算一条消息的 token 数。"""
        return estimate_text(_message_text(message))

    # ------------------------------------------------------------------ #
    # 测量
    # ------------------------------------------------------------------ #
    def measure(self, session: Session) -> dict:
        """测量一个会话的表面：每个节点一条 ``{"seq", "tokens"}`` + 总数。

        :raises RuntimeError: token-meter 表面（事件日志重放）与会话当前表面
            不匹配（顺序/长度不一致，如表面已损坏）。
        """
        nodes: list[dict] = []
        total = 0
        for seq in session.surface["nodes"]:
            # 表面 seq 从 1 起（日志索引 = seq-1）
            event = session.events[seq - 1] if 1 <= seq <= len(session.events) else None
            if event is None or event.seq != seq:
                raise RuntimeError(f"token-meter surface: seq {seq} 无匹配日志事件（表面损坏）")
            message = session.derive_event_message(event)
            tokens = self.estimate_message(message) if message is not None else 0
            nodes.append({"seq": seq, "tokens": tokens})
            total += tokens
        return {"nodes": nodes, "total_tokens": total}


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``tokenMeter`` 服务（token 估算 + 表面测量）。"""
    TokenMeter(ctx)


apply.provides = ["tokenMeter"]  # 声明：本插件提供 tokenMeter 服务

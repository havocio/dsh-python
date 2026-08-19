"""系统指令插件：在每轮第一步把工作指令作为上下文注入（演示 ``agent/pre-step`` 注入 seam）。

与 dsh 的 ``agent-instructions`` 思路一致——指令作为 ``form:'instructions'`` 的
上下文进入模型可见历史，而非塞进 system 提示词。真正的 system 提示词请通过
``AgentOptions.system`` 设置；本插件演示的是「插件在不改核心的情况下注入上下文」。
"""

from __future__ import annotations

from typing import Any, Optional

from dsh_py.services.message import MessageSource, TextBlock, as_text, create_user_message

name = "system-instructions"


def apply(ctx: Any, config: Optional[dict] = None) -> None:
    """注册指令注入。``config.instructions`` 为指令文本（可多行）。"""
    config = config or {}
    instructions = config.get("instructions", "")
    if not instructions:
        return

    def render() -> str:
        return "以下是你的工作指令（请严格遵循）：\n" + instructions

    @ctx.on("agent/pre-step")
    async def inject(payload: dict, nxt) -> Any:
        decision = await nxt()
        if decision["kind"] == "reject":
            return decision
        # 仅在本轮第一步注入，避免重复进入历史
        if payload.get("step") != 1:
            return decision
        text = render()
        # 去重：若本轮已包含相同内容则跳过
        if any(m.role == "user" and as_text(m.content) == text for m in decision["messages"]):
            return decision
        msg = create_user_message(
            [TextBlock(text)],
            MessageSource("plugin", plugin="system-instructions", form="instructions"),
        )
        return {"kind": "enter", "messages": [msg, *decision["messages"]]}

"""子代理控制工具（tool-subagent-control，对标 dsh 的 ``dsh-tool-subagent-control``）：
把 ``ctx.subagents`` 的可续跑子代理能力暴露为模型可调用的三个工具——

- ``send_message``（``ctx.subagents.followup``）：向某个可续跑子代理投递后续消息；
- ``interrupt_agent``（``ctx.subagents.interrupt``）：中止子代理当前轮；
- ``list_agents``（``ctx.subagents.list_children`` / ``list_descendants``）：枚举
  调用方（或指定 root）的直接/全部子代理。

实现要点（零外部依赖，薄适配层）：
- 授权：``send_message`` / ``interrupt_agent`` 校验调用方 Agent 确为该子的直接父或
  祖先（经 ``ctx.subagents`` 内部把关，越权由运行时抛 ``UNAUTHORIZED``）；
- 错误翻译：运行时的 ``WorkflowError`` 折叠为工具结果文本（``is_error=True``），
  不向上抛；
- ``list_agents`` 输出中文渲染的行表（含 id / 标签 / provider / 状态 / 树深）。

注意：可续跑子代理由 ``ctx.subagents``（dsh_py 的 subagents seam）提供；本插件
只做模型侧适配，不持有子代理状态。
"""

from __future__ import annotations

from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.services.message import TextBlock


def _render_agents(entries: list[dict], recursive: bool) -> str:
    """把子代理条目渲染为可读中文表格文本。"""
    if not entries:
        return "（没有发现子代理）"
    header = "子代理清单（直接子" if not recursive else "子代理清单（完整子树"
    lines = [header + "）：", ""]
    if recursive:
        lines.append(f"{'DEPTH':<6}{'ID':<38}{'标签':<16}{'PROVIDER':<14}{'STATUS'}")
        lines.append("-" * 86)
        for e in entries:
            lines.append(
                f"{e.get('depth', 0):<6}{e['id'][:36]:<38}{(e.get('label') or '-')[:14]:<16}"
                f"{(e.get('provider') or '-')[:12]:<14}{e.get('status', 'unknown')}"
            )
    else:
        lines.append(f"{'ID':<38}{'标签':<16}{'PROVIDER':<14}{'STATUS'}")
        lines.append("-" * 82)
        for e in entries:
            lines.append(
                f"{e['id'][:36]:<38}{(e.get('label') or '-')[:14]:<16}"
                f"{(e.get('provider') or '-')[:12]:<14}{e.get('status', 'unknown')}"
            )
    return "\n".join(lines)


def _translate_error(exc: Exception) -> str:
    """把运行时错误折叠为工具结果文本。"""
    code = getattr(exc, "code", "") or ""
    text = str(exc)
    if code:
        return f"错误[{code}]：{text}"
    return f"错误：{text}"


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 3 个子代理控制工具（依赖 ``ctx.subagents`` 与 ``ctx.tools``）。"""
    config = config or {}

    async def send_message_handler(args: dict, exec: dict) -> tuple:
        agent_id = args.get("agentId", "")
        message = args.get("message", "")
        if not agent_id or not str(message).strip():
            return "错误：agentId 与 message 均必填", True
        parent = exec.get("agent")
        if parent is None:
            return "错误：send_message 需要一个持有会话的调用方父 Agent", True
        content = [TextBlock(str(message))]
        try:
            msg_id = await ctx.subagents.followup(parent, str(agent_id), content, {})
        except Exception as exc:  # noqa: BLE001 - 折叠为工具错误
            return _translate_error(exc), True
        return f"已向子代理 {agent_id} 投递后续消息（消息 id：{msg_id}）。", False

    async def interrupt_agent_handler(args: dict, exec: dict) -> tuple:
        agent_id = args.get("agentId", "")
        if not agent_id:
            return "错误：agentId 必填", True
        parent = exec.get("agent")
        if parent is None:
            return "错误：interrupt_agent 需要一个持有会话的调用方父 Agent", True
        try:
            ctx.subagents.interrupt(str(agent_id), parent)
        except Exception as exc:  # noqa: BLE001 - 折叠为工具错误
            return _translate_error(exc), True
        return f"已向子代理 {agent_id} 发出中断请求（current turn 将被中止）。", False

    async def list_agents_handler(args: dict, exec: dict) -> tuple:
        root = args.get("root")
        recursive = bool(args.get("recursive", False))
        parent = exec.get("agent")
        if root:
            root_id = str(root)
        elif parent is not None:
            root_id = str(getattr(parent, "id", parent))
        else:
            return "错误：list_agents 需要 root（指定会话 id）或一个持有会话的调用方 Agent", True
        try:
            if recursive:
                entries = await ctx.subagents.list_descendants(root_id)
            else:
                entries = await ctx.subagents.list_children(root_id)
        except Exception as exc:  # noqa: BLE001 - 折叠为工具错误
            return _translate_error(exc), True
        return _render_agents(entries, recursive), False

    ctx.tools.register("send_message", "向一个可续跑子代理投递后续消息（作为它的下一轮输入）。", {
        "type": "object",
        "properties": {
            "agentId": {"type": "string", "description": "目标可续跑子代理的会话 id。"},
            "message": {"type": "string", "description": "要投递的后续消息文本。"},
        },
        "required": ["agentId", "message"],
    }, send_message_handler)

    ctx.tools.register("interrupt_agent", "中止一个可续跑子代理的当前轮（已发出的中断请求不会被重排）。", {
        "type": "object",
        "properties": {
            "agentId": {"type": "string", "description": "目标可续跑子代理的会话 id。"},
        },
        "required": ["agentId"],
    }, interrupt_agent_handler)

    ctx.tools.register("list_agents", "枚举当前（或指定 root）的直接/全部子代理。", {
        "type": "object",
        "properties": {
            "root": {"type": "string", "description": "要枚举的父会话 id；省略则用调用方会话。"},
            "recursive": {"type": "boolean", "description": "true 时枚举完整子树（含 depth），否则仅直接子。"},
        },
        "required": [],
    }, list_agents_handler)


apply.provides = ["toolSubagentControl"]
apply.inject = ["tools", "subagents"]

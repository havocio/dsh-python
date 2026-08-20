"""terminal 工具（tool-terminal，对标 dsh 的 ``dsh-tool-terminal``）：把持久终端
会话暴露为模型可调用的 ``terminal`` 工具（start / send / close 三操作）。

本实现为持久 shell 会话（非 PTY）；会话跨调用保持工作目录与环境。参数通过
``operation`` 区分：``start``（cwd 可选）返回会话 id；``send``（session_id +
command）返回新输出；``close``（session_id）终止会话。
"""

from __future__ import annotations

from typing import Any

from dsh_py.core.context import AppContext

TERMINAL_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {"type": "string",
                      "description": "操作：start（启动会话）/ send（发送命令）/ close（关闭会话）"},
        "session_id": {"type": "string", "description": "会话 id（send/close 必填）"},
        "command": {"type": "string", "description": "要发送的命令（send 必填）"},
        "cwd": {"type": "string", "description": "工作目录（start 可选）"},
    },
    "required": ["operation"],
}


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``terminal`` 工具（经 ``ctx.terminals`` 管理持久会话）。"""

    async def terminal_handler(args: dict) -> str:
        operation = args.get("operation", "")
        if operation == "start":
            session = ctx.terminals.spawn(cwd=args.get("cwd"))
            return f"终端会话已启动: {session.id}（shell: {session.shell}）", False
        if operation == "send":
            session_id = args.get("session_id", "")
            command = args.get("command", "")
            if not session_id or not command.strip():
                return "错误：send 需要 session_id 与非空 command", True
            try:
                session = ctx.terminals.get(session_id)
                output = session.send(command)
                return f"$ {command}\n{output.rstrip() if output else '（无输出）'}", False
            except KeyError as exc:
                return f"错误：{exc}", True
            except RuntimeError as exc:
                return f"错误：{exc}", True
        if operation == "close":
            session_id = args.get("session_id", "")
            if not session_id:
                return "错误：close 需要 session_id", True
            ctx.terminals.close(session_id)
            return f"终端会话已关闭: {session_id}", False
        return "错误：operation 必须是 start / send / close 之一", True

    ctx.tools.register("terminal", "持久终端会话：start 启动 / send 发送命令 / close 关闭",
                       TERMINAL_SCHEMA, terminal_handler)


apply.provides = ["toolTerminal"]
apply.inject = ["tools", "terminals"]

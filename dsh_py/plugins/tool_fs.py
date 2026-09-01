"""文件系统工具套件（tool-fs，对标 dsh 的 ``dsh-tool-fs``）：把 ``ctx.fs`` 能力
暴露为模型可调用的 ``read`` / ``write`` / ``edit`` 工具。

本插件拥有 schema、校验、读取窗口与格式化，绝不拥有具体提供方。``read``
按行窗口渲染（行号 + 截断标记），``edit`` 做字面匹配替换（唯一性或 replace_all）。
"""

from __future__ import annotations

import json
from typing import Any

from dsh_py.core.context import AppContext
from dsh_py.services.fs import FS_SANDBOX_DENIED, FsError, READ_LIMIT, READ_MAX_BYTES, READ_MAX_LINE_LENGTH

# 工具参数 JSON Schema
READ_SCHEMA = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string", "description": "要读取的文件的绝对路径"},
        "offset": {"type": "integer", "description": "起始行号（从 1 起，缺省 1）"},
        "limit": {"type": "integer", "description": f"最大返回行数（缺省 {READ_LIMIT}）"},
    },
    "required": ["file_path"],
}

WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string", "description": "要写入的文件的绝对路径"},
        "content": {"type": "string", "description": "完整文件内容（覆盖写入）"},
    },
    "required": ["file_path", "content"],
}

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string", "description": "要编辑的文件的绝对路径"},
        "old_string": {"type": "string", "description": "要替换的旧文本（必须唯一匹配，或 replace_all）"},
        "new_string": {"type": "string", "description": "替换后的新文本"},
        "replace_all": {"type": "boolean", "description": "是否替换全部匹配（缺省 False）"},
    },
    "required": ["file_path", "old_string", "new_string"],
}


def _format_read(result: dict) -> str:
    """按行窗口渲染读取结果（``行号 | 内容``，超长行截断标记）。"""
    lines = result["lines"]
    if not lines:
        return f"（文件 {result['path']} 为空或无选定行）"
    body = "\n".join(f"{index} | {text}" for index, text in lines)
    if len(lines) < result["total_lines"]:
        body += f"\n（… 共 {result['total_lines']} 行，显示 {lines[0][0]}-{lines[-1][0]}）"
    if result["truncated"]:
        body += "\n（内容超出读取上限，已截断）"
    return body


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 read / write / edit 文件系统工具。"""
    config = config or {}
    read_limit = int(config.get("readLimit", READ_LIMIT))
    max_line_length = int(config.get("readMaxLineLength", READ_MAX_LINE_LENGTH))
    max_bytes = int(config.get("readMaxBytes", READ_MAX_BYTES))

    async def read_handler(args: dict, exec: dict) -> str:
        file_path = args.get("file_path", "")
        offset = int(args.get("offset", 1) or 1)
        limit = int(args.get("limit", read_limit) or read_limit)
        if offset < 1 or limit < 1:
            return "错误：offset 与 limit 必须为正整数"
        if limit > read_limit:
            return f"错误：limit 不能超过 {read_limit}", True
        try:
            result = ctx.fs.read_text(file_path, offset=offset, limit=limit,
                                      max_line_length=max_line_length, max_bytes=max_bytes,
                                      actor=exec.get("agent"))
            return _format_read(result), False
        except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
            return f"错误：{exc}"

    async def write_handler(args: dict, exec: dict) -> str:
        file_path = args.get("file_path", "")
        content = args.get("content", "")
        try:
            result = ctx.fs.write_text(file_path, content, actor=exec.get("agent"))
            return f"已写入 {result['path']}（{result['bytes']} 字节）", False
        except (PermissionError, OSError, ValueError, FsError) as exc:
            return f"错误：{exc}"

    async def edit_handler(args: dict, exec: dict) -> str:
        file_path = args.get("file_path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        replace_all = bool(args.get("replace_all", False))
        try:
            result = ctx.fs.edit_text(file_path, old_string, new_string,
                                      replace_all=replace_all, actor=exec.get("agent"))
            if not result["replaced"]:
                return f"未找到匹配的旧文本（{result['count']} 处）：{file_path}", False
            return f"已替换 {result['count']} 处 → {file_path}（{result['bytes']} 字节）", False
        except (FileNotFoundError, ValueError, PermissionError, OSError, FsError) as exc:
            return f"错误：{exc}"

    ctx.tools.register("read", "按行窗口读取 UTF-8 文件（行号 + 截断标记）", READ_SCHEMA, read_handler)
    ctx.tools.register("write", "原子写入（覆盖）一个 UTF-8 文件", WRITE_SCHEMA, write_handler)
    ctx.tools.register("edit", "对文件做字面匹配替换（唯一匹配或 replace_all）", EDIT_SCHEMA, edit_handler)


apply.provides = ["toolFs"]
apply.inject = ["tools", "fs"]

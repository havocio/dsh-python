"""模型侧字符串替换编辑器工具（tool-str-replace-editor，对标 dsh 的
``dsh-tool-str-replace-editor``）：``view`` / ``create`` / ``str_replace`` / ``insert``
四个命令，基于 ``ctx.fs`` 本地后端（零外部依赖）。

本实现保留 dsh 的命令语义、参数校验、行号视图、截断标记与目录列举约定，但简化了
沙箱策略（dsh_py 的 ``fs`` 后端无 ``FsTarget``/``sandboxPolicy`` 维度，路径约束由
``ctx.fs.resolve`` 的沙箱根承担）。``view`` 目录时列出最多 2 层、剔除隐藏项、
``node_modules`` 与 ``__pycache__``；``create`` 不能覆盖已存在文件；``str_replace``
要求 ``old_str`` 全文件唯一；``insert`` 在 ``insert_line`` 之后插入。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from dsh_py.core.context import AppContext

TRUNCATED_MESSAGE = (
    "<response clipped><NOTE>为节省上下文仅展示部分文件内容。请先用 grep 搜索行号，"
    "再对目标位置发起工具调用。</NOTE>"
)

DEFAULT_DESCRIPTION = (
    "用于查看、创建和编辑文件的自定义编辑工具\n"
    "* 状态在多次命令调用与对话间持久\n"
    "* 若 `path` 是文件，`view` 显示 `cat -n` 的结果；若 `path` 是目录，`view` 列出最多 2 层的非隐藏文件与目录\n"
    "* `create` 命令在 `path` 已作为文件存在时不可用\n"
    "* 若某命令输出过长，将被截断并标记 `<response clipped>`\n\n"
    "使用 `str_replace` 命令的注意事项：\n"
    "* `old_str` 参数应精确匹配原文件中的一行或多行连续文本，注意空白字符\n"
    "* 若 `old_str` 在文件中不唯一，替换不会执行；请在 `old_str` 中包含足够上下文使其唯一\n"
    "* `new_str` 参数应包含用来替换 `old_str` 的编辑后文本\n"
).strip()

MAX_OUTPUT_CHARS = 16_000


def maybe_truncate(content: str, max_output_chars: int) -> str:
    """超长输出截断并附说明（对标 dsh 的 maybeTruncate）。"""
    if len(content) <= max_output_chars:
        return content
    return content[:max_output_chars] + TRUNCATED_MESSAGE


def _match_offsets(content: str, search: str) -> list:
    """返回 ``search`` 在 ``content`` 中所有出现处的字符偏移（对标 dsh 的 matchOffsets）。"""
    offsets: list = []
    offset = 0
    while True:
        found = content.find(search, offset)
        if found < 0:
            return offsets
        offsets.append(found)
        offset = found + len(search)
    return offsets


def _line_numbers_at(content: str, offsets: list) -> list:
    """把字符偏移换算为 1 起的行号（对标 dsh 的 lineNumbersAt）。"""
    line = 1
    cursor = 0
    out: list = []
    for off in offsets:
        while cursor < off:
            if content[cursor] == "\n":
                line += 1
            cursor += 1
        out.append(line)
    return out


# --------------------------------------------------------------------------- #
# 路径与存在性
# --------------------------------------------------------------------------- #
def _resolve_target(ctx: AppContext, path: str) -> str:
    """校验绝对路径并解析（尊重 fs 沙箱根）。"""
    if path.strip() == "":
        raise ValueError("path 必须是非空字符串")
    if not os.path.isabs(path):
        raise ValueError(f"路径 {path} 不是绝对路径，应以 `/` 开头。是否想写 /{path}？")
    return ctx.fs.resolve(path)


def _stat_existing(ctx: AppContext, absolute: str, command: str) -> dict:
    """取文件元信息；不存在 / 非文件（且非 view）按 dsh 语义报错。"""
    try:
        info = ctx.fs.info(absolute)
    except FileNotFoundError as exc:
        raise ValueError(f"路径 {absolute} 不存在，请提供有效路径。") from exc
    if info["type"] == "directory" and command != "view":
        raise ValueError(f"路径 {absolute} 是目录，只有 `view` 命令可用于目录")
    return info


def _required(value: Optional[str], parameter: str, command: str, allow_empty: bool = True) -> str:
    if value is None:
        raise ValueError(f"命令 {command} 需要参数 `{parameter}`")
    if not allow_empty and value == "":
        raise ValueError(f"命令 {command} 的参数 `{parameter}` 不能为空")
    return value


# --------------------------------------------------------------------------- #
# view
# --------------------------------------------------------------------------- #
def _format_file_view(path: str, content: str, max_output_chars: int,
                      view_range: Optional[list] = None) -> str:
    all_lines = content.split("\n")
    lines = all_lines
    initial_line = 1
    final_line: Optional[int] = None
    prompt = f"这是 {path} 的内容（带行号，共 {len(all_lines)} 行）"
    if view_range is not None:
        if (len(view_range) != 2 or view_range[0] is None or view_range[1] is None
                or not all(isinstance(v, int) for v in view_range)):
            raise ValueError("`view_range` 无效，应为两个整数列表")
        initial_line, final_line = view_range[0], view_range[1]
        if initial_line < 1 or initial_line > len(all_lines):
            raise ValueError(
                f"`view_range` 无效：[{initial_line}, {final_line}]，第一个元素应在 [1, {len(all_lines)}] 内")
        if final_line > len(all_lines):
            raise ValueError(
                f"`view_range` 无效：[{initial_line}, {final_line}]，第二个元素应不大于文件行数 {len(all_lines)}")
        if final_line != -1 and final_line < initial_line:
            raise ValueError(
                f"`view_range` 无效：[{initial_line}, {final_line}]，第二个元素应不小于第一个")
        lines = all_lines[initial_line - 1:] if final_line == -1 else all_lines[initial_line - 1:final_line]
        prompt += f"，view_range=[{initial_line}, {final_line}]"
    numbered = "\n".join(
        f"{str(initial_line + i).rjust(6)}  {line}" for i, line in enumerate(lines))
    return maybe_truncate(f"{prompt}:\n{numbered}\n", max_output_chars)


def _list_directory(ctx: AppContext, absolute: str, max_output_chars: int) -> str:
    def visit(dir_abs: str, depth: int) -> list:
        rows: list = []
        try:
            entries = ctx.fs.list(dir_abs)
        except NotADirectoryError:
            return rows
        for entry in entries:
            name = entry["name"]
            if name.startswith(".") or name in ("node_modules", "__pycache__"):
                continue
            etype = "d" if entry["type"] == "directory" else "f"
            child_path = os.path.join(dir_abs, name)
            rows.append(f"{etype}\t{child_path}")
            if entry["type"] == "directory" and depth < 2:
                rows.extend(visit(child_path, depth + 1))
        return rows

    rows = [f"d\t{absolute}", *visit(absolute, 1)]
    rows.sort(key=lambda r: r[r.index("\t") + 1:])
    listing = maybe_truncate("\n".join(rows) + "\n", max_output_chars)
    return (f"这是 {absolute} 下最多 2 层内的文件与目录（剔除隐藏项、node_modules 与 "
            f"Python 缓存目录）：\n{listing}\n")


def _view_path(ctx: AppContext, path: str, view_range: Optional[list],
               max_output_chars: int) -> str:
    absolute = _resolve_target(ctx, path)
    info = _stat_existing(ctx, absolute, "view")
    if info["type"] == "directory":
        if view_range is not None:
            raise ValueError("`path` 指向目录时不允许使用 `view_range` 参数")
        return _list_directory(ctx, absolute, max_output_chars)
    with open(absolute, "r", encoding="utf-8") as f:
        content = f.read()
    ctx.emit("fs/observed", {"path": absolute, "present": True, "version": 1})
    return _format_file_view(absolute, content, max_output_chars, view_range)


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #
def _create_file(ctx: AppContext, path: str, file_text: Optional[str]) -> str:
    content = _required(file_text, "file_text", "create")
    absolute = _resolve_target(ctx, path)
    if ctx.fs.exists(absolute):
        raise ValueError(f"文件已存在于 {absolute}，不能用 `create` 命令覆盖")
    ctx.fs.write_text(absolute, content)
    return f"新文件已成功创建于：{absolute}"


# --------------------------------------------------------------------------- #
# str_replace
# --------------------------------------------------------------------------- #
def _replace_in_file(ctx: AppContext, path: str, old_str: Optional[str],
                     new_str: Optional[str]) -> str:
    old_value = _required(old_str, "old_str", "str_replace", allow_empty=False)
    new_value = new_str or ""
    absolute = _resolve_target(ctx, path)
    _stat_existing(ctx, absolute, "str_replace")
    with open(absolute, "r", encoding="utf-8") as f:
        before = f.read()
    offsets = _match_offsets(before, old_value)
    if not offsets:
        raise ValueError(f"未执行替换：`old_str` 未原样出现在 {absolute} 中")
    if len(offsets) > 1:
        lines = _line_numbers_at(before, offsets)
        raise ValueError(
            f"未执行替换：`old_str` 在行 [{', '.join(map(str, lines))}] 出现多次，请确保其唯一")
    offset = offsets[0]
    after = before[:offset] + new_value + before[offset + len(old_value):]
    ctx.fs.write_text(absolute, after)
    return f"文件 {absolute} 已成功编辑。"


# --------------------------------------------------------------------------- #
# insert
# --------------------------------------------------------------------------- #
def _insert_in_file(ctx: AppContext, path: str, insert_line: Optional[int],
                    new_str: Optional[str]) -> str:
    if insert_line is None or not isinstance(insert_line, int):
        raise ValueError("命令 insert 需要参数 `insert_line`（整数）")
    value = _required(new_str, "new_str", "insert")
    absolute = _resolve_target(ctx, path)
    _stat_existing(ctx, absolute, "insert")
    with open(absolute, "r", encoding="utf-8") as f:
        before = f.read()
    lines = before.split("\n")
    if insert_line < 0 or insert_line > len(lines):
        raise ValueError(
            f"`insert_line` 参数无效：{insert_line}，应在文件行数范围 [0, {len(lines)}] 内")
    after = "\n".join([*lines[:insert_line], *value.split("\n"), *lines[insert_line:]])
    ctx.fs.write_text(absolute, after)
    return f"文件 {absolute} 已成功编辑。"


# --------------------------------------------------------------------------- #
# 处理器
# --------------------------------------------------------------------------- #
async def _handler(args: dict, exec: dict, ctx: AppContext, max_output_chars: int) -> tuple[str, bool]:
    command = args.get("command")
    path = args.get("path")
    try:
        if command == "view":
            text = _view_path(ctx, path, args.get("view_range"), max_output_chars)
        elif command == "create":
            text = _create_file(ctx, path, args.get("file_text"))
        elif command == "str_replace":
            text = _replace_in_file(ctx, path, args.get("old_str"), args.get("new_str"))
        elif command == "insert":
            text = _insert_in_file(ctx, path, args.get("insert_line"), args.get("new_str"))
        else:
            return f"错误：未知命令 `{command}`（应为 view/create/str_replace/insert）", True
        return text, False
    except (ValueError, FileNotFoundError, IsADirectoryError, PermissionError, OSError) as exc:
        return f"错误：{exc}", True


# --------------------------------------------------------------------------- #
# 插件入口
# --------------------------------------------------------------------------- #
def apply(ctx: AppContext, config: Any = None) -> None:
    """注册 ``str_replace_editor`` 工具。"""
    config = config or {}
    max_output_chars = int(config.get("maxOutputChars", MAX_OUTPUT_CHARS))
    description = config.get("description") or DEFAULT_DESCRIPTION
    if not isinstance(max_output_chars, int) or isinstance(max_output_chars, bool) or max_output_chars <= 0:
        raise ValueError("tool-str-replace-editor: maxOutputChars 必须是正整数")
    if description.strip() == "":
        raise ValueError("tool-str-replace-editor: description 不能为空")

    SCHEMA = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要运行的命令。允许选项：`view`、`create`、`str_replace`、`insert`。",
                "enum": ["view", "create", "str_replace", "insert"],
            },
            "path": {"type": "string", "description": "文件或目录的绝对路径，例如 `/repo/file.py` 或 `/repo`。"},
            "file_text": {"type": "string", "description": "`create` 命令所需参数，新建文件的内容。"},
            "insert_line": {"type": "integer", "description": "`insert` 命令所需参数。`new_str` 将插入到 `path` 第 `insert_line` 行之后。"},
            "new_str": {"type": "string", "description": "`str_replace` 的可选新字符串（不填则不新增）；`insert` 的必填插入内容。"},
            "old_str": {"type": "string", "description": "`str_replace` 必填参数，待替换的原字符串。"},
            "view_range": {
                "type": "array", "items": {"type": "integer"},
                "description": "`view` 针对文件的可选行范围，如 [11,12] 显示第 11、12 行；[start,-1] 显示到文末。1 起。",
            },
        },
        "required": ["command", "path"],
    }

    async def handler(arguments: dict, exec: dict) -> tuple[str, bool]:
        return await _handler(arguments, exec, ctx, max_output_chars)

    ctx.tools.register(
        "str_replace_editor",
        description,
        SCHEMA,
        handler,
    )


apply.provides = ["toolStrReplaceEditor"]
apply.inject = ["tools", "fs"]

"""lsp 工具（tool-lsp，对标 dsh 的 ``@deepseek-ai/dsh-tool-lsp``）。

模型侧 ``lsp`` 工具：四个操作（goToDefinition / findReferences /
goToImplementation / hover）。模型以「基于一」的 UTF-16 游标提供坐标，本工具转为
seam 的「零基」坐标后调用 ``ctx.lsp``，再把结果渲染为文本。
"""

from __future__ import annotations

from typing import Any

from dsh_py.core.context import AppContext

LSP_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": ["goToDefinition", "findReferences", "goToImplementation", "hover"],
            "description": "语义查询类型",
        },
        "filePath": {"type": "string", "description": "源文件路径（相对或绝对）"},
        "line": {"type": "integer", "description": "行号（从 1 开始）"},
        "character": {"type": "integer", "description": "列号（从 1 开始的 UTF-16 偏移）"},
        "workspaceRoot": {"type": "string", "description": "工作区根目录"},
    },
    "required": ["operation", "filePath", "line", "character", "workspaceRoot"],
}


def _render(result: Any) -> str:
    """把规范化结果渲染为模型可读文本。"""
    if result.kind == "hover":
        hover = result.hover
        if hover is None:
            return "（该位置无 hover 信息）"
        return hover.contents
    locations = result.locations or []
    if not locations:
        return "（未找到任何位置）"
    lines = []
    for loc in locations:
        rng = loc.range
        lines.append(f"{loc.uri}:{rng.start.line + 1}:{rng.start.character + 1}")
    return "\n".join(lines)


def apply(ctx: AppContext, config: Any = None) -> None:
    """注册 ``lsp`` 工具。"""

    async def lsp_handler(args: dict, exec: dict) -> tuple:
        operation = args.get("operation")
        file_path = args.get("filePath", "")
        line = args.get("line")
        character = args.get("character")
        workspace_root = args.get("workspaceRoot", "")
        if operation not in ("goToDefinition", "findReferences", "goToImplementation", "hover"):
            return f"错误：未知的 operation: {operation}", True
        try:
            result = ctx.lsp.query({
                "operation": operation,
                "filePath": file_path,
                "position": {"line": max(0, int(line) - 1), "character": max(0, int(character) - 1)},
                "workspaceRoot": workspace_root,
            })
            return _render(result), False
        except Exception as exc:  # noqa: BLE001 - seam 错误回流为文本
            return f"错误：{exc}", True

    ctx.tools.register(
        "lsp",
        "在代码中执行语义导航（goToDefinition/findReferences/goToImplementation/hover）；"
        "坐标为从 1 开始的行列号",
        LSP_TOOL_SCHEMA,
        lsp_handler,
    )


apply.provides = ["toolLsp"]
apply.inject = ["tools", "lsp"]

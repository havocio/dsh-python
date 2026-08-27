"""lsp 能力 seam（对标 dsh 的 ``@deepseek-ai/dsh-lsp``）。

LSP 能力 seam：规范化请求、provider、结果契约。位置/范围为零基 UTF-16（与协议一致）；
模型侧工具拥有「基于一的游标」约定。seam 仅暴露四个语义操作与 provider 注册，无协议逃逸。

本文件定义 ``LspService`` / ``LspProviderId`` 品牌 / ``LspError`` 与类型词汇；
通用 stdio 后端在 ``services/lsp_stdio.py``；模型侧工具在 ``plugins/tool_lsp.py``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: LSP 四类语义查询；闭包联合——新增操作是跨 seam/provider/工具的编译期强制变更。
LspOperation = str  # 'goToDefinition' | 'findReferences' | 'goToImplementation' | 'hover'


@dataclass
class LspPosition:
    """零基 UTF-16 光标坐标。"""

    line: int
    character: int


@dataclass
class LspRange:
    """零基 UTF-16 半开范围 ``[start, end)``。"""

    start: LspPosition
    end: LspPosition


@dataclass
class LspQueryRequest:
    """调用方的规范化查询；每个字段必填。"""

    operation: str  # LspOperation
    filePath: str
    position: LspPosition
    workspaceRoot: str


@dataclass
class LspProviderQuery(LspQueryRequest):
    """provider 收到的请求：调用方请求 + 由扩展映射派生的 ``languageId``。"""

    languageId: str


@dataclass
class LspLocation:
    """一个解析位置：文档 URI + 范围。"""

    uri: str
    range: LspRange


@dataclass
class LspHover:
    """规范化 hover 内容；无 hover 时为 ``None``。"""

    contents: str
    range: LspRange | None = None


#: 闭包结果联合：导航操作 → ``locations``；hover → ``hover`` 或 ``None``。
class LspQueryResult:
    """结果联合：``kind`` 区分 ``locations`` / ``hover``。"""

    kind: str  # 'locations' | 'hover'
    locations: list[LspLocation] | None = None
    resolvedWorkspaceUri: str | None = None
    hover: LspHover | None = None


#: provider 注册时保留的、不可信的模式字符串校验集。
LSP_UNAVAILABLE = "LSP_UNAVAILABLE"


class LspError(Exception):
    """LSP seam 的结构化错误；``code`` 经 ``tool/result`` 传出。"""

    def __init__(self, message: str, code: str = LSP_UNAVAILABLE) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def LspProviderId(id: str) -> str:  # noqa: N802 - 品牌工厂，保持原签名
    """把字符串品牌化为 ``LspProviderId``；注册时由注册表拒绝空 id。"""
    return id


class LspProvider:
    """语言服务器后端（注册于 ``ctx.lsp``）。"""

    id: str
    extensionToLanguage: dict[str, str]

    async def query(self, request: LspProviderQuery, signal: Any | None = None) -> LspQueryResult:
        raise NotImplementedError


class LspService:
    """LSP 能力 seam（``ctx.lsp``）：provider 注册/选择 + 规范化查询。"""

    def __init__(self) -> None:
        self._providers: dict[str, LspProvider] = {}
        self._ext_index: dict[str, LspProvider] = {}

    def registerProvider(self, provider: LspProvider) -> Any:
        """注册 provider，原子保留其 id 与所有规范化扩展；冲突或非法抛 ``LspError``。"""
        if not provider.id or not provider.id.strip():
            raise LspError("lsp provider id must be a non-empty string", "LSP_REGISTRATION")
        for ext in provider.extensionToLanguage:
            if ext in self._ext_index:
                raise LspError(f"extension already registered: {ext}", "LSP_REGISTRATION")
            self._ext_index[ext] = provider
        self._providers[provider.id] = provider
        return lambda: self._unregister(provider.id)

    def _unregister(self, provider_id: str) -> None:
        provider = self._providers.pop(provider_id, None)
        if provider is None:
            return
        for ext, p in list(self._ext_index.items()):
            if p.id == provider_id:
                del self._ext_index[ext]

    def query(self, request: LspQueryRequest, signal: Any | None = None) -> LspQueryResult:
        """按文件扩展名选择 provider 并运行一次查询。"""
        ext = _ext_of(request.filePath)
        provider = self._ext_index.get(ext)
        if provider is None:
            raise LspError(f"no LSP provider for extension {ext!r}", LSP_UNAVAILABLE)
        language_id = provider.extensionToLanguage.get(ext, "")
        provider_query = LspProviderQuery(
            operation=request.operation,
            filePath=request.filePath,
            position=request.position,
            workspaceRoot=request.workspaceRoot,
            languageId=language_id,
        )
        return provider.query(provider_query, signal)


def _ext_of(path: str) -> str:
    """取文件的扩展名（小写、含前导点）。"""
    dot = path.rfind(".")
    if dot == -1:
        return ""
    return path[dot:].lower()


__all__ = [
    "LspOperation", "LspPosition", "LspRange", "LspQueryRequest", "LspProviderQuery",
    "LspLocation", "LspHover", "LspQueryResult", "LspError", "LspProviderId",
    "LspProvider", "LspService", "LSP_UNAVAILABLE",
]

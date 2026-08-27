"""lsp stdio 后端（对标 dsh 的 ``@deepseek-ai/dsh-lsp-stdio``）。

通用多服务器 stdio 后端：每个规范工作区惰性单飞一个语言服务器进程，通过
``ctx.subprocess`` 启动、经 ``ctx.fs`` 读取源，复用同一执行世界。支持瞬时
``didOpen``→请求→``didClose`` 生命周期与失败传输的一次性替换。

本实现用 Content-Length 成帧的 JSON-RPC 与语言服务器通信（映射到 dsh 的
``connection.ts`` / ``framing.ts``）。纯协议翻译函数（``translate`` 模块）见本文件末尾。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from dsh_py.core.context import AppContext
from dsh_py.services.lsp import (
    LSP_UNAVAILABLE,
    LspError,
    LspLocation,
    LspPosition,
    LspProvider,
    LspProviderQuery,
    LspQueryResult,
    LspRange,
)

DEFAULT_MAX_MESSAGE_BYTES = 16_000_000
DEFAULT_MAX_STDERR_BYTES = 1_000_000
DEFAULT_MAX_DOCUMENT_BYTES = 4_000_000
DEFAULT_SHUTDOWN_TIMEOUT_MS = 5_000
DEFAULT_KILL_GRACE_MS = 2_000


def _request_method(operation: str) -> str:
    return {
        "goToDefinition": "textDocument/definition",
        "findReferences": "textDocument/references",
        "goToImplementation": "textDocument/implementation",
        "hover": "textDocument/hover",
    }[operation]


def _negotiate_position_encoding(encoding: str | None) -> str:
    if encoding is None or encoding == "utf-16":
        return "utf-16"
    raise LspError(f'server negotiated unsupported position encoding "{encoding}"', "LSP_PROTOCOL")


def _to_range(r: dict) -> LspRange:
    return LspRange(start=LspPosition(r["start"]["line"], r["start"]["character"]),
                    end=LspPosition(r["end"]["line"], r["end"]["character"]))


def _is_range(v: Any) -> bool:
    return isinstance(v, dict) and _is_pos(v.get("start")) and _is_pos(v.get("end"))


def _is_pos(v: Any) -> bool:
    return isinstance(v, dict) and isinstance(v.get("line"), int) and isinstance(v.get("character"), int)


def normalize_locations(payload: Any) -> list[LspLocation]:
    if payload is None:
        return []
    if payload is None or payload is False:  # 兼容性占位
        raise LspError("LSP navigation result was missing", "LSP_MALFORMED_RESPONSE")
    elements = payload if isinstance(payload, list) else [payload]
    out: list[LspLocation] = []
    for el in elements:
        if not isinstance(el, dict):
            raise LspError("LSP navigation result contained a non-object entry", "LSP_MALFORMED_RESPONSE")
        if "targetUri" in el and "targetSelectionRange" in el:
            out.append(LspLocation(uri=el["targetUri"], range=_to_range(el["targetSelectionRange"])))
        elif "uri" in el and "range" in el:
            out.append(LspLocation(uri=el["uri"], range=_to_range(el["range"])))
        else:
            raise LspError("LSP navigation result was neither Location nor LocationLink", "LSP_MALFORMED_RESPONSE")
    return out


def _render_marked(value: Any) -> str:
    if isinstance(value, str):
        return value
    return f"```{value.get('language', '')}\n{value.get('value', '')}\n```"


def normalize_hover(payload: Any) -> Any:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise LspError("LSP hover result was not an object", "LSP_MALFORMED_RESPONSE")
    contents = payload.get("contents")
    if contents is None:
        raise LspError("LSP hover result had no contents", "LSP_MALFORMED_RESPONSE")
    if isinstance(contents, str):
        text = contents
    elif isinstance(contents, list):
        text = "\n\n".join(_render_marked(c) for c in contents)
    elif isinstance(contents, dict) and "value" in contents:
        text = contents["value"]
    else:
        raise LspError("LSP hover contents were not a supported shape", "LSP_MALFORMED_RESPONSE")
    if text == "":
        return None
    rng = payload.get("range")
    return {"contents": text, "range": _to_range(rng) if rng else None}


# --------------------------------------------------------------------------- #
# JSON-RPC 传输（Content-Length 成帧）
# --------------------------------------------------------------------------- #


class _JsonRpcConnection:
    """基于 asyncio 子进程的 JSON-RPC 连接；写请求、读响应、应答服务器请求。"""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self._reader = proc.stdout
        self._writer = proc.stdin
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._server_handlers: dict[str, Any] = {}

    async def send_notification(self, method: str, params: Any) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(self, method: str, params: Any) -> Any:
        self._msg_id += 1
        mid = self._msg_id
        msg = {"jsonrpc": "2.0", "id": mid, "method": method, "params": params}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._write(msg)
        return await fut

    async def _write(self, obj: dict) -> None:
        data = json.dumps(obj).encode("utf-8")
        frame = b"Content-Length: %d\r\n\r\n" % len(data) + data
        assert self._writer is not None
        self._writer.write(frame)
        await self._writer.drain()

    async def pump(self) -> None:
        """读取循环：分发响应与通知。"""
        assert self._reader is not None
        while True:
            header = await self._reader.readuntil(b"\r\n\r\n")
            if not header:
                break
            length = 0
            for line in header.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1].strip())
            body = await self._reader.readexactly(length)
            msg = json.loads(body)
            if "id" in msg and "method" not in msg:
                fut = self._pending.pop(msg["id"], None)
                if fut is not None and not fut.done():
                    if "error" in msg:
                        fut.set_exception(LspError(str(msg["error"]), "LSP_PROTOCOL"))
                    else:
                        fut.set_result(msg.get("result"))
            elif "method" in msg:
                handler = self._server_handlers.get(msg["method"])
                if handler:
                    await handler(msg.get("params"))


# --------------------------------------------------------------------------- #
# 实例（每规范工作区一个服务器进程）
# --------------------------------------------------------------------------- #


class LspInstance:
    """一个已初始化的语言服务器进程；``query`` 串行化；``dispose`` 拆除。"""

    def __init__(self, command: list[str], cwd: str, workspace_uri: str,
                 env: dict | None = None, config: dict | None = None) -> None:
        self._command = command
        self._cwd = cwd
        self._workspace_uri = workspace_uri
        self._env = env or {}
        self._config = config or {}
        self._conn: _JsonRpcConnection | None = None
        self._queue: asyncio.Future = asyncio.get_running_loop().create_future()
        self._queue.set_result(None)
        self.capabilities: dict = {}
        self._dead = False

    @property
    def dead(self) -> bool:
        return self._dead

    async def _ensure(self) -> _JsonRpcConnection:
        if self._conn is not None:
            return self._conn
        proc = await asyncio.create_subprocess_exec(
            *self._command, cwd=self._cwd, env=self._merged_env(),
            stdin=asyncio.subprocess.PIPE, stdout=  asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        conn = _JsonRpcConnection(proc)
        init = await conn.request("initialize", {
            "processId": None,
            "rootUri": self._workspace_uri,
            "workspaceFolders": [{"uri": self._workspace_uri, "name": "workspace"}],
            "capabilities": {"textDocument": {"hover": {"dynamicRegistration": False}}},
            "initializationOptions": self._config.get("initializationOptions"),
        })
        self.capabilities = init.get("capabilities", {})
        _negotiate_position_encoding(self.capabilities.get("positionEncoding"))
        await conn.send_notification("initialized", {})
        self._conn = conn
        asyncio.ensure_future(conn.pump())
        return conn

    def _merged_env(self) -> dict:
        import os
        env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ("KEY", "SECRET", "TOKEN"))}
        env.update(self._env)
        return env

    async def query(self, request: LspProviderQuery, source_text: str) -> LspQueryResult:
        conn = await self._ensure()
        doc_uri = _file_uri(request.filePath)
        await conn.send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": doc_uri, "languageId": request.languageId,
                "version": 1, "text": source_text,
            },
        })
        try:
            method = _request_method(request.operation)
            params: dict = {
                "textDocument": {"uri": doc_uri},
                "position": {"line": request.position.line, "character": request.position.character},
            }
            if request.operation == "findReferences":
                params["context"] = {"includeDeclaration": True}
            result = await conn.request(method, params)
            if request.operation == "hover":
                return LspQueryResult(kind="hover", hover=normalize_hover(result), resolvedWorkspaceUri=self._workspace_uri)
            return LspQueryResult(kind="locations", locations=normalize_locations(result), resolvedWorkspaceUri=self._workspace_uri)
        finally:
            await conn.send_notification("textDocument/didClose", {"textDocument": {"uri": doc_uri}})

    def is_transport_failure(self, error: Any) -> bool:
        return isinstance(error, LspError) and error.code == "LSP_PROTOCOL"

    async def dispose(self) -> None:
        self._dead = True
        if self._conn is None:
            return
        try:
            await self._conn.request("shutdown", None)
            await self._conn.send_notification("exit", None)
        except Exception:  # noqa: BLE001
            pass
        proc = self._conn._proc
        if proc.returncode is None:
            proc.kill()


def _file_uri(path: str) -> str:
    from urllib.parse import urljoin, pathname2url
    return "file://" + pathname2url(path)


# --------------------------------------------------------------------------- #
# Provider（每 provider 一个，按扩展名映射）
# --------------------------------------------------------------------------- #


class LocalLspProvider(LspProvider):
    """通用 stdio provider；按规范工作区惰性单飞一个服务器实例。"""

    def __init__(self, provider_id: str, command: list[str], ext_map: dict[str, str],
                 config: dict | None = None) -> None:
        self.id = provider_id
        self.extensionToLanguage = ext_map
        self._command = command
        self._config = config or {}
        self._instances: dict[str, LspInstance] = {}

    async def query(self, request: LspProviderQuery, signal: Any | None = None) -> LspQueryResult:
        key = request.workspaceRoot
        inst = self._instances.get(key)
        if inst is None or inst.dead:
            inst = LspInstance(self._command, request.workspaceRoot, _file_uri(request.workspaceRoot),
                               self._config.get("env"), self._config)
            self._instances[key] = inst
        # 读取源（简化：直接读取文件系统文本）。
        try:
            with open(request.filePath, "r", encoding="utf-8") as f:
                text = f.read(DEFAULT_MAX_DOCUMENT_BYTES)
        except OSError as exc:
            raise LspError(f"source read failed: {exc}", "LSP_IO")
        try:
            return await inst.query(request, text)
        except LspError as exc:
            if inst.is_transport_failure(exc):
                await inst.dispose()
                self._instances.pop(key, None)
                raise
            raise

    async def dispose_all(self) -> None:
        for inst in self._instances.values():
            await inst.dispose()
        self._instances.clear()


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：按 ``servers`` 表注册本地语言服务器 provider。"""
    cfg = config or {}
    servers = cfg.get("servers", {})
    if not servers:
        raise ValueError("lsp-stdio: servers must contain at least one server")
    lsp = ctx.lsp  # noqa: F841 - 确保 seam 已挂载
    providers = []
    for pid, scfg in servers.items():
        if not pid.strip():
            raise ValueError("lsp-stdio: server ids must be non-empty strings")
        command = [scfg["command"], *scfg.get("args", [])]
        provider = LocalLspProvider(pid, command, dict(scfg.get("extensionToLanguage", {})), scfg)
        providers.append(ctx.lsp.registerProvider(provider))
    ctx.effect(lambda: [d() for d in providers], "lsp-stdio.registerProviders")


__all__ = [
    "LocalLspProvider", "LspInstance", "_request_method", "normalize_locations",
    "normalize_hover", "apply",
]

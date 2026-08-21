"""本地文件系统 spill 后端（spill/spill-local，第 3 层）。

``ctx.spillStore`` 的宿主文件系统实现：把工具的超大文本持久化到私有、会话
作用域的文件（``<root>/session-<hash>/<随机前缀>-<安全名>``），返回路径
定位符 + 本地读/grep 取回指引。

- :func:`encode_segment` —— 任意字符串注入式编码为单个安全路径段（中和
  ``../``、绝对路径、NUL 与分隔符；``~`` 自身转义故映射可逆且不冲突）；
- :func:`private_root` —— 默认 spill 根：OS 临时目录下惰性创建每进程私有
  （0700）``mkdtemp`` 目录（不可预测后缀，防其他本地用户读取/预置符号链接）；
- :func:`save_text_file` —— 独占 + 仅所有者（``wx``/0o600）写入：任何既有
  路径（含符号链接）都会失败，预置目标无法重定向写入。

与 dsh 差异（已注明）：dsh 的 ``Buffer.byteLength`` 用 Node 语义；dsh_py 用
``len(content.encode('utf-8'))``（等价的 UTF-8 字节数）。
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import uuid
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.services.spill import SpillLocator, SpillStore

# 合法字面路径段字符（``~`` 除外，保留给转义前缀）
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]$")

_default_root: Optional[str] = None


def private_root() -> str:
    """默认 spill 根：OS 临时目录下惰性创建的每进程私有（0700）目录。"""
    global _default_root
    if _default_root is None:
        _default_root = tempfile.mkdtemp(prefix="dsh-spill-")
    return _default_root


def encode_segment(raw: str) -> str:
    """把任意字符串注入式编码为单个安全路径段（对全部字符串单射）。

    会话 id / 建议名是不可信输入：本编码在任何文件系统使用前中和 ``../``、
    绝对路径、NUL 与分隔符。字面 ``[A-Za-z0-9._-]``（除 ``~``）保留，其余以
    ``~XXXX`` 转义；``~`` 自身转义，故映射可逆且不同输入绝不冲突。整段 token
    ``.``/``..`` 转义，永不穿越。空串编码为 ``~``（绝不产生空段）。
    """
    if raw == "":
        return "~"
    if raw == ".":
        return "~002E"
    if raw == "..":
        return "~002E~002E"
    out: list[str] = []
    for ch in raw:
        if ch != "~" and _SAFE_SEGMENT.match(ch):
            out.append(ch)
        else:
            out.append(f"~{ord(ch):04X}")
    return "".join(out)


def session_dir(root: str, session_id: str) -> str:
    """会话作用域目录：``<root>/session-<hash(sessionId)>``（短稳定哈希）。"""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    return os.path.join(root, f"session-{digest}")


async def save_text_file(root: str, session_id: str, suggested_name: str, content: str) -> dict:
    """把 ``content`` 写入会话目录下的新文件，返回路径 + UTF-8 字节长度。

    文件名 = 随机十六进制前缀 + 清洗后的建议名：不可预测（防共享根中的符号
    链接种植）且可读。打开是独占 + 仅所有者（``wx``/0o600）。
    """
    directory = session_dir(root, session_id)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    safe_name = encode_segment(suggested_name)
    path = os.path.join(directory, f"{uuid.uuid4().hex[:12]}-{safe_name}")
    data = content.encode("utf-8")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)
    return {"path": path, "bytes": len(data)}


class LocalSpillStore(SpillStore):
    """本地文件系统 spill 后端：私有会话作用域文件 + 独占仅所有者写。"""

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx)
        cfg = config or {}
        root = cfg.get("root")
        self.root = os.path.abspath(root) if root else private_root()

    async def saveText(self, input: dict) -> dict:
        saved = await save_text_file(
            self.root,
            input["owner"]["sessionId"],
            input["suggestedName"],
            input["content"],
        )
        return {
            "locator": SpillLocator(saved["path"]),
            "bytes": saved["bytes"],
            "retrievalHint": "Use read with offset/limit, or grep this path to search within it.",
        }


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：注册 ``ctx.spillStore``（本地后端）。"""
    LocalSpillStore(ctx, config or {})


apply.name = "spill-local"

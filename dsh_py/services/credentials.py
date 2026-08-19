"""凭据引用服务（对标 dsh 的 ``dsh-credentials``）。

``ctx.credentials`` 是凭据解析的 seam：配置 / 设置文件携带**引用**（ref），
消费者每次操作时经 :meth:`Credentials.resolve` 解析成真实值——引用格式
``^[A-Za-z_][A-Za-z0-9_]*$``（对齐 dsh 的 REF_PATTERN，如 ``DEEPSEEK_API_KEY``）。

- :func:`credential_ref` —— 校验并归一化一个引用；
- :class:`Credentials` —— ``resolve/set/delete/describe``，变更广播
  ``credentials/updated``；解析顺序：内存存储（``set`` 写入）→ 同名环境变量；
- 每次操作都重新解析，所以凭据变更对下一次操作立即可见（对齐 dsh 的
  「consumers re-resolve at each operation」）。
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service

# 合法引用名：字母/下划线开头，字母数字下划线（对齐 dsh 的 REF_PATTERN）
REF_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CredentialRefError(TypeError):
    """引用格式非法。"""


def credential_ref(value: str) -> str:
    """校验引用格式并原样返回（非法则抛 CredentialRefError）。"""
    if not isinstance(value, str) or not REF_PATTERN.fullmatch(value):
        raise CredentialRefError(
            f"credential ref {value!r} 非法（须匹配 {REF_PATTERN.pattern}）")
    return value


class Credentials(Service):
    """``credentials`` 服务：凭据引用解析，``ctx.credentials``。"""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "credentials")
        self._store: dict[str, str] = {}  # ref -> 显式写入的值

    async def resolve(self, ref: str) -> Optional[dict]:
        """解析一个引用：返回 ``{"value", "source"}``；全无则 None。

        解析顺序：显式存储（``set``）→ 同名环境变量。每次调用都重新解析，
        凭据变更对下一次操作立即可见。
        """
        credential_ref(ref)
        if ref in self._store:
            return {"value": self._store[ref], "source": "store"}
        env_value = os.environ.get(ref)
        if env_value is not None:
            return {"value": env_value, "source": "env"}
        return None

    async def set(self, ref: str, value: str) -> None:
        """显式写入一个凭据并广播 ``credentials/updated``。"""
        credential_ref(ref)
        self._store[ref] = value
        self.ctx.emit("credentials/updated", ref)

    async def delete(self, ref: str) -> bool:
        """删除显式写入的凭据（不影响环境变量）；成功返回 True。"""
        credential_ref(ref)
        if ref in self._store:
            del self._store[ref]
            self.ctx.emit("credentials/updated", ref)
            return True
        return False

    async def describe(self, ref: str) -> dict:
        """描述一个引用：当前是否可解析与来源。"""
        credential_ref(ref)
        resolved = await self.resolve(ref)
        if resolved is None:
            return {"ref": ref, "available": False}
        return {"ref": ref, "available": True, "source": resolved["source"]}


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``credentials`` 服务（凭据引用解析 seam）。"""
    Credentials(ctx)


apply.provides = ["credentials"]  # 声明：本插件提供 credentials 服务
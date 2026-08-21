"""非会话存储 hub（storage/storage，第 3 层）。

``ctx.storage``：具名后端注册表 + 可挂载的数据形式（form）。hub 本身不做
任何 IO——后端拥有媒体，数据形式（首先是 domain 层）拥有语义。

- :class:`BackendRegistry` —— 具名后端表（多后端并存；哪个后端服务哪个消费方
  由消费方配置决定，绝不是 hub 全局选择）。
- :class:`Storage` —— hub 服务：``backend`` 注册表 + ``mount/form`` 形式挂载。
- 后端契约（:class:`StorageBackend` / :class:`KvFacet` / :class:`KvUnit` /
  :class:`KvUnitDescriptor`）——一个后端拥有一个媒体并共享其生命周期；facets
  是可选成员（无法服务的数据形态直接省略，解析时 fail loud）。

与 dsh 差异（已注明）：dsh 的 ``Storage`` 经 ``storageBackendServiceKey``
为每个后端生成生命周期服务键，供 domain 插件以 ``ctx.inject`` 延迟激活；
dsh_py 的 domain 插件以 ``inject=["storage"]`` + 插件加载顺序保证后端先于
form 挂载（语义等价，机制简化）。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional, Protocol

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service

# 单元/表名的合法格式：作为文件名与无转义 SQL 标识符段都安全
UNIT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


# --------------------------------------------------------------------------- #
# 错误词汇
# --------------------------------------------------------------------------- #
STORAGE_ERROR_CODES = (
    "backend-not-found", "form-not-mounted", "duplicate-backend", "duplicate-mount",
    "version-mismatch", "malformed-medium", "closed",
)


class StorageError(Exception):
    """hub 与后端实现的错误：``code`` 是稳定契约，``message`` 是诊断散文。"""

    def __init__(self, code: str, message: str, cause: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        if cause is not None:
            self.__cause__ = cause


# --------------------------------------------------------------------------- #
# 后端契约
# --------------------------------------------------------------------------- #
class KvUnit(Protocol):
    """一个已打开的 KV 单元。值对本层是不透明 JSON：无 schema、无事件、无领域意义。

    单元**不**串行化并发写——写顺序是调用方的责任（domain 层每个单元跑一条
    写链）；单元只保证单次调用在媒体上原子、解析后耐久。close 之后的任何调用
    以 ``closed`` 拒绝。
    """

    async def loadAll(self) -> dict: ...
    async def putRecord(self, table: str, key: str, value: Any) -> None: ...
    async def deleteRecord(self, table: str, key: str) -> None: ...
    async def setGlobal(self, value: Any) -> None: ...
    async def close(self) -> None: ...


class KvFacet(Protocol):
    """键值数据形态：整单元快照 + 每条记录的耐久写。"""

    async def open(self, descriptor: dict) -> KvUnit: ...


class StorageBackend(Protocol):
    """一个已注册后端：拥有一个媒体并共享其生命周期。"""

    kv: Optional[KvFacet]

    async def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
class BackendRegistry:
    """具名 → 后端表。注册是 effect：返回的 disposer 移除该名字。

    disposer 不关闭后端——归属插件在注销后自行关闭。
    """

    def __init__(self) -> None:
        self._backends: dict[str, StorageBackend] = {}

    def register(self, name: str, backend: StorageBackend) -> Callable[[], None]:
        if name in self._backends:
            raise StorageError("duplicate-backend", f"storage 后端 {name!r} 已注册")
        self._backends[name] = backend

        def dispose() -> None:
            # 只移除本次注册的贡献：dispose 后重注册，过期 disposer 再触发
            # 不得移除后继者
            if self._backends.get(name) is backend:
                self._backends.pop(name, None)

        return dispose

    def get(self, name: str) -> StorageBackend:
        backend = self._backends.get(name)
        if backend is None:
            registered = ", ".join(self._backends.keys()) or "none"
            raise StorageError(
                "backend-not-found",
                f"storage 后端 {name!r} 未注册（已注册：{registered}）",
            )
        return backend

    def names(self) -> list[str]:
        return list(self._backends.keys())


class Storage(Service):
    """存储 hub 服务（``ctx.storage``）。"""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "storage")
        self.backend = BackendRegistry()
        self._forms: dict[str, Any] = {}

    def mount(self, form: str, facility: Any) -> Callable[[], None]:
        """挂载一个数据形式设施；返回的 disposer 卸载该形式。"""
        if form in self._forms:
            raise StorageError("duplicate-mount", f"storage 形式 {form!r} 已挂载")
        self._forms[form] = facility

        def unmount() -> None:
            # 过期 disposer 守卫：卸载后重挂，旧 disposer 不得移除后继者
            if self._forms.get(form) is facility:
                self._forms.pop(form, None)

        return unmount

    def form(self, form: str) -> Any:
        if form not in self._forms:
            raise StorageError("form-not-mounted", f"storage 形式 {form!r} 未挂载")
        return self._forms[form]

    @property
    def domain(self) -> Any:
        """领域数据形式；domain 层插件加载后可用。"""
        return self.form("domain")


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``ctx.storage`` hub 服务。"""
    Storage(ctx)


apply.name = "storage"
apply.provides = ["storage"]

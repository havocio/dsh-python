"""领域数据形式（storage/storage-domain，第 3 层）。

schema 校验、变更事件的 KV 域存储，架在 storage hub 之上：消费方只依赖本包，
绝不直接触碰后端。每次耐久写解析后，按写链顺序发一条 ``domain/changed``。

- :func:`define_domain` / :func:`domain_table` —— 领域声明词汇（名字/版本/表
  schema/可选全局单例）；配置错误在拥有包模块加载期 fail loud；
- :class:`DomainFacility` —— ``ctx.storageDomain``：按路由（默认后端 + 每域
  覆盖）打开已声明域；单开保留（并发开同名域 fail loud）；卸载时关闭遗留域；
- :class:`DomainImpl` —— 一个已打开域的运行时：内存权威 + 每条域的单写链
  （先等后端耐久，再改内存，再发事件——后端写被拒则内存不动，读写永不分叉）；
- :class:`KvTableImpl` —— 表句柄：get/entries/keys/size 同步读内存；
  put/delete/update 排写链。

与 dsh 差异（已注明）：dsh 的 record schema 用 zod；dsh_py 用
:mod:`dsh_py.core.schema`（schemastery 风格，``validate`` 抛 SchemaError）。
dsh 的写链是 Promise 尾；dsh_py 用 ``asyncio.Lock`` 串行（语义等价：按序、
失败不影响后续）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.services.storage import UNIT_NAME_RE, StorageError

logger = logging.getLogger("dsh_py.storage_domain")

DOMAIN_CHANGED_EVENT = "domain/changed"


# --------------------------------------------------------------------------- #
# 错误词汇
# --------------------------------------------------------------------------- #
class DomainError(Exception):
    """领域层错误：``code`` 是稳定契约，``message`` 是诊断散文。

    后端失败（``backend-not-found``/``version-mismatch``…）作为
    :class:`StorageError` 直接透传，领域层不重包。
    """

    def __init__(self, code: str, message: str, detail: Optional[dict] = None,
                 cause: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        if cause is not None:
            self.__cause__ = cause


# --------------------------------------------------------------------------- #
# 领域声明词汇
# --------------------------------------------------------------------------- #
def domain_table(value_schema: Any) -> dict:
    """声明一张表：``value_schema`` 校验每条存储记录。"""
    return {"valueSchema": value_schema}


def define_domain(spec: dict) -> dict:
    """校验并固定一个领域声明的字面类型与字段；错误在模块加载期 fail loud。

    拒绝：域/表名不符 ``UNIT_NAME_RE``；版本不是非负整数；global schema 接受
    ``None``（None 是媒体「从未写入」哨兵，可空 global 会在重开时把已存的
    None 静默还原为 initial）。
    """
    if UNIT_NAME_RE.match(spec["name"]) is None:
        raise ValueError(f"domain 名 {spec['name']!r} 必须匹配 {UNIT_NAME_RE.pattern}")
    if not isinstance(spec["version"], int) or spec["version"] < 0:
        raise ValueError(
            f"domain {spec['name']!r} version 必须是非负整数，得到 {spec['version']!r}",
        )
    for table in spec["tables"]:
        if UNIT_NAME_RE.match(table) is None:
            raise ValueError(f"domain {spec['name']!r} 表名 {table!r} 必须匹配 {UNIT_NAME_RE.pattern}")
    global_spec = spec.get("global")
    if global_spec is not None:
        try:
            global_spec["schema"].validate(None)
        except Exception:
            pass  # 拒绝 None：见 docstring
        else:
            raise ValueError(
                f"domain {spec['name']!r} global schema 不得接受 null："
                "null 是媒体「从未写入」哨兵，可空 global 无法往返",
            )
    return spec


def descriptor_of(spec: dict) -> dict:
    """把领域声明投影到后端面向的单元描述符。"""
    return {
        "name": spec["name"],
        "version": spec["version"],
        "tables": list(spec["tables"].keys()),
        "hasGlobal": spec.get("global") is not None,
    }


# --------------------------------------------------------------------------- #
# 领域运行时
# --------------------------------------------------------------------------- #
def _parse_record(domain: str, table: str, key: str, schema: Any, raw: Any) -> Any:
    """校验一条存储记录；失败翻译为带定位的 ``invalid-record``。"""
    try:
        return schema.validate(raw)
    except Exception as exc:  # noqa: BLE001 - SchemaError 或任何校验失败
        slot = "global" if table == "" else f"记录 {key!r}（表 {table!r}）"
        raise DomainError(
            "invalid-record",
            f"domain {domain!r}：已存 {slot} 不符合其 schema",
            detail={"table": table, "key": key},
            cause=exc,
        ) from exc


class KvTableImpl:
    """绑定到一张内存记录表与其域写链的表句柄。"""

    def __init__(self, host: Any, table_name: str, records: dict) -> None:
        self._host = host
        self._table_name = table_name
        self._records = records

    def get(self, key: str) -> Any:
        self._host.assert_readable()
        return self._records.get(key)

    def entries(self):
        self._host.assert_readable()
        return iter(list(self._records.items()))

    def keys(self):
        self._host.assert_readable()
        return iter(list(self._records.keys()))

    @property
    def size(self) -> int:
        self._host.assert_readable()
        return len(self._records)

    async def put(self, key: str, value: Any) -> None:
        return await self._host.enqueue(self._put_job(key, value))

    def _put_job(self, key: str, value: Any) -> Awaitable:
        async def job() -> None:
            await self._host.unit.putRecord(self._table_name, key, value)
            self._records[key] = value
            self._host.emit_changed({
                "domain": self._host.domain_name, "table": self._table_name,
                "key": key, "operation": "put", "value": value,
            })
        return job()

    async def delete(self, key: str) -> bool:
        return await self._host.enqueue(self._delete_job(key))

    def _delete_job(self, key: str) -> Awaitable:
        async def job() -> bool:
            # 存在性在该 job 的写链槽位判定：更早排队的同键 put 使本删除可见
            if key not in self._records:
                return False
            await self._host.unit.deleteRecord(self._table_name, key)
            del self._records[key]
            self._host.emit_changed({
                "domain": self._host.domain_name, "table": self._table_name,
                "key": key, "operation": "deleted",
            })
            return True
        return job()

    async def update(self, key: str, fn: Callable[[Any], Any]) -> Any:
        return await self._host.enqueue(self._update_job(key, fn))

    def _update_job(self, key: str, fn: Callable[[Any], Any]) -> Awaitable:
        async def job() -> Any:
            if key not in self._records:
                raise DomainError(
                    "missing-key",
                    f"domain {self._host.domain_name!r} 表 {self._table_name!r} "
                    f"无记录 {key!r} 可更新",
                )
            next_value = fn(self._records[key])
            await self._host.unit.putRecord(self._table_name, key, next_value)
            self._records[key] = next_value
            self._host.emit_changed({
                "domain": self._host.domain_name, "table": self._table_name,
                "key": key, "operation": "put", "value": next_value,
            })
            return next_value
        return job()


class DomainImpl:
    """一个已打开域的运行时（内存权威 + 单写链 + 变更事件）。"""

    def __init__(
        self,
        ctx: AppContext,
        spec: dict,
        unit: Any,
        records: dict,
        global_value: Any,
        on_closed: Callable[[], None],
    ) -> None:
        self._ctx = ctx
        self.name = spec["name"]
        self._unit = unit
        self._on_closed = on_closed
        self._tables: dict[str, KvTableImpl] = {}
        self._global_value = global_value
        self._global_spec = spec.get("global")
        self._lock = asyncio.Lock()
        self._disposing = False
        self._closed = False
        self._host = _TableHost(self)
        for table, table_records in records.items():
            self._tables[table] = KvTableImpl(self._host, table, table_records)

    # -- 句柄 ---------------------------------------------------------------- #
    @property
    def global_(self) -> Any:
        """全局单例句柄；声明无 global 的域访问它是调用方 bug（抛错）。"""
        if self._global_spec is None:
            raise RuntimeError(f"domain {self.name!r} 未声明 global")
        return _GlobalHandle(self)

    def table(self, name: str) -> KvTableImpl:
        table = self._tables.get(name)
        if table is None:
            raise RuntimeError(f"domain {self.name!r} 未声明表 {name!r}")
        return table

    # -- 生命周期 ------------------------------------------------------------ #
    async def close(self) -> None:
        """拒绝新写 → 排空已排队写（事件照发）→ 释放单元 → 释放域名。幂等。"""
        if self._closed:
            return
        self._disposing = True
        async with self._lock:  # 排空：等在途写完成
            pass
        await self._unit.close()
        self._closed = True
        self._on_closed()

    # -- 供 table/global 句柄使用 -------------------------------------------- #
    async def enqueue(self, job: Awaitable) -> Any:
        if self._disposing:
            raise DomainError("closed", f"domain {self.name!r} 已关闭")
        async with self._lock:
            return await job

    def assert_readable(self) -> None:
        if self._closed:
            raise DomainError("closed", f"domain {self.name!r} 已关闭")

    def emit_changed(self, change: dict) -> None:
        # 通知非事务参与者：提交点已过，监听器失败只记录不反噬
        try:
            self._ctx.emit(DOMAIN_CHANGED_EVENT, change)
        except Exception as exc:  # noqa: BLE001
            logger.warning("domain %r: %s 监听器失败：%r", self.name, DOMAIN_CHANGED_EVENT, exc)


class _TableHost:
    """把表句柄接到域拥有的写机制上的内部边界。"""

    def __init__(self, domain: DomainImpl) -> None:
        self.domain_name = domain.name
        self.unit = domain._unit
        self._domain = domain

    def enqueue(self, job: Awaitable) -> Any:
        return self._domain.enqueue(job)

    def assert_readable(self) -> None:
        self._domain.assert_readable()

    def emit_changed(self, change: dict) -> None:
        self._domain.emit_changed(change)


class _GlobalHandle:
    def __init__(self, domain: DomainImpl) -> None:
        self._domain = domain

    def get(self) -> Any:
        self._domain.assert_readable()
        return self._domain._global_value

    async def set(self, value: Any) -> None:
        return await self._domain.enqueue(self._set_job(value))

    def _set_job(self, value: Any) -> Awaitable:
        async def job() -> None:
            await self._domain._unit.setGlobal(value)
            self._domain._global_value = value
            self._domain.emit_changed({
                "domain": self._domain.name, "table": "", "key": "",
                "operation": "put", "value": value,
            })
        return job()


# --------------------------------------------------------------------------- #
# 领域设施（ctx.storageDomain）
# --------------------------------------------------------------------------- #
class DomainFacility:
    """挂载的领域数据形式：打开已声明域（路由后端）；单开保留。"""

    def __init__(self, ctx: AppContext, config: dict) -> None:
        self._ctx = ctx
        self._config = config
        self._domains: dict[str, DomainImpl] = {}
        self._reserved: set[str] = set()

    async def open(self, spec: dict) -> DomainImpl:
        if spec["name"] in self._reserved:
            raise DomainError("already-open", f"domain {spec['name']!r} 已打开")
        self._reserved.add(spec["name"])
        try:
            routes = self._config.get("routes", {}) or {}
            backend_name = routes.get(spec["name"]) or self._config["backend"]
            backend = self._ctx.storage.backend.get(backend_name)
            kv = getattr(backend, "kv", None)
            if kv is None:
                raise DomainError(
                    "facet-unsupported",
                    f"路由到 domain {spec['name']!r} 的后端 {backend_name!r} 无 kv facet",
                )
            unit = await kv.open(descriptor_of(spec))
            try:
                snapshot = await unit.loadAll()
                tables: dict[str, dict] = {}
                for table, table_spec in spec["tables"].items():
                    records: dict = {}
                    for key, raw in (snapshot["tables"].get(table) or {}).items():
                        records[key] = _parse_record(
                            spec["name"], table, key, table_spec["valueSchema"], raw,
                        )
                    tables[table] = records
                global_spec = spec.get("global")
                stored_global = snapshot.get("global")
                global_value = None
                if global_spec is not None:
                    if stored_global is None:
                        global_value = global_spec["initial"]  # 未写入：serve initial
                    else:
                        global_value = _parse_record(
                            spec["name"], "", "", global_spec["schema"], stored_global,
                        )
                # on_closed 在排空完成后运行：期间写仍发事件，域仍可解析
                domain = DomainImpl(
                    self._ctx, spec, unit, tables, global_value,
                    lambda: (self._domains.pop(spec["name"], None),
                             self._reserved.discard(spec["name"])),
                )
                self._domains[spec["name"]] = domain
                return domain
            except Exception:
                await unit.close()
                raise
        except Exception:
            self._reserved.discard(spec["name"])
            raise

    def get(self, name: str) -> Optional[DomainImpl]:
        return self._domains.get(name)

    async def close_all(self) -> None:
        await asyncio.gather(*(d.close() for d in list(self._domains.values())))


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：挂载 domain 数据形式并注册 ``ctx.storageDomain``。"""
    cfg = config or {}
    if "backend" not in cfg:
        raise ValueError("storage-domain: 必须配置默认 backend（没有放之四海皆准的媒体）")
    facility = DomainFacility(ctx, cfg)
    unmount = ctx.storage.mount("domain", facility)
    ctx.provide("storageDomain", facility)

    async def teardown() -> None:
        await facility.close_all()
        unmount()

    ctx.effect(lambda: asyncio.ensure_future(teardown()), label="storage-domain.closeAll")


apply.name = "storage-domain"
apply.inject = ["storage"]
apply.provides = ["storageDomain"]

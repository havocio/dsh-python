"""JSON 文件存储后端（storage/storage-json，第 3 层）。

在 JSON 文件中存储数据：每单元一个文件（``<root>/<unit>.json``），单文件
承载多表 + 可选全局单例槽。值对后端是不透明 JSON。

- 打开：媒体已存在时校验版本（不符 → ``version-mismatch``）；无法解析或
  结构非法 → ``malformed-medium``；
- 写：内存态为权威，每次写经临时文件 + ``os.replace`` 原子落盘（崩溃不产生
  半写行）；
- 关闭：原子落盘（幂等）。

与 dsh 差异（已注明）：dsh 的 storage-json 用 ``format`` 模块做版本化行
格式；dsh_py 以单 JSON 文档（``{"version", "tables", "global"}``）表达同一
契约，原子写复用 :mod:`dsh_py.util.atomic_write`。
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.services.storage import StorageError, StorageBackend
from dsh_py.util.atomic_write import write_file_atomic

# 后端注册名
BACKEND_NAME = "json"


def _read_unit_file(path: str) -> Optional[dict]:
    """读取单元文件；缺失返回 None；解析/结构非法抛 malformed-medium。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise StorageError(
            "malformed-medium", f"storage-json: 无法解析单元文件 {path!r}",
            cause=exc,
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("tables"), dict):
        raise StorageError(
            "malformed-medium", f"storage-json: 单元文件 {path!r} 结构非法",
        )
    return data


class JsonKvUnit:
    """一个已打开的 JSON KV 单元（内存权威 + 原子落盘）。"""

    def __init__(self, path: str, descriptor: dict) -> None:
        self._path = path
        self._name = descriptor["name"]
        self._closed = False
        data = _read_unit_file(path)
        if data is not None:
            if data.get("version") != descriptor["version"]:
                raise StorageError(
                    "version-mismatch",
                    f"storage-json: 单元 {self._name!r} 媒体版本 {data.get('version')!r} "
                    f"与期望 {descriptor['version']!r} 不符",
                )
            self._version = data["version"]
            self._tables: dict[str, dict[str, Any]] = {
                t: dict(records) for t, records in data["tables"].items()
            }
            self._global: Any = data.get("global", None)
        else:
            self._version = descriptor["version"]
            self._tables = {t: {} for t in descriptor["tables"]}
            self._global = None
            self._persist()

    def _assert_open(self) -> None:
        if self._closed:
            raise StorageError("closed", f"storage-json: 单元 {self._name!r} 已关闭")

    def _persist(self) -> None:
        write_file_atomic(
            self._path,
            json.dumps({"version": self._version, "tables": self._tables, "global": self._global},
                       ensure_ascii=False, indent=2),
            mode=0o600,
        )

    async def loadAll(self) -> dict:
        self._assert_open()
        return {
            "tables": {t: copy.deepcopy(records) for t, records in self._tables.items()},
            "global": copy.deepcopy(self._global),
        }

    async def putRecord(self, table: str, key: str, value: Any) -> None:
        self._assert_open()
        self._tables.setdefault(table, {})[key] = copy.deepcopy(value)
        self._persist()

    async def deleteRecord(self, table: str, key: str) -> None:
        self._assert_open()
        records = self._tables.get(table)
        if records is not None and key in records:
            del records[key]
            self._persist()

    async def setGlobal(self, value: Any) -> None:
        self._assert_open()
        self._global = copy.deepcopy(value)
        self._persist()

    async def close(self) -> None:
        if self._closed:
            return
        self._persist()
        self._closed = True


class JsonKvFacet:
    """JSON 后端的 kv facet：按单元名映射到 ``<root>/<name>.json``。"""

    def __init__(self, root: str) -> None:
        self._root = os.path.abspath(root)

    async def open(self, descriptor: dict) -> JsonKvUnit:
        os.makedirs(self._root, exist_ok=True)
        path = os.path.join(self._root, f"{descriptor['name']}.json")
        return JsonKvUnit(path, descriptor)


class JsonBackend:
    """JSON 文件后端（``ctx.storage.backend.get('json')``）。"""

    def __init__(self, root: str) -> None:
        self.kv = JsonKvFacet(root)

    async def close(self) -> None:
        return None


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：把 JSON 后端注册进 ``ctx.storage``。"""
    cfg = config or {}
    root = cfg.get("root") or os.path.join(
        os.path.expanduser("~"), ".dsh", "storage", "json",
    )
    backend = JsonBackend(root)
    dispose = ctx.storage.backend.register(BACKEND_NAME, backend)
    # 卸载：从注册表移除（媒体无持有资源，无需 close）
    ctx.effect(dispose, label="storage-json.register")


apply.name = "storage-json"
apply.inject = ["storage"]

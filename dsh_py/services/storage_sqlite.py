"""SQLite 存储后端（storage/storage-sqlite，第 3 层）。

在单个 SQLite 数据库文件中存储数据：单元元数据（版本）、记录（表名 + 键 +
JSON 文本值）与全局单例槽各一张表。值对后端是不透明 JSON。

- 打开：单元已存在且版本不符 → ``version-mismatch``；不存在则登记单元行；
- 写：每操作经 ``asyncio.to_thread`` 执行同步 sqlite3 事务（提交后即耐久）；
- 关闭：提交并关闭连接（幂等）。

内置 ``sqlite3``，零依赖。与 dsh 差异（已注明）：dsh 的 storage-sqlite 用
``schema.ts`` 的显式建表语句；dsh_py 用等价的三表结构（unit/records/global），
异步经 to_thread 包装同步驱动。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.services.storage import StorageError, StorageBackend

# 后端注册名
BACKEND_NAME = "sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS storage_units (
    name TEXT PRIMARY KEY,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS storage_records (
    unit TEXT NOT NULL,
    table_name TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (unit, table_name, key)
);
CREATE TABLE IF NOT EXISTS storage_global (
    unit TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SqliteKvUnit:
    """一个已打开的 SQLite KV 单元。"""

    def __init__(self, db_path: str, descriptor: dict) -> None:
        self._db_path = db_path
        self._name = descriptor["name"]
        self._version = descriptor["version"]
        self._declared_tables = list(descriptor.get("tables", []))
        self._closed = False
        self._open_db()

    def _open_db(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self._db_path)), exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        try:
            conn.executescript(_SCHEMA)
            row = conn.execute(
                "SELECT version FROM storage_units WHERE name = ?", (self._name,),
            ).fetchone()
            if row is not None:
                if row[0] != self._version:
                    raise StorageError(
                        "version-mismatch",
                        f"storage-sqlite: 单元 {self._name!r} 媒体版本 {row[0]!r} "
                        f"与期望 {self._version!r} 不符",
                    )
            else:
                conn.execute(
                    "INSERT INTO storage_units (name, version) VALUES (?, ?)",
                    (self._name, self._version),
                )
                conn.commit()
        finally:
            conn.close()

    def _assert_open(self) -> None:
        if self._closed:
            raise StorageError("closed", f"storage-sqlite: 单元 {self._name!r} 已关闭")

    def _run(self, operation) -> Any:
        """在后台线程执行一次同步 sqlite 操作。"""
        self._assert_open()

        def worker():
            conn = sqlite3.connect(self._db_path)
            try:
                return operation(conn)
            finally:
                conn.close()

        return asyncio.to_thread(worker)

    def _load_all_sync(self, conn: sqlite3.Connection) -> dict:
        # 声明的表恒返回（空表也含），再填已存记录
        tables: dict[str, dict[str, Any]] = {t: {} for t in self._declared_tables}
        for table_name, key, value_text in conn.execute(
            "SELECT table_name, key, value FROM storage_records WHERE unit = ?",
            (self._name,),
        ):
            tables.setdefault(table_name, {})[key] = json.loads(value_text)
        global_row = conn.execute(
            "SELECT value FROM storage_global WHERE unit = ?", (self._name,),
        ).fetchone()
        return {"tables": tables, "global": json.loads(global_row[0]) if global_row else None}

    async def loadAll(self) -> dict:
        self._assert_open()
        return await self._run(self._load_all_sync)

    async def putRecord(self, table: str, key: str, value: Any) -> None:
        value_text = json.dumps(value, ensure_ascii=False)

        def worker(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO storage_records (unit, table_name, key, value) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(unit, table_name, key) DO UPDATE SET value = excluded.value",
                (self._name, table, key, value_text),
            )
            conn.commit()

        await self._run(worker)

    async def deleteRecord(self, table: str, key: str) -> None:
        def worker(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM storage_records WHERE unit = ? AND table_name = ? AND key = ?",
                (self._name, table, key),
            )
            conn.commit()

        await self._run(worker)

    async def setGlobal(self, value: Any) -> None:
        value_text = json.dumps(value, ensure_ascii=False)

        def worker(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO storage_global (unit, value) VALUES (?, ?) "
                "ON CONFLICT(unit) DO UPDATE SET value = excluded.value",
                (self._name, value_text),
            )
            conn.commit()

        await self._run(worker)

    async def close(self) -> None:
        self._closed = True


class SqliteKvFacet:
    def __init__(self, db_path: str) -> None:
        self._db_path = os.path.abspath(db_path)

    async def open(self, descriptor: dict) -> SqliteKvUnit:
        return SqliteKvUnit(self._db_path, descriptor)


class SqliteBackend:
    """SQLite 后端（``ctx.storage.backend.get('sqlite')``）。"""

    def __init__(self, db_path: str) -> None:
        self.kv = SqliteKvFacet(db_path)
        self._db_path = db_path

    async def close(self) -> None:
        return None


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：把 SQLite 后端注册进 ``ctx.storage``。"""
    cfg = config or {}
    db_path = cfg.get("path") or os.path.join(
        os.path.expanduser("~"), ".dsh", "storage", "sqlite.db",
    )
    backend = SqliteBackend(db_path)
    dispose = ctx.storage.backend.register(BACKEND_NAME, backend)
    ctx.effect(dispose, label="storage-sqlite.register")


apply.name = "storage-sqlite"
apply.inject = ["storage"]

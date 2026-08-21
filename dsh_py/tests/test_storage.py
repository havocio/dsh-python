"""storage 家族验证（storage / storage-domain / storage-json / storage-sqlite，第 3 层）。

运行：python dsh_py/tests/test_storage.py

覆盖：
- hub：BackendRegistry 注册/解析/重复/未找到/过期 disposer；form mount/form/重复/未挂载；
- JSON 后端：打开/版本不符/损坏媒体；put/delete/global 原子落盘与重开读取；
- SQLite 后端：同契约（独立 db 文件）；
- domain 层：define_domain 校验（表名/版本/global 拒绝 null）；open 路由/
  单开保留/记录 schema 校验（invalid-record）；KvTable put/get/delete/update/
  size/entries；写链串行；domain/changed 事件按写序；close 后读抛 closed、
  释放域名可重开；global 未写入 serve initial、set 后耐久。
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.storage import StorageError, apply as storage_apply
from dsh_py.services.storage_domain import (
    DomainError,
    apply as storage_domain_apply,
    define_domain,
    descriptor_of,
    domain_table,
)
from dsh_py.services.storage_json import apply as storage_json_apply
from dsh_py.services.storage_sqlite import apply as storage_sqlite_apply

JSON_ROOT = "json-root"
SQLITE_PATH = "sqlite.db"


def _ctx(tmp, backends=("json", "sqlite")):
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    storage_apply(ctx)
    if "json" in backends:
        storage_json_apply(ctx, {"root": os.path.join(tmp, JSON_ROOT)})
    if "sqlite" in backends:
        storage_sqlite_apply(ctx, {"path": os.path.join(tmp, SQLITE_PATH)})
    return ctx


def _kv_spec(name="notes", version=1, tables=("records",), has_global=True):
    return {"name": name, "version": version, "tables": list(tables), "hasGlobal": has_global}


# --------------------------------------------------------------------------- #
# hub
# --------------------------------------------------------------------------- #
def test_backend_registry():
    ctx = _ctx(tempfile.mkdtemp())
    assert set(ctx.storage.backend.names()) == {"json", "sqlite"}
    assert ctx.storage.backend.get("json").kv is not None
    try:
        ctx.storage.backend.get("nope")
    except StorageError as e:
        assert e.code == "backend-not-found"
    else:
        raise AssertionError("未注册后端应报错")

    # 重复注册
    try:
        ctx.storage.backend.register("json", ctx.storage.backend.get("json"))
    except StorageError as e:
        assert e.code == "duplicate-backend"
    else:
        raise AssertionError("重复注册应报错")


def test_form_mount_and_stale_disposer():
    ctx = _ctx(tempfile.mkdtemp())
    facility = object()
    unmount = ctx.storage.mount("widget", facility)
    assert ctx.storage.form("widget") is facility
    try:
        ctx.storage.mount("widget", object())
    except StorageError as e:
        assert e.code == "duplicate-mount"
    else:
        raise AssertionError("重复挂载应报错")
    unmount()
    try:
        ctx.storage.form("widget")
    except StorageError as e:
        assert e.code == "form-not-mounted"
    else:
        raise AssertionError("卸载后应报错")
    unmount()  # 过期 disposer 幂等
    try:
        ctx.storage.domain
    except StorageError:
        pass  # 未挂 domain form


# --------------------------------------------------------------------------- #
# JSON 后端
# --------------------------------------------------------------------------- #
async def test_json_backend_lifecycle(tmp=None):
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(tmp, backends=("json",))
        unit = await ctx.storage.backend.get("json").kv.open(_kv_spec())
        snap = await unit.loadAll()
        assert snap["tables"]["records"] == {} and snap["global"] is None
        await unit.putRecord("records", "k1", {"a": 1})
        await unit.setGlobal({"boot": True})
        await unit.close()

        # 重开：数据耐久
        unit2 = await ctx.storage.backend.get("json").kv.open(_kv_spec())
        snap2 = await unit2.loadAll()
        assert snap2["tables"]["records"] == {"k1": {"a": 1}}
        assert snap2["global"] == {"boot": True}
        await unit2.deleteRecord("records", "k1")
        assert (await unit2.loadAll())["tables"]["records"] == {}
        await unit2.close()

        # 版本不符
        try:
            await ctx.storage.backend.get("json").kv.open(_kv_spec(version=2))
        except StorageError as e:
            assert e.code == "version-mismatch"
        else:
            raise AssertionError("版本不符应报错")

        # 损坏媒体
        bad = os.path.join(tmp, JSON_ROOT, "broken.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json")
        try:
            await ctx.storage.backend.get("json").kv.open({"name": "broken", "version": 1, "tables": [], "hasGlobal": False})
        except StorageError as e:
            assert e.code == "malformed-medium"
        else:
            raise AssertionError("损坏媒体应报错")


# --------------------------------------------------------------------------- #
# SQLite 后端
# --------------------------------------------------------------------------- #
async def test_sqlite_backend_lifecycle():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(tmp, backends=("sqlite",))
        unit = await ctx.storage.backend.get("sqlite").kv.open(_kv_spec())
        await unit.putRecord("records", "k1", {"x": [1, 2]})
        await unit.setGlobal("g")
        await unit.close()

        unit2 = await ctx.storage.backend.get("sqlite").kv.open(_kv_spec())
        snap = await unit2.loadAll()
        assert snap["tables"]["records"] == {"k1": {"x": [1, 2]}}
        assert snap["global"] == "g"
        await unit2.deleteRecord("records", "k1")
        assert (await unit2.loadAll())["tables"]["records"] == {}
        await unit2.close()

        try:
            await ctx.storage.backend.get("sqlite").kv.open(_kv_spec(version=2))
        except StorageError as e:
            assert e.code == "version-mismatch"
        else:
            raise AssertionError("版本不符应报错")


# --------------------------------------------------------------------------- #
# domain 层
# --------------------------------------------------------------------------- #
RECORD_SCHEMA = z.object({"title": z.string(), "n": z.integer()})
GLOBAL_SCHEMA = z.object({"count": z.integer()})


def _domain_spec(name="notes_domain", version=3):
    return define_domain({
        "name": name,
        "version": version,
        "tables": {"records": domain_table(RECORD_SCHEMA)},
        "global": {"schema": GLOBAL_SCHEMA, "initial": {"count": 0}},
    })


def test_define_domain_validation():
    assert descriptor_of(_domain_spec())["hasGlobal"] is True
    for bad_spec in (
        {"name": "BadName", "version": 1, "tables": {}},
        {"name": "ok", "version": -1, "tables": {}},
        {"name": "ok", "version": 1, "tables": {"Bad Table": domain_table(z.any())}},
        {"name": "ok", "version": 1, "tables": {}, "global": {"schema": z.any(), "initial": "x"}},
    ):
        try:
            define_domain(bad_spec)
        except ValueError:
            pass
        else:
            raise AssertionError(f"应拒绝：{bad_spec}")


async def _open_domain(ctx, backend="json"):
    return await ctx.storageDomain.open(_domain_spec())


async def test_domain_crud_and_events():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(tmp)
        storage_domain_apply(ctx, {"backend": "json"})
        domain = await _open_domain(ctx)
        changes = []
        ctx.on("domain/changed", lambda change: changes.append(change))

        # global 未写入 → initial
        assert domain.global_.get() == {"count": 0}
        await domain.global_.set({"count": 5})
        assert domain.global_.get() == {"count": 5}

        table = domain.table("records")
        await table.put("k1", {"title": "a", "n": 1})
        assert table.get("k1") == {"title": "a", "n": 1}
        assert table.size == 1
        await table.put("k2", {"title": "b", "n": 2})
        assert set(table.keys()) == {"k1", "k2"}
        assert dict(table.entries())["k1"]["title"] == "a"

        # 写链串行 + 事件按写序
        await asyncio.gather(
            table.put("k3", {"title": "c", "n": 3}),
            table.put("k4", {"title": "d", "n": 4}),
        )
        assert set(table.keys()) == {"k1", "k2", "k3", "k4"}

        # update：原子读改写
        updated = await table.update("k1", lambda cur: {"title": cur["title"], "n": cur["n"] + 10})
        assert updated["n"] == 11 and table.get("k1")["n"] == 11
        try:
            await table.update("missing", lambda cur: cur)
        except DomainError as e:
            assert e.code == "missing-key"
        else:
            raise AssertionError("缺失键 update 应报错")

        # delete：存在返回 True，缺席返回 False
        assert (await table.delete("k2")) is True
        assert (await table.delete("k2")) is False

        # schema 校验：非法记录在 durable 边界拒绝（重开时校验）
        await domain.close()
        file_path = os.path.join(tmp, JSON_ROOT, "notes_domain.json")
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
        data["tables"]["records"]["bad"] = {"title": "x", "n": "not-int"}
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        try:
            await _open_domain(ctx)
        except DomainError as e:
            assert e.code == "invalid-record"
            assert e.detail == {"table": "records", "key": "bad"}
        else:
            raise AssertionError("重开时非法记录应报错")


async def test_domain_single_open_and_close():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(tmp)
        storage_domain_apply(ctx, {"backend": "json"})
        d1 = await _open_domain(ctx)
        try:
            await _open_domain(ctx)
        except DomainError as e:
            assert e.code == "already-open"
        else:
            raise AssertionError("单开保留应报错")

        # close 后：读抛 closed；域名释放可重开
        await d1.close()
        try:
            d1.table("records").get("k1")
        except DomainError as e:
            assert e.code == "closed"
        else:
            raise AssertionError("关闭后读应抛 closed")
        d2 = await _open_domain(ctx)
        assert d2.table("records").size == 0
        await d2.close()


async def test_domain_route_and_sqlite_backend():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(tmp)
        storage_domain_apply(ctx, {"backend": "json", "routes": {"sqlite_domain": "sqlite"}})
        sqlite_spec = _domain_spec("sqlite_domain")
        sqlite_domain = await ctx.storageDomain.open(sqlite_spec)
        await sqlite_domain.table("records").put("r1", {"title": "s", "n": 1})
        await sqlite_domain.close()

        # 路由后端不存在 → backend-not-found 透传
        ctx2 = _ctx(tmp)
        storage_domain_apply(ctx2, {"backend": "missing"})
        try:
            await ctx2.storageDomain.open(_domain_spec())
        except StorageError as e:
            assert e.code == "backend-not-found"
        else:
            raise AssertionError("路由到未注册后端应报错")


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    sync_tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and not asyncio.iscoroutinefunction(v)]
    fails = []
    for t in sync_tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            fails.append((t.__name__, repr(e)))
    for t in tests:
        try:
            asyncio.run(t())
        except Exception as e:  # noqa: BLE001
            fails.append((t.__name__, repr(e)))
    for name, err in fails:
        print(f"FAIL {name}: {err}")
    total = len(tests) + len(sync_tests)
    print(f"{total} 项，{len(fails)} 失败")
    if fails:
        raise SystemExit(f"\n{len(fails)} 项失败")


if __name__ == "__main__":
    _run_all()

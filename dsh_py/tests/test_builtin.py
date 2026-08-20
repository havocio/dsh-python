"""内核内置服务 logger / reflect / registry 的验证（第 0 层收尾）。

运行：python -m dsh_py.tests.test_builtin
"""
import io
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import bootstrap


def test_builtins_provided_by_bootstrap():
    ctx = AppContext()
    bootstrap(ctx)
    for s in ("logger", "reflect", "registry"):
        assert ctx.has_service(s), f"内置服务 {s!r} 应由 bootstrap 提供"


def test_logger_level_filter_and_format():
    ctx = AppContext()
    bootstrap(ctx)
    buf = io.StringIO()
    old = sys.stderr
    sys.stderr = buf
    try:
        ctx.logger.set_level("INFO")
        ctx.logger.debug("hidden")          # 低于 INFO，过滤
        ctx.logger.info("hello", k=1)       # 输出
        ctx.logger.error("boom")            # 输出
    finally:
        sys.stderr = old
    out = buf.getvalue()
    assert "hidden" not in out
    assert "[INFO] [logger] hello k=1" in out
    assert "[ERROR] [logger] boom" in out


def test_reflect_introspection():
    ctx = AppContext()
    bootstrap(ctx)
    snap = ctx.reflect.snapshot()
    for s in ("logger", "reflect", "registry"):
        assert s in snap["services"]
    # 注册事件后内省可见
    ctx.on("my/event", lambda *a: None)
    assert "my/event" in ctx.reflect.events()
    # 作用域树内省
    assert "root" in ctx.reflect.scopes()


def test_registry_register_and_query():
    ctx = AppContext()
    bootstrap(ctx)
    seen = []
    ctx.on("registry/updated", lambda cat, id, op: seen.append((cat, id, op)))
    ctx.registry.register("agent", "a1", {"name": "A1"})
    assert ctx.registry.get("agent", "a1")["name"] == "A1"
    assert "a1" in ctx.registry.list("agent")
    assert "agent" in ctx.registry.list()
    assert ("agent", "a1", "register") in seen
    ctx.registry.remove("agent", "a1")
    assert ctx.registry.get("agent", "a1") is None
    assert ("agent", "a1", "remove") in seen


def main():
    test_builtins_provided_by_bootstrap()
    test_logger_level_filter_and_format()
    test_reflect_introspection()
    test_registry_register_and_query()
    print("OK: 内核内置服务（logger/reflect/registry）测试通过")


if __name__ == "__main__":
    main()

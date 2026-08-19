"""作用域树（extend / isolate / intercept / 事件可见性）的验证（第 0 层第二批）。

运行：python dsh_py/tests/test_scope.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext


def test_extend_child_independent_services():
    root = AppContext()
    child = root.extend("child")
    # 子作用域注册的服务，父不可见
    child.provide("only_child", 42)
    assert child.only_child == 42
    assert not root.has_service("only_child")
    # 父作用域的服务，子沿链可见
    root.provide("shared", "from-root")
    assert child.shared == "from-root"


def test_isolate_service_scope():
    root = AppContext()
    root.provide("storage", "root-storage")

    # 隔离 storage：子作用域可提供独立实现，父不受影响
    child = root.isolate("storage")
    assert child._isolate["storage"] is not root._isolate.get("storage")
    child.provide("storage", "child-storage")
    assert child.storage == "child-storage"
    assert root.storage == "root-storage"

    # 相同 label 的 isolate 调用共享作用域
    label = object()
    a = root.isolate("storage", label)
    b = root.isolate("storage", label)
    assert a._isolate["storage"] is b._isolate["storage"]


def test_isolate_blocks_parent_lookup():
    """隔离边界内的服务解析不应看到边界外的实现。"""
    root = AppContext()
    root.provide("db", "root-db")

    # 隔离 db 但不提供实现：解析应报错（而不是穿透到父的 root-db）
    child = root.isolate("db")
    assert child._isolate["db"] is not root._isolate.get("db")
    try:
        child.db
    except AttributeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("隔离边界内未提供实现时，不应穿透到父作用域")


def test_intercept_config():
    root = AppContext()
    child = root.intercept("llm", {"temperature": 0.2})
    # 拦截表沿祖先继承，子作用域可见；父不被修改
    assert child._intercept["llm"] == {"temperature": 0.2}
    assert "llm" not in root._intercept
    # 同名拦截在子孙作用域覆盖（对齐 cordis：intercept[name] = config），父仍保留原值
    grand = child.intercept("llm", {"max_tokens": 100})
    assert grand._intercept["llm"] == {"max_tokens": 100}
    assert child._intercept["llm"] == {"temperature": 0.2}
    assert grand._intercept is not child._intercept


def test_event_bubbles_to_parent_not_down():
    root = AppContext()
    child = root.extend("child")
    seen_root = []
    seen_child = []

    root.on("tick", lambda v: seen_root.append(v))
    child.on("tick", lambda v: seen_child.append(v))

    # 子作用域 emit：父（root）的监听器可见（冒泡）
    child.emit("tick", "from-child")
    assert seen_root == ["from-child"]
    assert seen_child == ["from-child"]

    # 父作用域 emit：子作用域的监听器不可见（只沿父链向上收集）
    seen_root.clear()
    seen_child.clear()
    root.emit("tick", "from-root")
    assert seen_root == ["from-root"]
    assert seen_child == [], "子作用域监听器不应收到父作用域事件"


def test_child_dispose_cleans_own_resources():
    root = AppContext()
    child = root.extend("ephemeral")
    seen = []
    child.provide("tmp_svc", object())
    child.on("tick", lambda v: seen.append(v))

    child.emit("tick", 1)
    assert seen == [1]
    assert child.has_service("tmp_svc")

    child.dispose()  # 会话结束：回收该作用域的全部资源
    child.emit("tick", 2)  # 监听器已注销（emit 在已卸载 ctx 上仍可调用，无监听器响应）
    assert seen == [1], "子作用域卸载后监听器不应再响应"
    assert not child.has_service("tmp_svc")
    # 父作用域不受影响
    assert root.has_service is not None
    assert not root.has_service("tmp_svc")


def test_global_listener_receives_all_scopes():
    root = AppContext()
    child = root.extend("child")
    seen = []
    # global 监听器恒可见（对标 cordis 的 { global: true }）
    root.on("evt", lambda v: seen.append(v), global_=True)
    root.emit("evt", "root")
    child.emit("evt", "child")
    assert seen == ["root", "child"]


async def main():
    test_extend_child_independent_services()
    test_isolate_service_scope()
    test_isolate_blocks_parent_lookup()
    test_intercept_config()
    test_event_bubbles_to_parent_not_down()
    test_child_dispose_cleans_own_resources()
    test_global_listener_receives_all_scopes()
    print("OK: 作用域树测试通过（extend/isolate/intercept、事件冒泡与隔离、作用域卸载）")


if __name__ == "__main__":
    asyncio.run(main())

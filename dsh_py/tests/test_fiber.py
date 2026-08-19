"""插件生命周期（Fiber / effect / 可卸载插件）的验证（第 0 层内核翻译 · 第一批）。

运行：python dsh_py/tests/test_fiber.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.core.fiber import Fiber, FiberState
from dsh_py.loader import CORE_PROFILE, load_profile


def test_fiber_state_lifecycle():
    fiber = Fiber(name="t")
    assert fiber.state == FiberState.PENDING
    fiber.start()
    assert fiber.state == FiberState.ACTIVE
    fiber.dispose()
    assert fiber.state == FiberState.DISPOSED
    # 幂等：重复 dispose 不抛错
    fiber.dispose()
    assert fiber.state == FiberState.DISPOSED


def test_effect_reverse_order():
    fiber = Fiber(name="t")
    fiber.start()
    order = []
    fiber.effect(lambda: order.append(1), label="a")
    fiber.effect(lambda: order.append(2), label="b")
    fiber.effect(lambda: order.append(3), label="c")
    fiber.dispose()
    # 逆序清理：c → b → a
    assert order == [3, 2, 1]


def test_effect_can_be_cancelled():
    fiber = Fiber(name="t")
    fiber.start()
    order = []
    cancel = fiber.effect(lambda: order.append(1), label="a")
    assert cancel() is True   # 提前取消
    assert cancel() is False  # 已取消，再取消返回 False
    fiber.dispose()
    assert order == []


def test_inactive_fiber_rejects_effect():
    fiber = Fiber(name="t")  # 未 start，处于 PENDING
    try:
        fiber.effect(lambda: None)
    except RuntimeError as e:
        assert "不可注册资源" in str(e)
    else:  # pragma: no cover
        raise AssertionError("PENDING 状态的 fiber 不应允许注册 effect")


def test_plugin_dispose_cleans_listener_and_service():
    ctx = AppContext()
    seen = []

    def demo_plugin(c, config):
        @c.on("tick")
        def listener(value):
            seen.append(value)
        c.provide("demo_svc", object())

    handle = ctx.plugin(demo_plugin, name="demo")
    assert ctx.has_service("demo_svc")
    ctx.emit("tick", 1)
    assert seen == [1]

    # 卸载插件：监听器注销 + 服务移除
    handle.dispose()
    ctx.emit("tick", 2)
    assert seen == [1], "插件卸载后监听器不应再响应"
    assert not ctx.has_service("demo_svc"), "插件卸载后服务应被移除"


def test_plugin_dispose_idempotent():
    ctx = AppContext()
    handle = ctx.plugin(lambda c, config: c.provide("x", 1), name="p")
    handle.dispose()
    handle.dispose()  # 幂等
    assert not ctx.has_service("x")


def test_provided_service_kept_if_replaced():
    ctx = AppContext()
    first = object()

    def p1(c, config):
        c.provide("svc", first)

    handle1 = ctx.plugin(p1, name="p1")
    second = object()
    ctx.provide("svc", second)  # 被后续覆盖（根 fiber 上）
    handle1.dispose()
    # 卸载 p1 不应移除已被替换的服务实例
    assert ctx.has_service("svc")
    assert ctx.svc is second


def test_load_profile_returns_unload_handles():
    ctx = AppContext()
    handles = load_profile(ctx, CORE_PROFILE)
    assert len(handles) == 5
    assert all(hasattr(h, "dispose") for h in handles)
    # 卸载全部核心插件后，五个核心服务应被回收
    for h in handles:
        h.dispose()
    for name in ("llm", "sessions", "tools", "agents", "agentLoop"):
        assert not ctx.has_service(name), f"卸载后 {name} 应被回收"


async def main():
    test_fiber_state_lifecycle()
    test_effect_reverse_order()
    test_effect_can_be_cancelled()
    test_inactive_fiber_rejects_effect()
    test_plugin_dispose_cleans_listener_and_service()
    test_plugin_dispose_idempotent()
    test_provided_service_kept_if_replaced()
    test_load_profile_returns_unload_handles()
    print("OK: 插件生命周期测试通过（Fiber 状态机、effect 逆序清理、插件可卸载）")


if __name__ == "__main__":
    asyncio.run(main())

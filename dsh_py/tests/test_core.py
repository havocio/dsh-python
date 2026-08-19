"""dsh_py 框架内核的自包含验证（Step 1）。

运行：python dsh_py/tests/test_core.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service


# --------------------------------------------------------------------------- #
# 测试用的桩服务
# --------------------------------------------------------------------------- #
class LlmStub(Service):
    """模拟一个注册为 ``llm`` 的服务。"""
    def __init__(self, ctx):
        super().__init__(ctx, "llm")

    def stream(self, options):  # pragma: no cover - 下方会用到
        return options


def test_service_registration_and_resolution():
    ctx = AppContext()
    svc = LlmStub(ctx)
    # 通过属性访问解析，对标 dsh 里的 ctx.llm
    assert ctx.llm is svc
    assert ctx.has_service("llm")
    assert not ctx.has_service("missing")


async def test_emit_and_on_fire_and_forget():
    ctx = AppContext()
    received = []

    async def listener(value):
        received.append(value)

    ctx.on("tick", listener)
    ctx.emit("tick", 42)
    # 发后不理会把协程调度到运行中的循环上，让出控制权以使其执行
    await asyncio.sleep(0)
    assert received == [42]


async def test_parallel_await_all():
    ctx = AppContext()
    seen = []

    async def a(x):
        seen.append(("a", x))

    async def b(x):
        seen.append(("b", x))

    await ctx.parallel("ev", 7)
    ctx.on("ev", a)
    ctx.on("ev", b)
    await ctx.parallel("ev", 7)
    assert ("a", 7) in seen and ("b", 7) in seen


async def test_serial_bail():
    ctx = AppContext()

    def first(x):
        return None  # 不拦截

    def second(x):
        return f"bailed:{x}"  # 拦截

    def third(x):  # pragma: no cover - 不应被执行
        return "should-not-run"

    ctx.on("s", first)
    ctx.on("s", second)
    ctx.on("s", third)
    result = await ctx.serial("s", 9)
    assert result == "bailed:9"


async def test_waterfall_chaining_with_next():
    ctx = AppContext()

    async def outer(payload, nxt):
        out = await nxt()
        return {"wrapped_by": "outer", "inner": out}

    async def middle(payload, nxt):
        out = await nxt()
        return {"wrapped_by": "middle", "inner": out}

    ctx.on("wf", outer)
    ctx.on("wf", middle)

    async def default():
        return {"base": True}

    result = await ctx.waterfall("wf", {"v": 1}, inner=default)
    # 最外层返回值胜出；链路把最内层逐层包裹
    assert result == {
        "wrapped_by": "outer",
        "inner": {"wrapped_by": "middle", "inner": {"base": True}},
    }


async def test_waterfall_veto_when_next_not_called():
    ctx = AppContext()

    async def gatekeeper(payload, nxt):
        # 否决：从不调用 next()
        return {"vetoed": True}

    ctx.on("wf2", gatekeeper)

    async def default():
        return {"ran": True}

    result = await ctx.waterfall("wf2", {}, inner=default)
    assert result == {"vetoed": True}


def test_plugin_inject_missing_raises():
    ctx = AppContext()

    def needs_llm(c, config):
        c.llm.stream(config)  # 会触碰尚未注册的服务

    try:
        ctx.plugin(needs_llm, {}, name="needs-llm", inject=["llm"])
    except RuntimeError as e:
        assert "llm" in str(e)
    else:  # pragma: no cover
        raise AssertionError("缺少 inject 依赖时本应抛出 RuntimeError")


def test_plugin_inject_resolves():
    ctx = AppContext()
    LlmStub(ctx)  # 提供 ctx.llm
    ran = {}

    def uses_llm(c, config):
        ran["ok"] = c.llm is not None

    ctx.plugin(uses_llm, {}, name="uses-llm", inject=["llm"])
    assert ran.get("ok") is True


async def main():
    test_service_registration_and_resolution()
    await test_emit_and_on_fire_and_forget()
    await test_parallel_await_all()
    await test_serial_bail()
    await test_waterfall_chaining_with_next()
    await test_waterfall_veto_when_next_not_called()
    test_plugin_inject_missing_raises()
    test_plugin_inject_resolves()
    print("OK: 内核测试全部通过（服务、事件、瀑布流、插件 inject）")


if __name__ == "__main__":
    asyncio.run(main())

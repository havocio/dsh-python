"""typert 协议层测试：装饰器元数据、注册扫描与校验、invoke 分派（direct/context）、
失败分类、客户端代理、注销。

运行：``python dsh_py/tests/test_typert.py``
"""

from __future__ import annotations

import asyncio

from dsh_py.core.context import AppContext
from dsh_py.services import typert as T


@T.remote_scope("calc")
class Calculator:
    """一个远程业务对象（@remote_scope 声明作用域）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    @T.remote()
    def add(self, a: int, b: int) -> int:
        self.calls.append("add")
        return a + b

    @T.remote("multiply")  # wire 别名
    def mul(self, a: int, b: int) -> int:
        return a * b

    async def slow(self, x: int) -> int:  # 未标记 → 不可远程
        return x * 2

    @T.remote()
    async def async_add(self, a: int, b: int) -> int:
        return a + b + 10


def _ctx() -> AppContext:
    ctx = AppContext()
    T.apply(ctx)
    return ctx


# --------------------------------------------------------------------------- #
# 1. 装饰器元数据
# --------------------------------------------------------------------------- #
def test_decorator_metadata() -> None:
    calc = Calculator()
    assert T._remote_scope_name(Calculator) == "calc"  # 类级作用域
    assert getattr(calc.add, T._REMOTE_ATTR) is True
    assert getattr(calc.add, T._WIRE_ATTR) == "add"
    assert getattr(calc.mul, T._WIRE_ATTR) == "multiply"  # 别名
    assert not getattr(calc.slow, T._REMOTE_ATTR, False)  # 未标记
    # 无装饰器的类：作用域回退类名
    assert T._remote_scope_name(object) == "object"


# --------------------------------------------------------------------------- #
# 2. 注册扫描与校验
# --------------------------------------------------------------------------- #
def test_register_scan_and_validation() -> None:
    ctx = _ctx()
    dispose = ctx.typertRegistry.register(obj=Calculator())  # 作用域从装饰器读
    assert ctx.typertRegistry.has_scope("calc")
    endpoints = ctx.typertRegistry.list()
    assert endpoints[0]["service"] == "calc"
    assert set(endpoints[0]["methods"]) == {"add", "multiply", "async_add"}  # slow 未标记
    # 无 @remote 方法 → 抛错
    class Plain:
        def hello(self) -> str:
            return "hi"

    try:
        ctx.typertRegistry.register("plain", Plain())
        raise AssertionError("无 @remote 方法应抛错")
    except ValueError:
        pass
    # 注销后作用域消失
    dispose()
    assert not ctx.typertRegistry.has_scope("calc")


# --------------------------------------------------------------------------- #
# 3. invoke：direct 分派与失败分类
# --------------------------------------------------------------------------- #
def test_invoke_direct_and_failures() -> None:
    async def main() -> None:
        ctx = _ctx()
        ctx.typertRegistry.register(obj=Calculator())
        # 成功（同步方法）
        result = await ctx.typertRegistry.invoke(
            T.InvocationDescriptor(id="1", service="calc", method="add", args={"a": 2, "b": 3}))
        assert result.ok and result.value == 5
        # wire 别名
        result = await ctx.typertRegistry.invoke(
            T.InvocationDescriptor(id="2", service="calc", method="multiply", args={"a": 4, "b": 5}))
        assert result.ok and result.value == 20
        # 异步方法
        result = await ctx.typertRegistry.invoke(
            T.InvocationDescriptor(id="3", service="calc", method="async_add", args={"a": 1, "b": 1}))
        assert result.ok and result.value == 12
        # 未注册作用域
        result = await ctx.typertRegistry.invoke(
            T.InvocationDescriptor(id="4", service="nope", method="x"))
        assert not result.ok and result.error.code == "SCOPE_NOT_FOUND"
        # 未注册方法
        result = await ctx.typertRegistry.invoke(
            T.InvocationDescriptor(id="5", service="calc", method="nope"))
        assert not result.ok and result.error.code == "METHOD_NOT_FOUND"
        # handler 异常 → FAILED（不抛）
        class Boom:
            @T.remote()
            def explode(self) -> None:
                raise RuntimeError("kaboom")

        ctx.typertRegistry.register("boom", Boom())
        result = await ctx.typertRegistry.invoke(
            T.InvocationDescriptor(id="6", service="boom", method="explode"))
        assert not result.ok and result.error.code == "FAILED"
        assert "kaboom" in result.error.message

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 4. 客户端代理
# --------------------------------------------------------------------------- #
def test_client_proxy() -> None:
    async def main() -> None:
        ctx = _ctx()
        ctx.typertRegistry.register(obj=Calculator())
        proxy = ctx.typertRegistry.client_for("calc")
        result = await proxy.add(a=10, b=20)
        assert result.ok and result.value == 30
        result = await proxy.multiply(a=6, b=7)
        assert result.ok and result.value == 42
        # 不存在的方法 → METHOD_NOT_FOUND 失败（不抛）
        result = await proxy.missing_method(x=1)
        assert not result.ok and result.error.code == "METHOD_NOT_FOUND"
        # 未注册命名空间 → SCOPE_NOT_FOUND
        other = ctx.typertRegistry.client_for("nope")
        result = await other.anything()
        assert not result.ok and result.error.code == "SCOPE_NOT_FOUND"

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 5. context 接收者
# --------------------------------------------------------------------------- #
def test_invoke_context_receiver() -> None:
    async def main() -> None:
        ctx = _ctx()
        # 注册一个 context 型接收者：从 ctx.<context> 服务解析
        # 用现有服务（如 tokenMeter）模拟：invocation.kind == "context" 时调 ctx 服务方法
        # 这里注册一个 dummy 服务演示
        class DemoService(T.Service):
            def __init__(self, ctx: AppContext) -> None:
                super().__init__(ctx, "demoService")

            def ping(self, text: str) -> str:
                return f"pong:{text}"

        DemoService(ctx)  # Service 构造自动 provide
        # context 接收者：端点（scope+method）须先注册，接收者从 ctx 服务解析
        class DemoRemote:
            @T.remote()
            def ping(self, text: str) -> str:
                raise RuntimeError("占位：context 型应调 ctx 服务而非注册对象")

        ctx.typertRegistry.register("demo", DemoRemote())
        result = await ctx.typertRegistry.invoke(T.InvocationDescriptor(
            id="c1", service="demo", method="ping",
            invocation={"kind": "context", "context": "demoService"},
            args={"text": "hi"},
        ))
        assert result.ok and result.value == "pong:hi"
        # context 服务不存在
        result = await ctx.typertRegistry.invoke(T.InvocationDescriptor(
            id="c2", service="demo", method="ping",
            invocation={"kind": "context", "context": "noSuchService"},
            args={},
        ))
        assert not result.ok and result.error.code == "CONTEXT_NOT_FOUND"

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def main() -> None:
    tests = [
        test_decorator_metadata,
        test_register_scan_and_validation,
        test_invoke_direct_and_failures,
        test_client_proxy,
        test_invoke_context_receiver,
    ]
    passed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"FAIL {test.__name__}: {exc}")
            traceback.print_exc()
    print(f"== {passed}/{len(tests)} passed ==")
    if passed != len(tests):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

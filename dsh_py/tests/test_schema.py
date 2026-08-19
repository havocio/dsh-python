"""Config schema 校验 + inject 依赖拓扑的验证（第 0 层第三批）。

运行：python dsh_py/tests/test_schema.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.core.schema import SchemaError
from dsh_py.loader import CORE_PROFILE, load_profile


# --------------------------------------------------------------------------- #
# schema 基础
# --------------------------------------------------------------------------- #
def test_scalar_types():
    assert z.string().validate("hi") == "hi"
    assert z.number().validate(3.14) == 3.14
    assert z.integer().validate(3) == 3
    assert z.boolean().validate(True) is True
    assert z.const("a").validate("a") == "a"
    try:
        z.integer().validate(3.5)
    except SchemaError as e:
        assert "期望整数" in str(e)
    else:  # pragma: no cover
        raise AssertionError("非整数应报错")


def test_optional_and_default():
    s = z.object({
        "a": z.string().default("x"),
        "b": z.integer().optional(),
    })
    out = s.validate({"a": None, "b": 1})
    assert out == {"a": "x", "b": 1}
    out2 = s.validate({})
    assert out2 == {"a": "x", "b": None}


def test_object_unknown_key_error_and_strip():
    s = z.object({"a": z.string()})
    try:
        s.validate({"a": "ok", "b": 1})
    except SchemaError as e:
        assert "未知字段" in str(e)
    else:  # pragma: no cover
        raise AssertionError("未知字段应报错")
    # extra="strip" 时剥离未知字段
    assert z.object({"a": z.string()}, extra="strip").validate({"a": "ok", "b": 1}) == {"a": "ok"}


def test_nested_path_in_error():
    s = z.object({"providers": z.array(z.object({"baseURL": z.string()}))})
    try:
        s.validate({"providers": [{"baseURL": 123}]})
    except SchemaError as e:
        assert "providers.0.baseURL" in e.path
    else:  # pragma: no cover
        raise AssertionError("嵌套错误应带路径定位")


def test_union():
    s = z.union([z.string(), z.integer()])
    assert s.validate("a") == "a"
    assert s.validate(1) == 1
    try:
        s.validate(1.5)
    except SchemaError as e:
        assert "联合" in str(e)
    else:  # pragma: no cover
        raise AssertionError("不匹配联合分支应报错")


# --------------------------------------------------------------------------- #
# 插件 Config 校验 + inject
# --------------------------------------------------------------------------- #
def test_plugin_config_validated_with_defaults():
    ctx = AppContext()
    seen = {}

    def apply(c, config):
        seen["config"] = config

    apply.Config = z.object({
        "instructions": z.string().default("默认指令"),
        "max_tokens": z.integer().optional(),
    })

    ctx.plugin(apply, {"max_tokens": 100})
    assert seen["config"] == {"instructions": "默认指令", "max_tokens": 100}

    # 配置写错：加载期即报错
    try:
        ctx.plugin(apply, {"max_tokens": "不是整数"})
    except SchemaError as e:
        assert "max_tokens" in e.path
    else:  # pragma: no cover
        raise AssertionError("配置错误应在加载期抛出 SchemaError")


def test_plugin_inject_read_from_apply_fn():
    ctx = AppContext()

    def needs_llm(c, config):
        assert c.llm is not None

    needs_llm.inject = ["llm"]
    # 未提供 llm 前加载应报错
    try:
        ctx.plugin(needs_llm)
    except RuntimeError as e:
        assert "llm" in str(e)
    else:  # pragma: no cover
        raise AssertionError("缺依赖应报错")

    from dsh_py.services.llm import LlmService
    LlmService(ctx)
    ctx.plugin(needs_llm)  # 依赖就绪后加载成功


# --------------------------------------------------------------------------- #
# 拓扑排序
# --------------------------------------------------------------------------- #
def test_topo_sort_reorders_by_dependency():
    """依赖者后加载：即使列表顺序颠倒，provides 者先执行。"""
    ctx = AppContext()
    order = []

    def provider(c, config):
        order.append("provider")
        c.provide("cache", object())

    provider.provides = ["cache"]

    def consumer(c, config):
        order.append("consumer")
        assert c.cache is not None

    consumer.inject = ["cache"]

    # 故意把 consumer 排在 provider 前面
    load_profile(ctx, [consumer, provider])
    assert order == ["provider", "consumer"]


def test_topo_sort_missing_provider_raises():
    def orphan(c, config):
        pass

    orphan.inject = ["不存在的东西"]
    try:
        load_profile(AppContext(), [orphan])
    except RuntimeError as e:
        assert "不存在的东西" in str(e)
    else:  # pragma: no cover
        raise AssertionError("依赖无人提供应报错")


def test_topo_sort_cycle_raises():
    def a(c, config):
        pass

    def b(c, config):
        pass

    a.provides = ["x"]
    a.inject = ["y"]
    b.provides = ["y"]
    b.inject = ["x"]
    try:
        load_profile(AppContext(), [a, b])
    except RuntimeError as e:
        assert "循环依赖" in str(e)
    else:  # pragma: no cover
        raise AssertionError("循环依赖应报错")


def test_core_profile_topo_stable():
    """CORE_PROFILE 拓扑排序后顺序保持正确（registry 先于 loop）。"""
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    assert ctx.has_service("llm")
    assert ctx.has_service("agents")
    assert ctx.has_service("agentLoop")
    # 顺序颠倒的 profile 也能被拓扑纠正
    ctx2 = AppContext()
    reversed_profile = list(reversed(CORE_PROFILE))
    load_profile(ctx2, reversed_profile)
    assert ctx2.has_service("agentLoop") and ctx2.has_service("agents")


async def main():
    test_scalar_types()
    test_optional_and_default()
    test_object_unknown_key_error_and_strip()
    test_nested_path_in_error()
    test_union()
    test_plugin_config_validated_with_defaults()
    test_plugin_inject_read_from_apply_fn()
    test_topo_sort_reorders_by_dependency()
    test_topo_sort_missing_provider_raises()
    test_topo_sort_cycle_raises()
    test_core_profile_topo_stable()
    print("OK: schema 校验与依赖拓扑测试通过（类型/默认值/路径定位、Config 校验、inject 排序/缺失/循环）")


if __name__ == "__main__":
    asyncio.run(main())

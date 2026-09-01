"""code-runtime seam 契约单测（对照 dsh 临时冒烟脚本，正式入库）。

覆盖：绑定命名空间校验、运行请求校验、保留字/标识符/dunder 规则、结果构造。
均不依赖任何后端执行器（进程/worker），仅验证 seam 契约的纯逻辑。
"""

from __future__ import annotations

import asyncio

from dsh_py.services.code_runtime import (
    CodeBindingErrorClass,
    CodeBindingNamespace,
    CodeRunFailure,
    CodeRunRequest,
    CodeRunResult,
    DUNDER_MEMBER,
    IDENTIFIER,
    PORTABLE_RESERVED_WORDS,
    RESERVED_BINDING_GLOBALS,
    validate_binding_namespace,
    validate_run_request,
)


async def test_validate_binding_namespace_ok() -> None:
    ns = CodeBindingNamespace(global_name="tools", functions={"run": lambda *a: None})
    validate_binding_namespace(ns)  # 不应抛
    ec_ns = CodeBindingNamespace(
        global_name="fs",
        functions={},
        errorClass=CodeBindingErrorClass(name="FsError", memberNameProperty="code"),
    )
    validate_binding_namespace(ec_ns)
    print("  ✓ 合法绑定命名空间通过校验")


async def test_validate_binding_namespace_rejects() -> None:
    # 非标识符
    try:
        validate_binding_namespace(CodeBindingNamespace(global_name="1bad", functions={}))
        raise AssertionError("应拒绝非标识符")
    except ValueError:
        pass
    # 保留字
    try:
        validate_binding_namespace(CodeBindingNamespace(global_name="return", functions={}))
        raise AssertionError("应拒绝保留字")
    except ValueError:
        pass
    # 后端保留槽位
    try:
        validate_binding_namespace(CodeBindingNamespace(global_name="console", functions={}))
        raise AssertionError("应拒绝后端保留槽位")
    except ValueError:
        pass
    # 错误成员名：dunder
    try:
        validate_binding_namespace(CodeBindingNamespace(
            global_name="fs", functions={},
            errorClass=CodeBindingErrorClass(name="E", memberNameProperty="__x__")))
        raise AssertionError("应拒绝 dunder 成员")
    except ValueError:
        pass
    # 错误成员名：根保留集
    try:
        validate_binding_namespace(CodeBindingNamespace(
            global_name="fs", functions={},
            errorClass=CodeBindingErrorClass(name="E", memberNameProperty="name")))
        raise AssertionError("应拒绝根保留成员名")
    except ValueError:
        pass
    print("  ✓ 非法绑定命名空间被精确拒绝")


async def test_validate_run_request_delegates() -> None:
    bad = CodeRunRequest(program="x", bindings=[
        CodeBindingNamespace(global_name="return", functions={})])
    try:
        validate_run_request(bad)
        raise AssertionError("应拒绝非法绑定")
    except ValueError:
        pass
    good = CodeRunRequest(program="x", bindings=[
        CodeBindingNamespace(global_name="tools", functions={"run": lambda *a: None})])
    validate_run_request(good)  # 通过
    print("  ✓ run 请求校验委托到命名空间校验")


async def test_reserved_word_and_identifier_rules() -> None:
    assert IDENTIFIER.match("abc_1") and not IDENTIFIER.match("1abc")
    assert "return" in PORTABLE_RESERVED_WORDS
    assert "console" in RESERVED_BINDING_GLOBALS
    assert DUNDER_MEMBER.match("__proto__") and not DUNDER_MEMBER.match("normal")
    print("  ✓ 保留字/标识符/dunder 规则正确")


async def test_code_run_result_defaults() -> None:
    r = CodeRunResult(value=1)
    assert r.logs == []  # __post_init__ 自动填充
    assert r.error is None
    r2 = CodeRunResult(value=None, error=CodeRunFailure(kind="abort", message="x"))
    assert r2.logs == []
    print("  ✓ CodeRunResult 默认值（logs 自动填充）")


async def main() -> None:
    print("== test_code_runtime ==")
    await test_validate_binding_namespace_ok()
    await test_validate_binding_namespace_rejects()
    await test_validate_run_request_delegates()
    await test_reserved_word_and_identifier_rules()
    await test_code_run_result_defaults()
    print("OK: code-runtime seam 契约单测通过")


if __name__ == "__main__":
    asyncio.run(main())

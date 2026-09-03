"""skill 家族契约单测（纯逻辑 / mock，不依赖 provider 后端）。

覆盖 ``services/skill.py`` 的纯逻辑可测面：名称校验、调用策略标志、文本转义、
技能渲染、候选/定义校验、码点比较。运行范式与仓库其他 ``test_*.py`` 一致。
"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

from dsh_py.services.skill import (
    SkillDefinition,
    SkillInvocationPolicy,
    SkillResourceBase,
    SkillSummary,
    _compare_code_points,
    _escape_attr,
    _render_resource_hint,
    _validate_candidate,
    _validate_definition,
    escape_text,
    is_model_invocable,
    is_skill_name,
    is_user_invocable,
    render_skill_content,
)


def test_is_skill_name() -> None:
    assert is_skill_name("my-skill")
    assert is_skill_name("a")
    assert is_skill_name("a-b-c")
    assert is_skill_name("a1-b2")
    assert not is_skill_name("MySkill")
    assert not is_skill_name("my_skill")
    assert not is_skill_name("")
    assert is_skill_name("1abc")  # 首字符可为数字（正则 [a-z0-9]+）
    assert not is_skill_name("-abc")
    assert not is_skill_name("a b")
    print("  ✓ is_skill_name kebab-case 校验正确")


def test_invocation_flags() -> None:
    inv = SkillInvocationPolicy(model_invocable=True, user_invocable=False)
    s = SkillSummary(name="x", description="d", invocation=inv, source="s", provider="p")
    assert is_model_invocable(s) is True
    assert is_user_invocable(s) is False
    print("  ✓ is_model/user_invocable 读取标志正确")


def test_escape() -> None:
    assert _escape_attr('a&b"c<d') == "a&amp;b&quot;c&lt;d"
    assert escape_text("a&b<c>d") == "a&amp;b&lt;c&gt;d"
    print("  ✓ _escape_attr / escape_text 转义正确")


def test_render_resource_hint() -> None:
    base = SkillSummary(name="x", description="d",
                        invocation=SkillInvocationPolicy(True, True),
                        source="s", provider="p")

    # 无资源基 → provider 托管提示
    none_hint = _render_resource_hint(base)
    assert any("provider \"p\"" in line for line in none_hint)

    # directory 基 → 基目录提示
    dir_skill = SkillSummary(name="x", description="d",
                             invocation=SkillInvocationPolicy(True, True),
                             source="s", provider="p",
                             resource_base=SkillResourceBase(kind="directory", path="/sk"))
    dir_hint = _render_resource_hint(dir_skill)
    assert any("/sk" in line for line in dir_hint)

    # url 基 → 基 URL 提示
    url_skill = SkillSummary(name="x", description="d",
                             invocation=SkillInvocationPolicy(True, True),
                             source="s", provider="p",
                             resource_base=SkillResourceBase(kind="url", url="https://e"))
    assert any("https://e" in line for line in _render_resource_hint(url_skill))

    # opaque 基 → description 提示
    op_skill = SkillSummary(name="x", description="d",
                            invocation=SkillInvocationPolicy(True, True),
                            source="s", provider="p",
                            resource_base=SkillResourceBase(kind="opaque", description="DSC"))
    assert any("DSC" in line for line in _render_resource_hint(op_skill))
    print("  ✓ _render_resource_hint 四种分支正确")


def test_render_skill_content() -> None:
    s = SkillDefinition(name="my-skill", description="d",
                        invocation=SkillInvocationPolicy(True, True),
                        source="s", provider="p", content="BODY")
    out = render_skill_content(s)
    assert '<skill_content name="my-skill">' in out
    assert "<skill_instructions>" in out
    assert "BODY" in out
    assert out.strip().endswith("</skill_content>")
    print("  ✓ render_skill_content 包裹形状正确")


def test_compare_code_points() -> None:
    assert _compare_code_points("a", "a") == 0
    assert _compare_code_points("a", "b") == -1
    assert _compare_code_points("b", "a") == 1
    print("  ✓ _compare_code_points 比较正确")


def test_validate_definition() -> None:
    good = SkillDefinition(name="ok-skill", description="d",
                           invocation=SkillInvocationPolicy(True, True),
                           source="s", provider="p", content="body")
    _validate_definition(good)  # 不应抛

    for bad in (
        SkillDefinition(name="Bad", description="d",
                        invocation=SkillInvocationPolicy(True, True),
                        source="s", provider="p", content="body"),
        SkillDefinition(name="ok-skill", description="",
                        invocation=SkillInvocationPolicy(True, True),
                        source="s", provider="p", content="body"),
        SkillDefinition(name="ok-skill", description="d",
                        invocation=SkillInvocationPolicy(True, True),
                        source="s", provider="p", content=123),  # type: ignore[arg-type]
    ):
        try:
            _validate_definition(bad)
            raise AssertionError("应抛错")
        except (TypeError, RuntimeError):
            pass
    print("  ✓ _validate_definition 边界校验正确")


def test_validate_candidate() -> None:
    cand = SimpleNamespace(
        name="ok-skill", description="d",
        invocation=SkillInvocationPolicy(True, True),
        when_to_use=None, source="p", rank=1, provider="p", path=None)
    _validate_candidate(cand, "p")  # 不应抛

    # provider 不匹配 → RuntimeError
    try:
        _validate_candidate(cand, "other")
        raise AssertionError("provider 不匹配应抛 RuntimeError")
    except RuntimeError:
        pass

    # 非法名 → RuntimeError
    bad_name = SimpleNamespace(
        name="Bad", description="d",
        invocation=SkillInvocationPolicy(True, True),
        when_to_use=None, source="p", rank=1, provider="p", path=None)
    try:
        _validate_candidate(bad_name, "p")
        raise AssertionError("非法名应抛 RuntimeError")
    except RuntimeError:
        pass

    # rank 非法（bool） → RuntimeError
    bad_rank = SimpleNamespace(
        name="ok-skill", description="d",
        invocation=SkillInvocationPolicy(True, True),
        when_to_use=None, source="p", rank=True, provider="p", path=None)  # type: ignore[arg-type]
    try:
        _validate_candidate(bad_rank, "p")
        raise AssertionError("rank 非法应抛 RuntimeError")
    except RuntimeError:
        pass
    print("  ✓ _validate_candidate 边界校验正确")


def _main() -> None:
    print("== test_skill ==")
    test_is_skill_name()
    test_invocation_flags()
    test_escape()
    test_render_resource_hint()
    test_render_skill_content()
    test_compare_code_points()
    test_validate_definition()
    test_validate_candidate()
    print("== test_skill: ALL PASS ==")


if __name__ == "__main__":
    _main()

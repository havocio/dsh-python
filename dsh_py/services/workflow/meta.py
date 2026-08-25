"""Meta 校验：把调用方提供的数据对照 ``WorkflowMeta`` 契约逐条命名违规。

Meta 作为 schema 检查过的 JSON 数据到达，绝不作为被求值的脚本文本；在宿主上
求值它可能运行脚本环境之外的 getter（dsh 的 worker 超时用于隔离模型写的代码，
这里内联同样坚持「meta 只当数据用」）。对标 ``@deepseek-ai/dsh-workflow-worker-thread/meta``。
"""

from __future__ import annotations

from typing import Any

from .types import WorkflowMeta, WorkflowPhase
from . import WorkflowError

KNOWN_FIELDS = frozenset({"name", "description", "whenToUse", "phases"})
KNOWN_PHASE_FIELDS = frozenset({"title", "detail", "provider", "model"})


def _validate_meta_shape(meta: Any) -> tuple[WorkflowMeta | None, list[str]]:
    """收集一个 meta 值的形状违规（按 seam 契约是纯 JSON 数据）。"""
    violations: list[str] = []
    if not isinstance(meta, dict):
        return None, ["meta must be an object"]
    record = meta
    for key in record:
        if key not in KNOWN_FIELDS:
            violations.append(f"meta.{key} is not a recognized field (name/description/whenToUse/phases)")
    name = record.get("name")
    if not isinstance(name, str) or len(name) == 0:
        violations.append("meta.name must be a non-empty string")
    description = record.get("description")
    if not isinstance(description, str) or len(description) == 0:
        violations.append("meta.description must be a non-empty string")
    when_to_use = record.get("whenToUse")
    if when_to_use is not None and not isinstance(when_to_use, str):
        violations.append("meta.whenToUse must be a string")
    phases: list[WorkflowPhase] = []
    raw_phases = record.get("phases")
    if raw_phases is not None:
        if not isinstance(raw_phases, list):
            violations.append("meta.phases must be an array")
        else:
            for index, phase in enumerate(raw_phases):
                if not isinstance(phase, dict):
                    violations.append(f"meta.phases[{index}] must be an object")
                    continue
                entry = phase
                for key in entry:
                    if key not in KNOWN_PHASE_FIELDS:
                        violations.append(f"meta.phases[{index}].{key} is not a recognized field")
                ptitle = entry.get("title")
                if not isinstance(ptitle, str) or len(ptitle) == 0:
                    violations.append(f"meta.phases[{index}].title must be a non-empty string")
                pdetail = entry.get("detail")
                if pdetail is not None and not isinstance(pdetail, str):
                    violations.append(f"meta.phases[{index}].detail must be a string")
                pprovider = entry.get("provider")
                if pprovider is not None and not isinstance(pprovider, str):
                    violations.append(f"meta.phases[{index}].provider must be a string")
                pmodel = entry.get("model")
                if pmodel is not None and not isinstance(pmodel, str):
                    violations.append(f"meta.phases[{index}].model must be a string")
                if len(violations) == 0:
                    phases.append(
                        WorkflowPhase(
                            title=ptitle,
                            detail=pdetail,
                            provider=pprovider,
                            model=pmodel,
                        )
                    )
    if violations:
        return None, violations
    return (
        WorkflowMeta(
            name=name,
            description=description,
            whenToUse=when_to_use,
            phases=tuple(phases) if raw_phases is not None else None,
        ),
        [],
    )


def validate_meta(value: Any) -> WorkflowMeta:
    """校验调用方提供的 meta 值并返回**规范化副本**（引擎绝不别名调用方对象）。

    :param value: start 请求中的 meta 数据（按 seam 契约是纯 JSON）。
    :returns: 已校验、规范化的 meta 块。
    :raises WorkflowError: ``META_INVALID``，逐条命名全部违规。
    """
    meta, violations = _validate_meta_shape(value)
    if meta is None:
        raise WorkflowError(f"invalid meta: {'; '.join(violations)}", "META_INVALID")
    return meta

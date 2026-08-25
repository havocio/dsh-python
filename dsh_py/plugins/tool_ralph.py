"""模型侧前台 Ralph 循环（对标 dsh 的 ``@deepseek-ai/dsh-tool-ralph``）。

一段**固定脚本**经 workflow/subagent seam 每轮启动一个全新的结构化输出子，
子之间只携带不可变目标与上一个有界 handoff。模型只能提供数据——循环、
provider 路由、schema 与 handoff 校验均部署所有，模型无法改动。

**与 dsh 差异**：``RALPH_SCRIPT`` 从 JS 转写为 Python（hook 契约相同）；
结构化输出依赖 subagents seam 的 JSON 提取兜底（见 services/subagents.py）；
``exec.parent`` 判定省略。``presentCall/presentResult`` 展示挂钩 dsh_py 无
对应缝，省略。
"""

from __future__ import annotations

import json
from typing import Any

from dsh_py.core.context import AppContext

name = "tool-ralph"
inject = ["tools", "workflowEngine", "subagents", "systemPrompt"]

RALPH_META = {
    "name": "ralph-loop",
    "description": "Iterate toward one objective with a fresh child and bounded structured handoff per round.",
    "phases": [{"title": "Fresh-agent rounds", "detail": "One clean child context per Ralph round."}],
}

#: 固定、部署所有的编排（Python re-target）。每轮用全新结构化子，只携带
#: 不可变目标与前一个有界 handoff。
RALPH_SCRIPT = '''\
import json

report_schema = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["continue", "complete", "blocked"]},
        "summary": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "nextSteps": {"type": "array", "items": {"type": "string"}},
        "blocker": {"type": "string"},
    },
    "required": ["status", "summary", "evidence", "nextSteps", "blocker"],
    "additionalProperties": False,
}

def normalized_text(value):
    return isinstance(value, str) and len(value) > 0 and value == value.strip()

def normalized_list(value):
    return isinstance(value, list) and all(normalized_text(v) for v in value)

def validate_report(report):
    if not isinstance(report, dict):
        raise RuntimeError("Ralph child returned no structured round report")
    if not normalized_text(report.get("summary")):
        raise RuntimeError("Ralph round report summary must be non-empty and normalized")
    if not normalized_list(report.get("evidence")) or not normalized_list(report.get("nextSteps")):
        raise RuntimeError("Ralph round report evidence and nextSteps must contain only non-empty normalized strings")
    if not isinstance(report.get("blocker"), str) or report["blocker"] != report["blocker"].strip():
        raise RuntimeError("Ralph round report blocker must be a normalized string")
    status = report.get("status")
    if status == "continue":
        if len(report["nextSteps"]) == 0 or report["blocker"] != "":
            raise RuntimeError("a continuing Ralph report needs nextSteps and an empty blocker")
    elif status == "complete":
        if len(report["evidence"]) == 0 or len(report["nextSteps"]) != 0 or report["blocker"] != "":
            raise RuntimeError("a complete Ralph report needs evidence, no nextSteps, and an empty blocker")
    elif status == "blocked":
        if not normalized_text(report["blocker"]):
            raise RuntimeError("a blocked Ralph report needs a concrete blocker")
    else:
        raise RuntimeError("Ralph round report status is invalid")
    serialized = json.dumps(report)
    if len(serialized) > args["maxHandoffChars"]:
        raise RuntimeError("Ralph round report exceeds maxHandoffChars (" + str(len(serialized)) + " > " + str(args["maxHandoffChars"]) + ")")
    return report

previous = None
phase("Fresh-agent rounds")
for round_number in range(1, args["maxRounds"] + 1):
    prior = "(none — this is the first round)" if previous is None else json.dumps(previous)
    prompt = "\\n\\n".join([
        "You are one fresh worker in a foreground Ralph loop. You receive no parent conversation and no prior child session. Do not call the ralph tool: this round already is its worker.",
        "Immutable objective:\\n" + args["objective"],
        "Ralph round: " + str(round_number) + " of " + str(args["maxRounds"]) + ".",
        "The shared workspace and its current working tree are the long-term memory and source of truth. Inspect them before acting, preserve existing work, perform concrete in-scope work, and verify what you change. Treat the previous report only as a bounded handoff; confirm it against the workspace.",
        "Previous structured handoff:\\n" + prior,
        "Return one report with exact normalized strings. Use status continue with at least one nextSteps entry while useful work remains; complete only with concrete evidence and no nextSteps; blocked only when no meaningful progress is possible without human input or an external-state change. blocker must be empty unless blocked.",
    ])
    raw_report = await agent(prompt, {
        "label": "Ralph round " + str(round_number),
        "phase": "Fresh-agent rounds",
        "schema": report_schema,
    })
    if raw_report is None:
        return {"status": "round-failed", "roundsStarted": round_number, "lastReport": previous}
    report = validate_report(raw_report)
    if report["status"] == "complete":
        return {"status": "complete", "roundsStarted": round_number, "report": report}
    if report["status"] == "blocked":
        return {"status": "blocked", "roundsStarted": round_number, "report": report}
    previous = report
return {"status": "budget-limited", "roundsStarted": args["maxRounds"], "report": previous}
'''

DESCRIPTION = (
    "Run a foreground fresh-agent Ralph loop toward one immutable objective. "
    "Use only when the direct human explicitly asks for Ralph or fresh-agent iteration. Each round "
    "opens a new child with no parent conversation or prior child session; the shared workspace is "
    "long-term memory, and only a bounded structured report crosses rounds. The call returns when "
    "a worker reports completion or a concrete blocker, or at the round limit. Ordinary long-running same-session work "
    "belongs to goal tools."
)


def resolve_config(config: Any) -> dict:
    """校验默认值，即使调用方未经 Loader 规范化直接调 apply()。"""
    config = config or {}
    subagent_provider = config.get("subagentProvider", "spawn")
    max_rounds = int(config.get("maxRounds", 256) or 256)
    max_handoff_chars = int(config.get("maxHandoffChars", 16384) or 16384)
    max_result_chars = int(config.get("maxResultChars", 16384) or 16384)
    if len(subagent_provider) == 0 or subagent_provider != subagent_provider.strip():
        raise TypeError("subagentProvider must be a non-empty normalized string")
    if not isinstance(max_rounds, int) or max_rounds < 1:
        raise TypeError("maxRounds must be a positive safe integer")
    if not isinstance(max_handoff_chars, int) or max_handoff_chars < 1:
        raise TypeError("maxHandoffChars must be a positive safe integer")
    if not isinstance(max_result_chars, int) or max_result_chars < 1:
        raise TypeError("maxResultChars must be a positive safe integer")
    return {
        "subagentProvider": subagent_provider,
        "maxRounds": max_rounds,
        "maxHandoffChars": max_handoff_chars,
        "maxResultChars": max_result_chars,
    }


def resolve_max_rounds(requested: Any, ceiling: int) -> int:
    """把一个模型选定的上限解析到部署天花板内。"""
    value = int(requested) if requested is not None else ceiling
    if not isinstance(value, int) or value < 1:
        raise TypeError("Ralph maxRounds must be a positive safe integer")
    if value > ceiling:
        raise TypeError(f"Ralph maxRounds {value} exceeds the deployment ceiling {ceiling}")
    return value


def require_fresh_provider(ctx: AppContext, provider_name: str) -> Any:
    """要求配置的路由真的意味着全新结构化子。"""
    provider = ctx.subagents.get_provider(provider_name)
    if provider is None:
        raise RuntimeError(f'Ralph subagent provider "{provider_name}" is not registered')
    if not provider.capabilities.output_schema:
        raise RuntimeError(f'Ralph subagent provider "{provider_name}" does not support structured output')
    if provider.inherits_parent_context:
        raise RuntimeError(
            f'Ralph subagent provider "{provider_name}" inherits parent context; Ralph requires a fresh provider'
        )
    return provider


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _normalized_text(value: Any) -> bool:
    return isinstance(value, str) and len(value) > 0 and value == value.strip()


def _normalized_list(value: Any) -> bool:
    return isinstance(value, list) and all(_normalized_text(v) for v in value)


def _read_report(value: Any, expected_status: str, max_chars: int) -> dict:
    """防御性解码固定脚本跨 provider 边界的 report。"""
    if (
        not _is_record(value)
        or sorted(value.keys()) != ["blocker", "evidence", "nextSteps", "status", "summary"]
        or value["status"] != expected_status
        or not _normalized_text(value["summary"])
        or not _normalized_list(value["evidence"])
        or not _normalized_list(value["nextSteps"])
        or not isinstance(value["blocker"], str)
        or value["blocker"] != value["blocker"].strip()
    ):
        raise RuntimeError("Ralph workflow returned a malformed round report")
    report = {
        "status": expected_status,
        "summary": value["summary"],
        "evidence": value["evidence"],
        "nextSteps": value["nextSteps"],
        "blocker": value["blocker"],
    }
    if expected_status == "continue" and (len(report["nextSteps"]) == 0 or report["blocker"] != ""):
        raise RuntimeError("Ralph workflow returned an invalid continuing report")
    if expected_status == "complete" and (
        len(report["evidence"]) == 0 or len(report["nextSteps"]) != 0 or report["blocker"] != ""
    ):
        raise RuntimeError("Ralph workflow returned an invalid completion report")
    if expected_status == "blocked" and not _normalized_text(report["blocker"]):
        raise RuntimeError("Ralph workflow returned an invalid blocked report")
    chars = len(json.dumps(report))
    if chars > max_chars:
        raise RuntimeError(f"Ralph workflow returned an oversized handoff ({chars} > {max_chars})")
    return report


def _read_run_result(value: Any, max_rounds: int, max_handoff_chars: int) -> dict:
    """防御性解码固定脚本的终态值。"""
    if (
        not _is_record(value)
        or not isinstance(value.get("roundsStarted"), int)
        or value["roundsStarted"] < 1
        or value["roundsStarted"] > max_rounds
    ):
        raise RuntimeError("Ralph workflow returned a malformed terminal result")
    rounds_started = value["roundsStarted"]
    status = value.get("status")
    if status == "complete":
        if sorted(value.keys()) != ["report", "roundsStarted", "status"]:
            raise RuntimeError("Ralph workflow returned a malformed terminal result")
        return {"status": "complete", "roundsStarted": rounds_started, "report": _read_report(value["report"], "complete", max_handoff_chars)}
    if status == "blocked":
        if sorted(value.keys()) != ["report", "roundsStarted", "status"]:
            raise RuntimeError("Ralph workflow returned a malformed terminal result")
        return {"status": "blocked", "roundsStarted": rounds_started, "report": _read_report(value["report"], "blocked", max_handoff_chars)}
    if status == "budget-limited":
        if sorted(value.keys()) != ["report", "roundsStarted", "status"]:
            raise RuntimeError("Ralph workflow returned a malformed terminal result")
        if rounds_started != max_rounds:
            raise RuntimeError("Ralph workflow returned budget-limited before the round limit")
        return {"status": "budget-limited", "roundsStarted": rounds_started, "report": _read_report(value["report"], "continue", max_handoff_chars)}
    if status == "round-failed":
        if sorted(value.keys()) != ["lastReport", "roundsStarted", "status"]:
            raise RuntimeError("Ralph workflow returned a malformed terminal result")
        if rounds_started == 1:
            if value["lastReport"] is not None:
                raise RuntimeError("Ralph workflow returned an invalid first-round failure")
            return {"status": "round-failed", "roundsStarted": rounds_started}
        if value["lastReport"] is None:
            raise RuntimeError("Ralph workflow returned a round failure without its last handoff")
        return {
            "status": "round-failed",
            "roundsStarted": rounds_started,
            "lastReport": _read_report(value["lastReport"], "continue", max_handoff_chars),
        }
    raise RuntimeError("Ralph workflow returned an unknown terminal status")


def stop_reason_error(result: Any) -> str | None:
    """非干净的 workflow 结束是错误，绝不是部分 Ralph 成功。"""
    if result.stopReason == "completed":
        return None
    if result.stopReason == "cancelled":
        return f"Ralph workflow was cancelled{(' (' + result.error + ')') if result.error is not None else ''}"
    if result.stopReason == "error":
        return f"Ralph workflow failed: {result.error if result.error is not None else 'unknown error'}"
    return f"Ralph workflow ended abnormally ({result.stopReason})"


TRUNCATION_NOTICE = "\n… [truncated]"


def bound_result(text: str, max_chars: int) -> str:
    """封顶完整的父侧文本，含信封与截断标记。"""
    if len(text) <= max_chars:
        return text
    if max_chars <= len(TRUNCATION_NOTICE):
        return TRUNCATION_NOTICE[:max_chars]
    return f"{text[: max_chars - len(TRUNCATION_NOTICE)]}{TRUNCATION_NOTICE}"


def render_result(result: dict, max_chars: int) -> str:
    """渲染固定终态信封（不把自我报告当作认证）。"""
    rounds = f"{result['roundsStarted']} round{'' if result['roundsStarted'] == 1 else 's'}"
    report = json.dumps(result["report"], ensure_ascii=False, indent=2)
    if result["status"] == "complete":
        text = f"Ralph worker reported completion after {rounds}.\nFinal report:\n{report}"
    elif result["status"] == "blocked":
        text = f"Ralph worker reported a blocker after {rounds}.\nFinal report:\n{report}"
    else:  # budget-limited
        text = f"Ralph reached its {rounds} limit; the worker reported work remaining.\nFinal report:\n{report}"
    return bound_result(text, max_chars)


def render_round_failure(result: dict, max_chars: int) -> str:
    """渲染普通子失败与最近一个耐久 handoff。"""
    header = f"Ralph round {result['roundsStarted']} child failed before producing a structured report."
    text = (
        f"{header}\nNo previous handoff was available."
        if "lastReport" not in result
        else f"{header}\nLast successful handoff:\n{json.dumps(result['lastReport'], ensure_ascii=False, indent=2)}"
    )
    return bound_result(text, max_chars)


def apply(ctx: AppContext, config: Any = None) -> None:
    resolved = resolve_config(config)

    if ctx.has_service("systemPrompt"):
        ctx.systemPrompt.section({
            "name": "tool:ralph",
            "order": 116,
            "text": (
                "Use the ralph tool ONLY when the direct human explicitly asks for a Ralph loop or "
                "fresh-agent iterative execution. Each Ralph round starts a fresh child with no conversation "
                "seed and uses the shared workspace as durable memory. Completion and blockers are worker "
                "reports, not independent evaluation. Use same-session goal tools for ordinary long-running "
                "objectives, and plain subagents or workflows for bounded delegation and fan-out."
            ),
        })

    parameters = {
        "type": "object",
        "properties": {
            "objective": {
                "type": "string",
                "required": True,
                "description": "The immutable completion objective for every fresh Ralph round.",
            },
            "maxRounds": {
                "type": "number",
                "description": "Optional positive safe-integer round cap, bounded by the deployment ceiling.",
            },
        },
        "required": ["objective"],
    }

    async def handler(args: dict, exec: dict) -> tuple[str, bool]:
        parent = exec.get("agent")
        if parent is None:
            return "Ralph tool requires a calling agent (exec.agent was undefined)", True
        objective = str(args.get("objective", "")).strip()
        if not objective:
            return "Ralph objective must be a non-empty string", True
        try:
            max_rounds = resolve_max_rounds(args.get("maxRounds"), resolved["maxRounds"])
            require_fresh_provider(ctx, resolved["subagentProvider"])
        except Exception as error:  # noqa: BLE001 - 配置/路由错误归为工具错误
            return str(error), True

        try:
            run = ctx.workflowEngine.start({
                "script": RALPH_SCRIPT,
                "meta": RALPH_META,
                "args": {"objective": objective, "maxRounds": max_rounds, "maxHandoffChars": resolved["maxHandoffChars"]},
                "subagentProvider": resolved["subagentProvider"],
                "maxTotalAgents": max_rounds,
                "parent": parent,
                "signal": exec.get("signal"),
            })
        except Exception as error:  # noqa: BLE001 - 同步启动失败
            return f"Ralph workflow start failed: {error}", True

        remove = None
        signal = exec.get("signal")
        if signal is not None:
            remove = signal.add_listener(lambda: run.cancel("parent step aborted"))
            if signal.aborted:
                run.cancel("parent step aborted")

        try:
            settled = await run.result
            error = stop_reason_error(settled)
            if error is not None:
                return error, True
            value = _read_run_result(settled.value, max_rounds, resolved["maxHandoffChars"])
            if value["status"] == "round-failed":
                return render_round_failure(value, resolved["maxResultChars"]), True
            return render_result(value, resolved["maxResultChars"]), False
        finally:
            if remove is not None:
                remove()
            await run.dispose()

    ctx.tools.register("ralph", DESCRIPTION, parameters, handler)


apply.name = name
apply.inject = inject

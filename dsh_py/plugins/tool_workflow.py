"""模型侧 ``workflow`` 工具（对标 dsh 的 ``@deepseek-ai/dsh-tool-workflow``）。

运行一个 **Python** 编排脚本（脚本语言由 JS 重新定向，见 engine docstring）
扇出子代理，返回脚本的最终值。它拥有模型侧 schema 与运行生命周期；脚本
解析、执行、上限与取消都在 ``ctx.workflowEngine``（seam）背后，换更硬的引擎
不触碰模型所见。执行 await ``run.result`` 并总是 dispose 运行；非 completed
原因变成工具错误；运行被记录成调用父 Session 的耐久事件（
``tool-workflow/run-start|agent-start|agent-end|run-end``）。

**与 dsh 差异**：dsh 经 ``output.schema/render`` 返回结构化 ``{runId,
agentsStarted, result}`` 并渲染卡片；dsh_py 工具是 ``(text, is_error)`` 契约，
直接返回渲染文本（模型所见等价）。``presentCall/presentResult`` 展示挂钩
dsh_py 无对应缝，省略。``recordsRun`` 的「仅顶层运行记录」判定依赖 dsh 的
``exec.parent``；dsh_py 的 exec 无 parent 字段，一律记录（已文档化）。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from dsh_py.core.context import AppContext

name = "tool-workflow"
inject = ["tools", "workflowEngine", "systemPrompt"]

#: 渲染文本的字符上限（更长的 JSON 值截断并加提示）。
DEFAULT_MAX_RESULT_CHARS = 50_000

#: 脚本作者契约，嵌入工具描述。这是模型侧规格：meta 块、hooks 与其精确语义、
#: 受支持的 schema 子集。脚本语言为 **Python**（dsh_py 重新定向）。
DESCRIPTION = """Run a Python workflow script that orchestrates subagents at scale. Use this for work that fans out across many independent pieces — an audit over many files, a migration, multi-angle research, adversarial verification of findings — where you write the orchestration as a script instead of delegating turn by turn.

The workflow's identity rides the `meta` parameter as JSON: required `name` (short kebab-case) and `description` strings, optional `whenToUse` string and `phases` array (`{title, detail?, provider?, model?}`). The `script` parameter is the plain Python body ONLY (NOT async-unsafe code; no `export const meta` statement — meta is a parameter, not code), running with top-level await; end with `return <value>` — the value must be JSON-serializable and is this tool's result.

Script-body hooks (globals):
- `agent(prompt, opts?) -> Promise` — run one subagent to completion. Without `opts.schema` it resolves to the child's final text; with `opts.schema` (an object-rooted JSON Schema using ONLY type/properties/required/additionalProperties/items/enum/const/oneOf — no pattern/format/numeric bounds) it resolves to the validated object. Resolves `None` when the child fails (filter with `.filter(Boolean)` equivalents, e.g. `[x for x in items if x is not None]`). Other opts: `label` (display), `phase` (progress group), and independent `provider`/`model` LLM target overrides (either may be provided alone). Anything else (`effort`/`isolation`/`agentType`) is rejected loudly.
- `pipeline(items, *stages) -> Promise` — run each item through the stages independently with NO barrier between stages (prefer this for multi-stage work). Each stage receives `(prev, item, index)`. An ordinary stage throw drops that ITEM to `None` and skips its remaining stages.
- `parallel(thunks) -> Promise` — run zero-argument functions concurrently and await ALL of them (a barrier; use only when a stage genuinely needs every prior result together). A throwing thunk resolves to `None`.
- `phase(title)` — start a progress phase; `log(message)` — narrate progress; `args` — the tool call's `args` input, verbatim (e.g. `args["files"]`).

Misused hooks (bad arguments, unknown options, unsupported schemas, tripped caps) throw errors that ALWAYS kill the script — they never dissolve into a per-item `None`.

Constraints: concurrency and total-agent caps apply; the executor is trusted (not a sandbox — the agents do the work, the script only coordinates them). The run executes in the foreground: this call returns when the whole script finishes."""


def _render_recording_error(error: Any) -> str:
    try:
        return str(error)
    except Exception:  # noqa: BLE001
        return "[unrenderable thrown value]"


def create_workflow_recorder(ctx: AppContext) -> dict:
    """把活跃的顶层 workflow 运行投影到其父 Session（记录失败不影响工具执行）。"""
    active: dict = {}

    def append(session: Any, event_type: str, data: dict) -> bool:
        try:
            session.append(event_type, data)
            return True
        except Exception as error:  # noqa: BLE001 - 记录失败禁用耐久记录
            ctx.logger.warn(
                f"tool-workflow: disabled durable record after {event_type} append failed: {_render_recording_error(error)}"
            )
            return False

    def on_agent_start(info: Any, agent: Any) -> None:
        session = active.get(info.id)
        if session is None:
            return
        data = {
            "runId": info.id,
            "seq": agent.seq,
            "label": agent.label,
            **({"phase": agent.phase} if agent.phase is not None else {}),
            "childId": agent.childId,
        }
        if not append(session, "tool-workflow/agent-start", data):
            active.pop(info.id, None)

    def on_agent_end(info: Any, agent: Any) -> None:
        session = active.get(info.id)
        if session is None:
            return
        data = {"runId": info.id, "seq": agent.seq, "outcome": agent.outcome}
        if not append(session, "tool-workflow/agent-end", data):
            active.pop(info.id, None)

    ctx.on("workflow/agent-start", on_agent_start, global_=True)
    ctx.on("workflow/agent-end", on_agent_end, global_=True)

    def start(session: Any, run: Any) -> None:
        if append(session, "tool-workflow/run-start", {"runId": run.id, "name": run.meta.name}):
            active[run.id] = session

    def finish(run_id: Any, stop_reason: str) -> None:
        session = active.get(run_id)
        if session is not None:
            append(session, "tool-workflow/run-end", {"runId": run_id, "stopReason": stop_reason})
        active.pop(run_id, None)

    def abandon(run_id: Any) -> None:
        active.pop(run_id, None)

    return {"start": start, "finish": finish, "abandon": abandon}


def stop_reason_error(result: Any) -> str | None:
    """非 ``completed`` 的 stop reason 意味着脚本没有干净结束。"""
    if result.stopReason == "completed":
        return None
    if result.stopReason == "cancelled":
        return f"workflow run was cancelled{(' (' + result.error + ')') if result.error is not None else ''}"
    if result.stopReason == "error":
        return f"workflow run failed: {result.error if result.error is not None else 'unknown error'}"
    return f"workflow run ended abnormally ({result.stopReason})"


def render_result(name: str, agents_started: int, value: Any, max_chars: int) -> str:
    """渲染运行结果文本：meta 名、agent 数与 JSON 值（封顶）。"""
    try:
        rendered = json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001 - 物化后的值应当可序列化，防御兜底
        rendered = json.dumps({"value": repr(value)}, ensure_ascii=False)
    clipped = (
        f"{rendered[:max_chars]}\n… [truncated: {len(rendered) - max_chars} more characters]"
        if len(rendered) > max_chars
        else rendered
    )
    return (
        f'workflow "{name}" completed ({agents_started} agent{"" if agents_started == 1 else "s"}).\n'
        f"Return value:\n{clipped}"
    )


def apply(ctx: AppContext, config: Any = None) -> None:
    config = config or {}
    tool_name = config.get("toolName", "workflow")
    max_result_chars = int(config.get("maxResultChars", DEFAULT_MAX_RESULT_CHARS) or DEFAULT_MAX_RESULT_CHARS)
    recorder = create_workflow_recorder(ctx)

    # 用法策略随工具发布（master 约定：工具指导以 prompt section 形式存在）。
    if ctx.has_service("systemPrompt"):
        ctx.systemPrompt.section({
            "name": f"tool:{tool_name}",
            "order": 115,
            "text": (
                f"Use the {tool_name} tool ONLY when the user explicitly asks for a workflow or for large "
                "multi-agent orchestration: you write a Python script (the tool description documents the exact "
                "format) that fans work out across many subagents with phases and structured results. For one or "
                "two delegations, prefer plain subagent calls."
            ),
        })

    parameters = {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "required": True,
                "description": "The plain-Python workflow script body (top-level await allowed; NO `export const meta` statement; end with `return <json-value>`).",
            },
            "meta": {
                "type": "object",
                "additionalProperties": True,
                "description": "The workflow identity block (plain JSON — never code).",
                "properties": {
                    "name": {"type": "string", "required": True, "description": "Short kebab-case workflow name."},
                    "description": {"type": "string", "required": True, "description": "One-line description of what the workflow does."},
                    "whenToUse": {"type": "string", "description": "Optional guidance on when this workflow applies."},
                    "phases": {
                        "type": "array",
                        "description": "Optional phase declarations matched by phase() calls.",
                        "items": {
                            "type": "object",
                            "additionalProperties": True,
                            "properties": {
                                "title": {"type": "string", "required": True, "description": "The phase title phase() calls match by exact string."},
                                "detail": {"type": "string", "description": "Optional one-line description of the phase."},
                                "provider": {"type": "string", "description": "Optional provider override this phase is expected to use."},
                                "model": {"type": "string", "description": "Optional model override this phase is expected to use."},
                            },
                        },
                    },
                },
            },
            "args": {
                "type": "object",
                "additionalProperties": True,
                "description": "Optional JSON input exposed to the script as the `args` global (wrap a bare list as a field, e.g. {\"files\": [...]}).",
            },
        },
        "required": ["script", "meta"],
    }

    async def handler(args: dict, exec: dict) -> tuple[str, bool]:
        parent = exec.get("agent")
        if not parent:
            return "workflow tool requires a calling agent (exec.agent was undefined)", True

        # meta/body 校验失败（META_INVALID/SCRIPT_PARSE）在这里同步抛出，被
        # execute_with_agent 归为 isError——模型看到违规清单并纠正调用。
        try:
            run = ctx.workflowEngine.start({
                "script": args["script"],
                "meta": args["meta"],
                **({"args": args["args"]} if args.get("args") is not None else {}),
                "parent": parent,
                "signal": exec.get("signal"),
            })
        except Exception as error:  # noqa: BLE001 - 同步启动失败归为工具错误
            return f"workflow start failed: {error}", True

        records_run = True  # 差异：dsh 用 exec.parent 判定顶层；dsh_py 无该字段，一律记录
        if records_run:
            recorder["start"](parent.session, run)

        # 桥接工具的 abort 信号到运行：父步骤被中止时取消整个运行。信号也直接
        # 进入引擎，但本地桥保持工具契约，即使某个实现忽略它。
        remove = None
        signal = exec.get("signal")
        if signal is not None:
            remove = signal.add_listener(lambda: run.cancel("parent step aborted"))
            if signal.aborted:
                run.cancel("parent step aborted")

        result = None
        try:
            result = await run.result
            error = stop_reason_error(result)
            if error is not None:
                return error, True
            return render_result(run.meta.name, result.agentsStarted, result.value, max_result_chars), False
        finally:
            if remove is not None:
                remove()
            try:
                # 保持成员监听器活着直到释放完成：引擎在达到静默时可能合成被
                # 取消的成员结束。
                await run.dispose()
                if records_run:
                    if result is not None:
                        recorder["finish"](run.id, result.stopReason)
            finally:
                if records_run:
                    recorder["abandon"](run.id)

    ctx.tools.register(tool_name, DESCRIPTION, parameters, handler)


apply.name = name
apply.inject = inject

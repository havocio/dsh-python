"""通用钩子桥（hooks，治理类；对标 dsh 的 hook-protocol 桥）。

把 dsh_py 的拦截点接到**外部命令钩子**（经 ``hooks_protocol`` 的 ``run_hook`` /
``parse_hook_output`` / ``merge_hook_outputs`` 复用 CC/Codex 同款协议）：

- ``PreToolUse``（``tools/execute``）：命中的钩子返回 ``continue:false``/``decision:
  block|deny`` 时**否决**该工具调用——返回拒绝文本给模型，工具本体不执行；
- ``PostToolUse``（``tools/post-execute``）：命中的钩子可返回 ``additionalContext``，
  折叠进下一 step 的附加上下文；
- ``Stop``（``agent/status`` 空闲）：分离（非阻塞）运行，仅经 ``hook/invoked`` /
  ``hook/result`` 事件留痕，不阻塞 turn。

配置采用扁平的钩子组：每组含 ``point``（拦截点）、``matcher``（工具名模式，仅
PreToolUse/PostToolUse 生效；``*``/空 = 匹配全部）、``hooks``（命令列表）。``dialect``
决定匹配模式（``claude-code`` 用 literal 管道交替，其余用 regex）。
"""

from __future__ import annotations

import time
from typing import Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.services.hooks_protocol import (
    DEFAULT_HOOK_TIMEOUT_MS,
    CommandHook,
    DetachedRuns,
    MatcherMode,
    RunHookOptions,
    append_hook_invoked,
    append_hook_result,
    matches_matcher,
    merge_hook_outputs,
    run_hook,
    summarize_stderr,
)
from dsh_py.services.message import MessageSource, TextBlock, create_user_message


# 通用桥写入 hook/* 事件的方言标记
DIALECT = "generic"

# 注入的提醒消息来源标签
PLUGIN_SOURCE = MessageSource("plugin", plugin="hooks", form="notice")


def _now() -> int:
    return int(time.time() * 1000)


Config = z.object(
    {
        "hooks": z.array(z.object(
            {
                "point": z.string().default("PreToolUse"),
                "matcher": z.string().optional(),
                "hooks": z.array(z.object(
                    {
                        "command": z.string(),
                        "timeout_sec": z.number().optional(),
                    }
                )).default([]),
            }
        )).default([]),
        "dialect": z.string().default("generic"),
        "default_timeout_ms": z.number().default(DEFAULT_HOOK_TIMEOUT_MS),
        "points": z.array(z.string()).default(["PreToolUse", "PostToolUse", "Stop"]),
    },
    extra="strip",
)


def apply(ctx: AppContext, config: dict | None = None) -> None:
    """安装通用钩子桥，按配置把拦截点接到外部命令钩子。"""
    config = config or {}
    points = set(config.get("points", ["PreToolUse", "PostToolUse", "Stop"]))
    mode: MatcherMode = "claude-code" if config.get("dialect") == "claude-code" else "codex"
    default_timeout_ms = int(config.get("default_timeout_ms", DEFAULT_HOOK_TIMEOUT_MS))

    # 按 point 分组的钩子组（保留 matcher）
    groups_by_point: dict[str, list] = {}
    for group in config.get("hooks", []):
        groups_by_point.setdefault(group.get("point", "PreToolUse"), []).append(group)

    detached = DetachedRuns()

    def matched_groups(point: str, tool_name: Optional[str]) -> list:
        result = []
        for group in groups_by_point.get(point, []):
            matcher = group.get("matcher")
            if tool_name is None or matches_matcher(matcher, tool_name, mode):
                result.append(group)
        return result

    def build_payload(point: str, exec: Optional[dict], tool_result_text: Optional[str]) -> dict:
        """构建写入钩子 stdin 的信任载荷（对齐 CC 的 PreToolUse/PostToolUse/Stop 形态）。"""
        if point == "PreToolUse" and exec is not None:
            try:
                import json
                tool_input = json.loads(exec.get("arguments") or "{}")
            except json.JSONDecodeError:
                tool_input = {}
            return {"tool_name": exec.get("name"), "tool_input": tool_input, "tool_use_id": ""}
        if point == "PostToolUse" and exec is not None:
            try:
                import json
                tool_input = json.loads(exec.get("arguments") or "{}")
            except json.JSONDecodeError:
                tool_input = {}
            return {
                "tool_name": exec.get("name"), "tool_input": tool_input,
                "tool_response": tool_result_text or "", "tool_use_id": "",
            }
        if point == "Stop":
            return {"stop_hook_input": []}
        return {}

    async def run_point(point: str, exec: Optional[dict], tool_result_text: Optional[str], session, turn: int):
        """运行某拦截点命中的全部钩子，返回合并结果（含附加上下文文本列表）。"""
        groups = matched_groups(point, exec.get("name") if exec else None)
        if not groups:
            from dsh_py.services.hooks_protocol import HookOutput
            return [], []
        cwd = getattr(getattr(session, "header", None), "cwd", None)
        additional_texts: list = []
        invoked_records: list = []
        for gi, group in enumerate(groups):
            for hi, hspec in enumerate(group.get("hooks", [])):
                hook = CommandHook(command=hspec["command"], timeout_sec=hspec.get("timeout_sec"))
                handler_id = f"{point}-{gi}-{hi}"
                append_hook_invoked(ctx, session, turn, point, DIALECT, handler_id, group.get("matcher"))
                options = RunHookOptions(
                    payload=build_payload(point, exec, tool_result_text),
                    cwd=cwd,
                    trailing_newline=(mode == "claude-code"),
                    default_timeout_ms=default_timeout_ms,
                    expected_event_name=point,
                )
                result = await run_hook(ctx.shell, hook, options, _now)
                outcome = result.output
                invoked_records.append((handler_id, outcome))
                if outcome.additional_context:
                    additional_texts.append(outcome.additional_context)
        # 合并 + 写 hook/result
        outputs = [rec[1] for rec in invoked_records]
        merged = merge_hook_outputs(outputs)
        for handler_id, outcome in invoked_records:
            append_hook_result(
                ctx, session, turn, point, handler_id,
                decision=merged.decision if outcome is outputs[0] else ("block" if (outcome.continue_flag is False or outcome.decision in ("block", "deny")) else "pass"),
                duration_ms=0,
                exit_code=outcome.exit_code,
                stderr_summary=summarize_stderr(outcome.stderr) if outcome.stderr else None,
            )
        return merged, additional_texts

    # PreToolUse：否决则拒绝工具（不执行），否则 delegate
    if "PreToolUse" in points:
        @ctx.on("tools/execute")
        async def on_pre_tool_use(event, next):
            exec = event["exec"]
            agent = exec.get("agent")
            session = getattr(agent, "session", None) if agent else None
            turn = _latest_turn(session)
            merged, _ = await run_point("PreToolUse", exec, None, session, turn)
            if merged.decision == "block":
                reason = merged.block_reason or "hook blocked this tool call"
                return f"Error: tool call blocked by hook: {reason}", True
            return await next()

    # PostToolUse：收集 additionalContext 注入下一 step
    if "PostToolUse" in points:
        @ctx.on("tools/post-execute")
        async def on_post_tool_use(event, next):
            exec = event["exec"]
            agent = exec.get("agent")
            session = getattr(agent, "session", None) if agent else None
            turn = _latest_turn(session)
            result = event.get("result") or {}
            tool_text = ""
            for block in (result.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "text":
                    tool_text = block.get("text", "")
            merged, additional_texts = await run_point("PostToolUse", exec, tool_text, session, turn)
            downstream = await next()
            contexts = [create_user_message([TextBlock(t)], source=PLUGIN_SOURCE) for t in additional_texts]
            if merged.system_messages:
                # 系统告警：作为 notice 注入第一条（克制——仅展现首个）
                contexts.append(create_user_message(
                    [TextBlock(merged.system_messages[0])], source=PLUGIN_SOURCE))
            base = downstream.get("additionalContexts", []) if isinstance(downstream, dict) else []
            return {**(downstream if isinstance(downstream, dict) else {}), "additionalContexts": [*base, *contexts]}

    # Stop：agent 空闲时分离运行（仅留痕，不阻塞）
    if "Stop" in points:
        @ctx.on("agent/status")
        async def on_agent_status(event, next=None):
            payload = event if isinstance(event, dict) else {}
            if payload.get("status") != "idle":
                if next is not None:
                    return await next()
                return None
            agent = payload.get("agent")
            session = getattr(agent, "session", None) if agent else None
            if session is None or not groups_by_point.get("Stop"):
                if next is not None:
                    return await next()
                return None
            turn = _latest_turn(session)

            async def _run_stop():
                await run_point("Stop", None, None, session, turn)

            detached.submit(_run_stop())
            if next is not None:
                return await next()
            return None


def _latest_turn(session) -> int:
    """从会话事件中取当前最新 turn 号（供 hook/* 事件留痕）。"""
    if session is None:
        return 0
    last = 0
    for ev in getattr(session, "events", []):
        if ev.type == "turn/start":
            last = ev.data.get("turn", last)
    return last


apply.Config = Config
apply.name = "hooks"
apply.inject = ["shell", "tools"]

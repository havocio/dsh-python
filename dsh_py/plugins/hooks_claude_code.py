"""Claude Code 钩子桥（hooks-claude-code，治理类；对标 dsh 的 hooks-claude-code）。

读取一份**未经修改的** Claude Code ``hooks.json``（或 settings 的 ``hooks`` 段），把其 7 个事件
映射到 harness 拦截点，复用 ``hooks_protocol`` 的共享执行/解析引擎。桥自身拥有 CC 载荷形态、
环境变量、命令替换与决策映射；共享执行与解析在 ``hooks_protocol`` / ``hooks_config``。

- ``SessionStart`` → ``agent/session-start``：分离（非阻塞）运行，结果解析后注入会话（经 ``agent.insert``）。
- ``UserPromptSubmit`` → ``agent/pre-step``：``decision`` 为 deny/ask → 拒绝进入；否则把上下文并入下一步消息。
- ``PreToolUse`` → ``tools/execute``：deny/ask → 拒绝执行工具（短路，工具本体不运行）。
- ``PostToolUse`` → ``tools/post-execute``：把 ``additionalContext``/``systemMessage`` 折叠为下一 step 附加上下文。
- ``Stop`` → ``agent/status``(idle)：分离运行；阻断性 Stop 钩子经 ``agent.followup`` 强制继续（尽力而为）。
- ``SubagentStart``/``SubagentStop``：dsh_py 尚无 ``subagent/start``/``subagent/end`` 拦截点，仅解析、加载时告警，不会触发。

命令串支持 ``${CLAUDE_PLUGIN_ROOT}`` / ``${CLAUDE_PROJECT_DIR}`` 替换；``CLAUDE_PROJECT_DIR`` 同时作为
钩子进程环境变量导出（缺省取 agent 会话工作目录）。stdin 载荷带尾随换行（CC 约定）。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.services.hooks_config import parse_claude_code_config
from dsh_py.services.hooks_protocol import (
    DEFAULT_HOOK_TIMEOUT_MS,
    DEFAULT_STDERR_SUMMARY_MAX_CHARS,
    run_hook_point,
)
from dsh_py.services.message import MessageSource, TextBlock, create_user_message

# 本桥写入 hook/* 事件的方言标记
DIALECT = "claude-code"
# 注入上下文的来源标签
PLUGIN_SOURCE = MessageSource("plugin", plugin="hooks-claude-code")

# 无 matcher 主体的事件
_NO_MATCHER_EVENTS = {"UserPromptSubmit", "Stop"}


def _assert_positive_int(name: str, value: Any) -> None:
    """摘要上限/超时必须是正整数，否则配置无效。"""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"hooks-claude-code: {name} 必须是正整数")


Config = z.object(
    {
        "configPath": z.string(),  # 必填：hooks.json / settings 路径
        "pluginRoot": z.string().optional(),
        "projectDir": z.string().optional(),
        "defaultTimeoutMs": z.number().default(DEFAULT_HOOK_TIMEOUT_MS),
        "stderrSummaryMaxChars": z.number().default(DEFAULT_STDERR_SUMMARY_MAX_CHARS),
    },
    extra="strip",
)


def apply(ctx: AppContext, config: dict | None = None) -> None:
    """安装 Claude Code 钩子桥：读取配置、解析、把各事件接到 harness 拦截点。"""
    config = config or {}
    stderr_cap = config.get("stderrSummaryMaxChars", DEFAULT_STDERR_SUMMARY_MAX_CHARS)
    _assert_positive_int("stderrSummaryMaxChars", stderr_cap)
    default_timeout_ms = int(config.get("defaultTimeoutMs", DEFAULT_HOOK_TIMEOUT_MS))
    _assert_positive_int("defaultTimeoutMs", default_timeout_ms)

    config_path = config.get("configPath")
    if not config_path:
        ctx.logger.warn("hooks-claude-code: 缺少 configPath，未注册任何钩子")
        return
    abspath = config_path if os.path.isabs(config_path) else os.path.abspath(config_path)

    try:
        with open(abspath, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception as error:
        ctx.logger.warn(f"hooks-claude-code: 无法加载钩子配置 {abspath}: {error} —— 未注册任何钩子")
        return

    try:
        parsed = parse_claude_code_config(raw, config.get("pluginRoot"), config.get("projectDir"))
    except ValueError as error:
        ctx.logger.warn(f"hooks-claude-code: 钩子配置解析失败：{error} —— 未注册任何钩子")
        return

    for skipped in parsed.skipped:
        ctx.logger.warn(f'hooks-claude-code: 跳过 {skipped.event} 上不支持的 "{skipped.reason}" 钩子（仅 command 钩子运行）')
    if parsed.config.get("SubagentStart") or parsed.config.get("SubagentStop"):
        ctx.logger.warn(
            "hooks-claude-code: 已解析 SubagentStart/SubagentStop 钩子，但 dsh_py 尚无 subagent/start|end 拦截点，将不会触发"
        )

    # 分离运行追踪 + 卸载时取消
    tasks: set = set()

    def _submit(coro):
        task = asyncio.ensure_future(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    ctx.effect(lambda: _drain(tasks), "hooks-claude-code: 终止分离钩子运行")

    counter = {"n": 0}

    def next_handler_id(point: str) -> str:
        counter["n"] += 1
        return f"claude-code:{point}:{counter['n']}"

    plugin_root = config.get("pluginRoot")
    project_dir_cfg = config.get("projectDir")

    # ---- 载荷构建（CC 方言形态）------------------------------------------- #
    def _transcript_path(agent: Any) -> str:
        try:
            sp = getattr(ctx, "sessionPersistence", None)
            if sp is not None and hasattr(sp, "locate"):
                loc = sp.locate(agent.session.header)
                if loc is not None:
                    return getattr(loc, "path", "") or ""
        except Exception:
            pass
        return ""

    def _base(agent: Any, event: str) -> dict:
        header = getattr(getattr(agent, "session", None), "header", None)
        return {
            "session_id": getattr(header, "id", "") if header else "",
            "transcript_path": _transcript_path(agent) if agent is not None else "",
            "cwd": getattr(header, "cwd", os.getcwd()) if header else os.getcwd(),
            "hook_event_name": event,
        }

    def _session_start_payload(agent: Any, source: str) -> dict:
        return {**_base(agent, "SessionStart"), "source": source}

    def _user_prompt_payload(agent: Any, prompt: str) -> dict:
        return {**_base(agent, "UserPromptSubmit"), "prompt": prompt}

    def _pre_tool_payload(exec: dict) -> dict:
        agent = exec.get("agent")
        try:
            tool_input = json.loads(exec.get("arguments") or "{}")
        except json.JSONDecodeError:
            tool_input = {}
        return {**_base(agent, "PreToolUse"), "tool_name": exec.get("name"), "tool_input": tool_input, "tool_use_id": exec.get("callId", "")}

    def _post_tool_payload(exec: dict, result_text: str) -> dict:
        agent = exec.get("agent")
        try:
            tool_input = json.loads(exec.get("arguments") or "{}")
        except json.JSONDecodeError:
            tool_input = {}
        return {**_base(agent, "PostToolUse"), "tool_name": exec.get("name"), "tool_input": tool_input, "tool_use_id": exec.get("callId", ""), "tool_response": result_text}

    def _stop_payload(agent: Any) -> dict:
        return {**_base(agent, "Stop"), "stop_hook_active": False}

    # ---- 上下文消息构建 --------------------------------------------------- #
    def _build_context_message(merged: Any) -> Any:
        texts = list(merged.additional_contexts)
        if merged.system_messages:
            texts.append(merged.system_messages[0])
        if not texts:
            return None
        return create_user_message([TextBlock(t) for t in texts], source=PLUGIN_SOURCE)

    def _last_turn(agent: Any) -> int:
        if agent is None:
            return 0
        session = getattr(agent, "session", None)
        if session is None:
            return 0
        last = 0
        for ev in getattr(session, "events", []):
            if getattr(ev, "type", None) == "turn/start":
                data = getattr(ev, "data", None)
                if isinstance(data, dict):
                    last = data.get("turn", last)
        return last

    def _messages_to_text(messages: list) -> str:
        parts = []
        for message in messages:
            content = getattr(message, "content", None) or []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                elif getattr(block, "type", None) == "text":
                    parts.append(getattr(block, "text", ""))
        return "".join(parts)

    def _result_text(result: dict) -> str:
        content = (result or {}).get("content") or []
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)

    def _opts(agent: Any, turn: Optional[int], signal: Any) -> dict:
        workdir = getattr(getattr(agent, "session", None), "header", None) if agent is not None else None
        project_dir = project_dir_cfg or (getattr(workdir, "cwd", None) if workdir else None)
        return {
            "agent": agent,
            "turn": turn,
            "signal": signal,
            "mode": "claude-code",
            "dialect": DIALECT,
            "trailing_newline": True,
            "default_timeout_ms": default_timeout_ms,
            "stderr_summary_max_chars": stderr_cap,
            "project_dir": project_dir,
            "next_handler_id": next_handler_id,
        }

    # ---- 拦截点接线 ------------------------------------------------------- #
    @ctx.on("agent/session-start")
    def _on_session_start(event: dict) -> None:
        agent = event.get("agent")
        if agent is None:
            return
        source = event.get("source", "")

        async def _run():
            try:
                merged = await run_hook_point(
                    ctx, parsed.config.get("SessionStart", []), "SessionStart", source,
                    _session_start_payload(agent, source), _opts(agent, None, None),
                )
                message = _build_context_message(merged)
                if message is not None:
                    agent.insert(message, "next-turn")
            except Exception as error:
                ctx.logger.warn(f"hooks-claude-code: SessionStart 钩子失败：{error}")

        _submit(_run())

    @ctx.on("agent/pre-step")
    async def _on_pre_step(event: dict, next):
        messages = event.get("messages") or []
        if not messages:
            return await next()
        agent = event.get("agent")
        turn = event.get("turn")
        signal = event.get("signal")
        prompt = _messages_to_text(messages)
        merged = await run_hook_point(
            ctx, parsed.config.get("UserPromptSubmit", []), "UserPromptSubmit", "",
            _user_prompt_payload(agent, prompt), _opts(agent, turn, signal),
        )
        if merged.decision in ("block", "ask"):
            return {"kind": "reject"}
        downstream = await next()
        if not isinstance(downstream, dict) or downstream.get("kind") != "enter":
            return downstream
        message = _build_context_message(merged)
        if message is None:
            return downstream
        return {"kind": "enter", "messages": [*downstream.get("messages", []), message]}

    @ctx.on("tools/execute")
    async def _on_pre_tool(event: dict, next):
        exec = event.get("exec") or {}
        agent = exec.get("agent")
        turn = _last_turn(agent)
        merged = await run_hook_point(
            ctx, parsed.config.get("PreToolUse", []), "PreToolUse", exec.get("name", ""),
            _pre_tool_payload(exec), _opts(agent, turn, exec.get("signal")),
        )
        if merged.decision in ("block", "ask"):
            reason = merged.block_reason or "被 PreToolUse 钩子拦截"
            return f"Error: 工具调用被钩子拦截：{reason}", True
        return await next()

    @ctx.on("tools/post-execute")
    async def _on_post_tool(event: dict, next):
        exec = event.get("exec") or {}
        agent = exec.get("agent")
        turn = _last_turn(agent)
        result = event.get("result") or {}
        merged = await run_hook_point(
            ctx, parsed.config.get("PostToolUse", []), "PostToolUse", exec.get("name", ""),
            _post_tool_payload(exec, _result_text(result)), _opts(agent, turn, exec.get("signal")),
        )
        message = _build_context_message(merged)
        downstream = await next()
        base_contexts = (downstream.get("additionalContexts", []) if isinstance(downstream, dict) else [])
        return {"additionalContexts": [*base_contexts, *( [message] if message else [] )]}

    @ctx.on("agent/status")
    async def _on_status(event: dict, next=None):
        if not isinstance(event, dict) or event.get("status") != "idle":
            if next is not None:
                return await next()
            return None
        agent = event.get("agent")
        if agent is None or not parsed.config.get("Stop"):
            if next is not None:
                return await next()
            return None

        async def _run():
            try:
                merged = await run_hook_point(
                    ctx, parsed.config.get("Stop", []), "Stop", "",
                    _stop_payload(agent), _opts(agent, None, None),
                )
                if merged.decision in ("block", "ask"):
                    text = merged.block_reason or "continue: 被 Stop 钩子拦截"
                    agent.followup(create_user_message([TextBlock(text)], source=PLUGIN_SOURCE))
            except Exception as error:
                ctx.logger.warn(f"hooks-claude-code: Stop 钩子失败：{error}")

        _submit(_run())
        if next is not None:
            return await next()
        return None


def _drain(tasks: set) -> None:
    """卸载时取消仍在进行中的分离钩子运行。"""
    for task in list(tasks):
        if not task.done():
            task.cancel()


apply.Config = Config
apply.name = "hooks-claude-code"
apply.inject = ["shell"]

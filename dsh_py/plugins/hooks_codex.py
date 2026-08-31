"""Codex 钩子桥（hooks-codex，治理类；对标 dsh 的 hooks-codex）。

读取一份**未经修改的** Codex ``hooks.json``，把其 5 个事件映射到 harness 拦截点，复用
``hooks_protocol`` 共享引擎。与 Claude Code 桥的差异（Codex 方言）：

- matcher **永远按正则**解释，无字面快路径；无 ``${...}`` 命令替换；不导出任何钩子环境变量；
- stdin 载荷**不带尾随换行**；干净的非 JSON stdout 折叠为模型上下文（``plainStdoutAsContext``）；
- 载荷为 snake_case，每个事件带 ``model`` 与 ``permission_mode``，turn 作用域事件带 ``turn_id``；
- **仅阻断性决策被兑现**（deny → 拒绝/阻断）；不支持 pre-tool 审批或输入改写；
- 仅 5 个事件（无 Subagent 事件），其余映射同 CC 桥。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.services.hooks_config import parse_codex_config
from dsh_py.services.hooks_protocol import (
    DEFAULT_HOOK_TIMEOUT_MS,
    DEFAULT_STDERR_SUMMARY_MAX_CHARS,
    run_hook_point,
)
from dsh_py.services.message import MessageSource, TextBlock, create_user_message

DIALECT = "codex"
PLUGIN_SOURCE = MessageSource("plugin", plugin="hooks-codex")
_NO_MATCHER_EVENTS = {"UserPromptSubmit", "Stop"}


def _assert_positive_int(name: str, value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"hooks-codex: {name} 必须是正整数")


Config = z.object(
    {
        "configPath": z.string(),  # 必填
        "model": z.string().default(""),
        "defaultTimeoutMs": z.number().default(DEFAULT_HOOK_TIMEOUT_MS),
        "stderrSummaryMaxChars": z.number().default(DEFAULT_STDERR_SUMMARY_MAX_CHARS),
    },
    extra="strip",
)


def apply(ctx: AppContext, config: dict | None = None) -> None:
    """安装 Codex 钩子桥：读取配置、解析、把各事件接到 harness 拦截点。"""
    config = config or {}
    stderr_cap = config.get("stderrSummaryMaxChars", DEFAULT_STDERR_SUMMARY_MAX_CHARS)
    _assert_positive_int("stderrSummaryMaxChars", stderr_cap)
    default_timeout_ms = int(config.get("defaultTimeoutMs", DEFAULT_HOOK_TIMEOUT_MS))
    _assert_positive_int("defaultTimeoutMs", default_timeout_ms)
    model = config.get("model", "")

    config_path = config.get("configPath")
    if not config_path:
        ctx.logger.warn("hooks-codex: 缺少 configPath，未注册任何钩子")
        return
    abspath = config_path if os.path.isabs(config_path) else os.path.abspath(config_path)

    try:
        with open(abspath, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception as error:
        ctx.logger.warn(f"hooks-codex: 无法加载钩子配置 {abspath}: {error} —— 未注册任何钩子")
        return

    try:
        parsed = parse_codex_config(raw)
    except ValueError as error:
        ctx.logger.warn(f"hooks-codex: 钩子配置解析失败：{error} —— 未注册任何钩子")
        return

    for skipped in parsed.skipped:
        ctx.logger.warn(f'hooks-codex: 跳过 {skipped.event} 上不支持的 "{skipped.reason}" 钩子（仅同步 command 钩子运行）')

    tasks: set = set()

    def _submit(coro):
        task = asyncio.ensure_future(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    ctx.effect(lambda: _drain(tasks), "hooks-codex: 终止分离钩子运行")

    counter = {"n": 0}

    def next_handler_id(point: str) -> str:
        counter["n"] += 1
        return f"codex:{point}:{counter['n']}"

    # ---- 载荷构建（Codex 方言形态）--------------------------------------- #
    def _transcript_path(agent: Any) -> Any:
        try:
            sp = getattr(ctx, "sessionPersistence", None)
            if sp is not None and hasattr(sp, "locate"):
                loc = sp.locate(agent.session.header)
                if loc is not None:
                    return getattr(loc, "path", None)
        except Exception:
            pass
        return None

    def _base(agent: Any, event: str) -> dict:
        header = getattr(getattr(agent, "session", None), "header", None)
        return {
            "session_id": getattr(header, "id", "") if header else "",
            "transcript_path": _transcript_path(agent) if agent is not None else None,
            "cwd": getattr(header, "cwd", os.getcwd()) if header else os.getcwd(),
            "hook_event_name": event,
            "model": model,
            "permission_mode": "default",
        }

    def _turn_base(agent: Any, event: str) -> dict:
        return {**_base(agent, event), "turn_id": str(_last_turn(agent))}

    def _session_start_payload(agent: Any, source: str) -> dict:
        return {**_base(agent, "SessionStart"), "source": source}

    def _user_prompt_payload(agent: Any, prompt: str, turn: int) -> dict:
        return {**_base(agent, "UserPromptSubmit"), "turn_id": str(turn), "prompt": prompt}

    def _command_of(exec: dict) -> str:
        try:
            args = json.loads(exec.get("arguments") or "{}")
        except json.JSONDecodeError:
            return ""
        if isinstance(args, dict) and isinstance(args.get("command"), str):
            return args["command"]
        return ""

    def _pre_tool_payload(exec: dict) -> dict:
        agent = exec.get("agent")
        return {**_turn_base(agent, "PreToolUse"), "tool_name": exec.get("name"), "tool_input": {"command": _command_of(exec)}, "tool_use_id": exec.get("callId", "")}

    def _post_tool_payload(exec: dict, result_text: str) -> dict:
        agent = exec.get("agent")
        return {**_turn_base(agent, "PostToolUse"), "tool_name": exec.get("name"), "tool_input": {"command": _command_of(exec)}, "tool_use_id": exec.get("callId", ""), "tool_response": result_text}

    def _stop_payload(agent: Any) -> dict:
        return {**_turn_base(agent, "Stop"), "stop_hook_active": False, "last_assistant_message": None}

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
        return {
            "agent": agent,
            "turn": turn,
            "signal": signal,
            "mode": "codex",
            "dialect": DIALECT,
            "trailing_newline": False,
            "plain_stdout_as_context": True,
            "default_timeout_ms": default_timeout_ms,
            "stderr_summary_max_chars": stderr_cap,
            "project_dir": None,
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
                ctx.logger.warn(f"hooks-codex: SessionStart 钩子失败：{error}")

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
            _user_prompt_payload(agent, prompt, turn if isinstance(turn, int) else _last_turn(agent)),
            _opts(agent, turn, signal),
        )
        if merged.decision in ("block", "deny"):  # Codex 仅兑现阻断性决策
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
        if merged.decision in ("block", "deny"):
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
                if merged.decision in ("block", "deny"):
                    text = merged.block_reason or "continue: 被 Stop 钩子拦截"
                    agent.followup(create_user_message([TextBlock(text)], source=PLUGIN_SOURCE))
            except Exception as error:
                ctx.logger.warn(f"hooks-codex: Stop 钩子失败：{error}")

        _submit(_run())
        if next is not None:
            return await next()
        return None


def _drain(tasks: set) -> None:
    for task in list(tasks):
        if not task.done():
            task.cancel()


apply.Config = Config
apply.name = "hooks-codex"
apply.inject = ["shell"]

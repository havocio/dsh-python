"""钩子配置解析（hooks-config，方言特有；对标 dsh 各桥的 config.ts）。

把 Claude Code / Codex 的 ``hooks.json``（或 settings 的 ``hooks`` 段）解析为共享的
:class:`MatcherGroup` 列表，按事件名索引。两方言差异：

- **Claude Code**：7 个事件（含 ``SubagentStart``/``SubagentStop``）；``${CLAUDE_PLUGIN_ROOT}`` /
  ``${CLAUDE_PROJECT_DIR}`` 命令替换；matcher 在纯 ``[A-Za-z0-9_|]+`` 时按字面（管道=交替），
  否则按正则；``UserPromptSubmit``/``Stop`` 无 matcher 主体。
- **Codex**：5 个事件；不做命令替换；matcher 永远按正则；跳过 ``async:true`` 与一切非
  ``command`` 钩子；超时接受 ``timeout`` 或 ``timeoutSec`` 别名。

两方言都**宽容**：未知事件/畸形条目被忽略而非启动失败；非法正则（matcher 命中且需编译）抛
``ValueError``，让桥在注册监听器前整体拒绝该配置。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from dsh_py.services.hooks_protocol import CommandHook, MatcherGroup


# Claude Code 支持的 7 个钩子事件（含子代理起止）
CLAUDE_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SubagentStart",
    "SubagentStop",
]

# Codex 支持的 5 个钩子事件
CODEX_EVENTS = [
    "PreToolUse",
    "PostToolUse",
    "SessionStart",
    "UserPromptSubmit",
    "Stop",
]

# 这些事件没有 matcher 主体（按全匹配处理），解析时丢弃其 matcher
_NO_MATCHER_EVENTS = {"UserPromptSubmit", "Stop"}


@dataclass
class SkippedHook:
    """一个被跳过（不支持）的钩子，供桥告警。"""

    event: str
    reason: str


@dataclass
class ParsedClaudeConfig:
    """Claude Code 配置解析结果。"""

    config: dict  # event -> list[MatcherGroup]
    skipped: list


@dataclass
class ParsedCodexConfig:
    """Codex 配置解析结果。"""

    config: dict  # event -> list[MatcherGroup]
    skipped: list


def _as_object(value: Any) -> Optional[dict]:
    """返回纯对象（dict，非 list/None），否则 None。"""
    return value if isinstance(value, dict) else None


def substitute_command(command: str, plugin_root: Optional[str], project_dir: Optional[str]) -> str:
    """对命令串做 ``${CLAUDE_PLUGIN_ROOT}`` / ``${CLAUDE_PROJECT_DIR}`` 替换。

    未设置的变量保持原样（不替换为空，避免破坏命令结构）。
    """
    out = command
    if plugin_root is not None:
        out = out.replace("${CLAUDE_PLUGIN_ROOT}", plugin_root)
    if project_dir is not None:
        out = out.replace("${CLAUDE_PROJECT_DIR}", project_dir)
    return out


def _validate_matcher(matcher: Optional[str], mode: str, event: str) -> None:
    """校验 matcher：claude-code 纯 token 视为字面（合法）；其余按正则编译，非法抛 ``ValueError``。"""
    if matcher is None or matcher == "" or matcher == "*":
        return
    if mode == "claude-code" and re.fullmatch(r"[A-Za-z0-9_|]+", matcher):
        return  # 字面交替，合法
    try:
        re.compile(matcher)
    except re.error as exc:
        raise ValueError(f"钩子 matcher {matcher!r}（事件 {event}）不是合法正则：{exc}")


def _hook_timeout(raw: dict) -> Optional[float]:
    """从原始钩子对象取超时（秒）。Codex 接受 ``timeout`` 或 ``timeoutSec`` 别名。"""
    for key in ("timeout", "timeoutSec"):
        value = raw.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _parse_groups(
    raw_groups: Any,
    events: list,
    *,
    mode: str,
    substitutor,
    accept_async: bool,
    skip_reason: str,
) -> ParsedClaudeConfig:
    """通用：遍历 events，把每个事件的 matcher 组解析为 MatcherGroup 列表。"""
    config: dict = {}
    skipped: list = []
    for event in events:
        if not isinstance(raw_groups, dict):
            break
        groups_raw = raw_groups.get(event)
        if not isinstance(groups_raw, list):
            continue
        groups: list = []
        for raw_group in groups_raw:
            group = _as_object(raw_group)
            if group is None or not isinstance(group.get("hooks"), list):
                continue
            commands: list = []
            for raw_hook in group["hooks"]:
                hook = _as_object(raw_hook)
                if hook is None:
                    continue
                hook_type = hook.get("type") if isinstance(hook.get("type"), str) else "command"
                if hook_type != "command":
                    skipped.append(SkippedHook(event, f'unsupported "{hook_type}" hook'))
                    continue
                if not accept_async and hook.get("async") is True:
                    skipped.append(SkippedHook(event, "async hook"))
                    continue
                command = hook.get("command")
                if not isinstance(command, str):
                    continue
                commands.append(CommandHook(command=substitutor(command), timeout_sec=_hook_timeout(hook)))
            if not commands:
                continue
            matcher = None if event in _NO_MATCHER_EVENTS else (
                hook_matcher if isinstance((hook_matcher := group.get("matcher")), str) else None
            )
            _validate_matcher(matcher, mode, event)
            groups.append(MatcherGroup(hooks=commands, matcher=matcher))
        if groups:
            config[event] = groups
    return ParsedClaudeConfig(config=config, skipped=skipped)


def parse_claude_code_config(raw: Any, plugin_root: Optional[str] = None, project_dir: Optional[str] = None) -> ParsedClaudeConfig:
    """解析 Claude Code 钩子配置（settings 的 ``hooks`` 段或裸事件映射）。

    非 command 钩子进入 ``skipped``；``${CLAUDE_PLUGIN_ROOT}``/``${CLAUDE_PROJECT_DIR}`` 在解析期替换；
    ``UserPromptSubmit``/``Stop`` 的 matcher 被丢弃。matcher 命中且为非法正则 → 抛 ``ValueError``。
    """
    root = _as_object(raw)
    hooks_map = None
    if root is not None:
        hooks = root.get("hooks")
        hooks_map = hooks if isinstance(hooks, dict) else root
    if hooks_map is None:
        return ParsedClaudeConfig(config={}, skipped=[])

    def substitutor(command: str) -> str:
        return substitute_command(command, plugin_root, project_dir)

    return _parse_groups(
        hooks_map, CLAUDE_EVENTS,
        mode="claude-code", substitutor=substitutor, accept_async=True, skip_reason="unsupported",
    )


def parse_codex_config(raw: Any) -> ParsedCodexConfig:
    """解析 Codex 钩子配置（``{ hooks: … }`` 包裹或裸事件映射）。

    跳过非 command 与 ``async:true`` 钩子（进 ``skipped``）；不做命令替换；matcher 永远按正则；
    ``UserPromptSubmit``/``Stop`` 的 matcher 被丢弃。非法正则 → 抛 ``ValueError``。
    """
    root = _as_object(raw)
    hooks_map = None
    if root is not None:
        hooks = root.get("hooks")
        hooks_map = hooks if isinstance(hooks, dict) else root
    if hooks_map is None:
        return ParsedCodexConfig(config={}, skipped=[])

    def substitutor(command: str) -> str:
        return command  # Codex 不做命令替换

    return _parse_groups(
        hooks_map, CODEX_EVENTS,
        mode="codex", substitutor=substitutor, accept_async=False, skip_reason="unsupported",
    )

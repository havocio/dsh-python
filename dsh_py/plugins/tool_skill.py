"""耐久会话技能目录 + 模型侧 ``skill`` 加载工具。

对齐 dsh 的 ``@deepseek-ai/dsh-tool-skill``：本包拥有 schema、校验、提示指引与
目录发布，绝不拥有具体 provider（provider 选择在 ``ctx.skills``）。

两个 pre-step 监听器（注册顺序即瀑布嵌套顺序，对齐 dsh）：
1. **目录发布**（内层）：技能工具可见时快照模型可调用技能，把 ``<available_skills>``
   目录以 ``skill-catalog`` 来源进入本步（首次发布 / 替换更新 / 摘要去重）。
2. **``/name`` 手势注入**（外层）：claimed user 消息首行以 ``/<skill>`` 开头且命名
   **用户可调用**技能时，把技能正文以 ``skill-invocation`` 来源注入本步末尾——
   背景在前（工作区规则、运行时策略、目录），模型必须执行的材料最后、最靠近回答。

适配（dsh_py 差异，均已注明）：
- 无 ``defineTool`` 输出 schema / 展示层：handler 直接返回 ``render_skill_content``
  文本（dsh 的结构化输出在 dsh_py 工具契约中未表达）。
- ``tools.get(name, agent)`` 身份比较 → ``ctx.tools.has(name)``（dsh_py 工具全局
  单注册，无 per-agent 阴影）；``agent`` 作为 scope 键传入查找（dsh_py 无
  每-agent 分层注册，等效 global，仅为对齐 dsh 语义）。
- 目录历史用 ``session.surface``（nodes）+ ``session.events`` 反向扫描
  ``user/message`` 的 ``source.kind == 'skill-catalog'``。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.services.message import Message, MessageSource, create_user_message
from dsh_py.services.skill import (
    escape_text,
    is_model_invocable,
    is_skill_name,
    is_user_invocable,
    render_skill_content,
)

DEFAULT_CATALOG_DESCRIPTION_MAX_LENGTH = 500

# 技能工具 schema
SKILL_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The exact skill name from the available skills list."},
    },
    "required": ["name"],
}

# 空白界定的 ``/name`` 记号（公共技能名语法）：任意位置的词边界——第二个 ``/``
# 或任何非边界字符都会破坏匹配，把文件路径（/usr/bin）与分数（5/8）排除在外。
SKILL_GESTURE = re.compile(r"(^|\s)\/([a-z0-9]+(?:-[a-z0-9]+)*)(?=\s|$)")


# ---------------------------------------------------------------------------
# 目录渲染 / 摘要
# ---------------------------------------------------------------------------

def catalog_description(value: str, max_length: int) -> str:
    """归一化、限长的目录描述（原样未转义——转义属于帧，不落库）。"""
    normalized = re.sub(r"\s+", " ", value).strip()
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max_length - 3]}..."


def catalog_source_entries(skills: list, description_max_length: int) -> list[dict]:
    """与渲染目录行镜像的耐久条目列表（供非模型消费者）。"""
    return [
        {"name": s.name, "description": catalog_description(s.description, description_max_length)}
        for s in skills
    ]


def _render_catalog_entries(entries: list[dict]) -> list[str]:
    return [f"- `{e['name']}`: {escape_text(e['description'])}" for e in entries]


def digest_catalog_entries(entries: list[dict]) -> str:
    """目录身份基于耐久条目而非渲染散文；逐条 JSON 引号化保证边界精确。"""
    canonical = "\n".join(json.dumps([e["name"], e["description"]], ensure_ascii=False) for e in entries)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _render_catalog_message(entries: list[dict]) -> Message:
    text = "\n".join([
        "<system-reminder>",
        "A skill is a reusable set of task-specific instructions. The following skills are available in this session:",
        "",
        "<available_skills>",
        *_render_catalog_entries(entries),
        "</available_skills>",
        "",
        "If the user names a skill, or the task clearly matches a skill's description, call the `skill` tool with the exact skill name before taking task actions. Load all applicable skills, then follow their full instructions. This catalog contains summaries only; do not infer or follow a skill's instructions until it has been loaded.",
        "A user may also invoke a skill directly; its <skill_content> block then appears in this conversation. Follow it, and do not call the `skill` tool again for that skill.",
        "</system-reminder>",
    ])
    return create_user_message(
        [{"type": "text", "text": text}],
        MessageSource(kind="skill-catalog", form="catalog", entries=tuple(entries)),
    )


def _render_catalog_update(entries: list[dict]) -> Message:
    if not entries:
        availability = [
            "No skills are currently available through the `skill` tool. Do not use names from earlier skill catalogs.",
            "A user may still invoke a skill directly; its <skill_content> block then appears in this conversation. Follow it, and do not call the `skill` tool for it.",
        ]
    else:
        availability = [
            "Use only names in this replacement catalog. If the user names a listed skill, or the task clearly matches its description, call the `skill` tool with the exact name before acting.",
            "A user may also invoke a skill directly; its <skill_content> block then appears in this conversation. Follow it, and do not call the `skill` tool again for that skill.",
        ]
    text = "\n".join([
        "<system-reminder>",
        "The available skill catalog changed. This complete catalog replaces every earlier available-skills list in this session:",
        "",
        "<available_skills>",
        *_render_catalog_entries(entries),
        "</available_skills>",
        "",
        *availability,
        "</system-reminder>",
    ])
    return create_user_message(
        [{"type": "text", "text": text}],
        MessageSource(kind="skill-catalog", form="catalog", update=True, entries=tuple(entries)),
    )


def _read_catalog_entries(source: Any) -> Optional[list]:
    """从一个来源读耐久目录条目；不可读返回 None（视为「非本插件的目录」）。"""
    entries = getattr(source, "entries", None)
    if not isinstance(entries, (tuple, list)) or not entries:
        return None
    readable: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        name = entry.get("name")
        description = entry.get("description")
        if not isinstance(name, str) or name == "" or not isinstance(description, str):
            return None
        readable.append({"name": name, "description": description})
    return readable


def _catalog_history(agent: Any) -> dict:
    """会话历史里最后一次可见目录（visibleDigest + published 标记）。"""
    session = agent.session
    surface = session.surface
    visible = set(surface.get("nodes", []))
    events = session.events
    published = False
    for event in reversed(events):
        if event.type != "user/message":
            continue
        source = getattr(event.data, "source", None)
        if source is None or getattr(source, "kind", None) != "skill-catalog":
            continue
        entries = _read_catalog_entries(source)
        if entries is None:
            continue
        digest = digest_catalog_entries(entries)
        published = True
        if event.seq in visible:
            return {"visible_digest": digest, "published": True}
    return {"published": published}


def _catalog_message(messages: list) -> Any:
    """本步消息里已发布的目录消息（message + entries）；无则 None。"""
    for message in messages:
        source = getattr(message, "source", None)
        if source is None or getattr(source, "kind", None) != "skill-catalog":
            continue
        entries = _read_catalog_entries(source)
        if entries is not None:
            return {"message": message, "entries": entries}
    return None


# ---------------------------------------------------------------------------
# 手势识别
# ---------------------------------------------------------------------------

def invoked_skill_names(messages: list) -> list:
    """claimed user 消息中的 ``/name`` 手势 token，首见顺序去重。

    仅扫描 ``source.kind == 'user'`` 消息——外部文本无法伪造手势。
    """
    names: list[str] = []
    for message in messages:
        source = getattr(message, "source", None)
        if source is None or getattr(source, "kind", None) != "user":
            continue
        for block in message.content:
            # dsh_py 的内容块是 dict（{type, text, ...}）
            if isinstance(block, dict):
                if block.get("type") != "text":
                    continue
                text = block.get("text")
            else:
                if getattr(block, "type", None) != "text":
                    continue
                text = getattr(block, "text", "")
            if not isinstance(text, str):
                continue
            for match in SKILL_GESTURE.finditer(text):
                name = match.group(2)
                if name and name not in names:
                    names.append(name)
    return names


# ---------------------------------------------------------------------------
# 工具 handler
# ---------------------------------------------------------------------------

async def _skill_handler(args: dict, exec: dict, ctx: AppContext) -> tuple[str, bool]:
    name = args.get("name", "")
    if not is_skill_name(name):
        return f'invalid skill name "{name}"', True
    skills = getattr(ctx, "skills", None) if ctx.has_service("skills") else None
    if skills is None:
        return "skill: the skills service is not mounted", True
    agent = exec.get("agent")
    signal = exec.get("signal")
    lookup: dict = {"signal": signal}
    if agent is not None:
        session = getattr(agent, "session", None)
        if session is not None:
            header = getattr(session, "header", None)
            lookup["cwd"] = getattr(header, "cwd", None) if header is not None else None
        lookup["scope"] = agent  # dsh_py 无每-agent 分层注册，等效 global
    try:
        summaries = await skills.list(lookup)
        summary = next((s for s in summaries if s.name == name), None)
        if summary is None:
            return f'skill "{name}" is unknown or no longer available', True
        if not is_model_invocable(summary):
            return f'skill "{name}" is not available for model invocation', True
        skill = await skills.get(name, lookup)
        if skill is None:
            return f'skill "{name}" is unknown or no longer available', True
        if not is_model_invocable(skill):
            return f'skill "{name}" is not available for model invocation', True
    except Exception as exc:  # noqa: BLE001
        return f"skill load failed: {exc}", True
    return render_skill_content(skill), False


# ---------------------------------------------------------------------------
# 插件入口
# ---------------------------------------------------------------------------

def _assert_positive_integer(name: str, value: Any, minimum: int = 1) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"tool-skill: {name} must be an integer greater than or equal to {minimum}")


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """注册 ``skill`` 加载工具 + 目录 / 手势两个 pre-step 监听器。"""
    config = config or {}
    catalog_description_max_length = config.get("catalogDescriptionMaxLength", DEFAULT_CATALOG_DESCRIPTION_MAX_LENGTH)
    _assert_positive_integer("catalogDescriptionMaxLength", catalog_description_max_length, 3)

    tools = getattr(ctx, "tools", None) if ctx.has_service("tools") else None
    if tools is not None:
        tools.register(
            "skill",
            "Load the full instructions for an available skill. Call this with the exact skill name from the session skill catalog before acting on a task that names or clearly matches that skill.",
            SKILL_TOOL_SCHEMA,
            _make_skill_handler(ctx),
        )

    # ── 内层：目录发布（先注册 = 外层是手势监听，见模块 docstring）──
    @ctx.on("agent/pre-step")
    async def _catalog_pre_step(payload: dict, next):  # noqa: ANN001
        decision = await next()
        if decision.get("kind") == "reject":
            return decision
        signal = payload.get("signal")
        if signal is not None and getattr(signal, "throw_if_aborted", None):
            signal.throw_if_aborted()
        agent = payload.get("agent")
        tool_visible = tools is not None and tools.has("skill")
        lookup: dict = {"signal": signal}
        session = getattr(agent, "session", None) if agent is not None else None
        header = getattr(session, "header", None) if session is not None else None
        lookup["cwd"] = getattr(header, "cwd", None) if header is not None else None
        if agent is not None:
            lookup["scope"] = agent
        skills = getattr(ctx, "skills", None) if ctx.has_service("skills") else None
        if not tool_visible or skills is None:
            snapshot = {"skills": [], "complete": True}
        else:
            snapshot = await skills.snapshot(lookup)
        if signal is not None and getattr(signal, "throw_if_aborted", None):
            signal.throw_if_aborted()
        if not snapshot["complete"]:
            return decision
        skills_view = [s for s in snapshot["skills"] if is_model_invocable(s)]
        entries = catalog_source_entries(skills_view, catalog_description_max_length)
        digest = digest_catalog_entries(entries)
        history = _catalog_history(agent) if agent is not None else {"published": False}
        existing = _catalog_message(decision.get("messages", []))

        def enter_with(messages: list) -> dict:
            return {"kind": "enter", "messages": messages}

        if history.get("visible_digest") == digest:
            if existing is None:
                return decision
            return enter_with([m for m in decision.get("messages", []) if m is not existing["message"]])

        if existing is not None and digest_catalog_entries(existing["entries"]) == digest:
            return decision

        if not history.get("published") and not skills_view:
            if existing is None:
                return decision
            return enter_with([m for m in decision.get("messages", []) if m is not existing["message"]])

        catalog = _render_catalog_update(entries) if history.get("published") else _render_catalog_message(entries)
        messages = decision.get("messages", [])
        if existing is None:
            return enter_with([*messages, catalog])
        return enter_with([catalog if m is existing["message"] else m for m in messages])

    # ── 外层：/name 手势注入（正文最后进入，最靠近回答）──
    @ctx.on("agent/pre-step")
    async def _gesture_pre_step(payload: dict, next):  # noqa: ANN001
        decision = await next()
        if decision.get("kind") == "reject":
            return decision
        names = invoked_skill_names(payload.get("messages", []))
        if not names:
            return decision
        signal = payload.get("signal")
        if signal is not None and getattr(signal, "throw_if_aborted", None):
            signal.throw_if_aborted()
        agent = payload.get("agent")
        skills = getattr(ctx, "skills", None) if ctx.has_service("skills") else None
        if skills is None:
            return decision
        lookup: dict = {"signal": signal}
        session = getattr(agent, "session", None) if agent is not None else None
        header = getattr(session, "header", None) if session is not None else None
        lookup["cwd"] = getattr(header, "cwd", None) if header is not None else None
        if agent is not None:
            lookup["scope"] = agent
        injections: list[Message] = []
        for name in names:
            skill = await skills.get(name, lookup)
            if signal is not None and getattr(signal, "throw_if_aborted", None):
                signal.throw_if_aborted()
            # 未知名 / 用户禁用技能保持普通散文：手势不是本边界承认的主张
            if skill is None or not is_user_invocable(skill):
                continue
            injections.append(create_user_message(
                [{"type": "text", "text": render_skill_content(skill)}],
                MessageSource(kind="skill-invocation", name=name, form="instructions"),
            ))
        if not injections:
            return decision
        return {"kind": "enter", "messages": [*decision.get("messages", []), *injections]}


def _make_skill_handler(ctx: AppContext) -> Any:
    async def handler(args: dict, exec: dict) -> tuple[str, bool]:  # noqa: ANN001
        return await _skill_handler(args, exec, ctx)

    return handler


apply.inject = ["agents", "tools", "skills"]  # 声明：本插件依赖这些服务（供 loader 拓扑排序）

__all__ = [
    "DEFAULT_CATALOG_DESCRIPTION_MAX_LENGTH",
    "catalog_description",
    "catalog_source_entries",
    "digest_catalog_entries",
    "invoked_skill_names",
    "apply",
]

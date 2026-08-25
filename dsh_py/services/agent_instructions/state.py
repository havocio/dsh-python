"""会话可见的工作区指令状态与动态协调（agent-instructions/state，对标 dsh 的
``state.ts``）。

将「已加载的基线文件」转换为比较与版本缓存状态；按可见状态与提供者可见文件
比较，渲染出 set/replace/remove 跃迁。版本缓存按会话隔离，刻意不保留指令正文。
"""

from __future__ import annotations

import os
import types
import weakref
from dataclasses import dataclass
from typing import List, Optional

from dsh_py.services.agent_instructions.digest import (
    instruction_content_sha1,
    trimmed_instruction_digest,
)
from dsh_py.services.agent_instructions.files import (
    ancestor_chain,
    descendant_dirs_between,
    find_project_root,
    probe_scope_instruction,
    read_scope_instruction,
    relative_display,
)
from dsh_py.services.agent_instructions.render import (
    USER_GLOBAL_DIRECTORY,
    USER_GLOBAL_FILE,
    AgentInstructionChange,
    candidate_scope_key,
    decode_scope_key,
    instruction_scope_key,
    render_instruction_changes,
)
from dsh_py.services.message import Message, MessageSource, TextBlock, create_user_message

name = "agent-instructions"


@dataclass
class AgentInstructionSource:
    """每个工作区上下文的生产者 / 文件 / 协调事实。"""

    kind: str = "agent-instructions"
    form: str = "instructions"
    baseline: Optional[bool] = None
    baselineIdentity: str = ""
    changes: tuple = ()


@dataclass
class InstructionVersionState:
    """逐作用域的元数据缓存（刻意不保留指令正文）。"""

    path: str
    version: object
    digest: str
    trimmedDigest: str


# 按会话隔离的版本缓存（会话对象本身作为弱键）
InstructionVersionCache = weakref.WeakKeyDictionary


@dataclass
class InstructionVersionUpdate:
    change: AgentInstructionChange
    state: Optional[InstructionVersionState] = None


@dataclass
class ReconciledInstructionContext:
    context: Message
    versionUpdates: List[InstructionVersionUpdate]


def workspace_context_message(text: str) -> Message:
    """为渲染后的基线构造 user 角色消息（仅取内容；来源在 compose 中重写）。"""
    return create_user_message(
        [TextBlock(text)],
        source=MessageSource("plugin", plugin=name),
    )


def is_workspace_context_source(source) -> bool:
    """判断一个来源是否为 agent-instructions。"""
    if not isinstance(source, MessageSource):
        return False
    return source.kind == "plugin" and source.plugin == name


def is_workspace_context(message: Message) -> bool:
    return is_workspace_context_source(message.source)


def workspace_instruction_changes(source) -> List[AgentInstructionChange]:
    """从来源携带的 changes 中解析出合法跃迁。"""
    changes: List[AgentInstructionChange] = []
    raw = source.changes if isinstance(source, MessageSource) else ()
    for value in raw:
        if not isinstance(value, dict):
            continue
        action = value.get("action")
        if action not in ("set", "replace", "remove"):
            continue
        scope = value.get("scope")
        path = value.get("path")
        if not isinstance(scope, str) or not isinstance(path, str):
            continue
        digest = value.get("digest")
        if digest is not None and not isinstance(digest, str):
            continue
        changes.append(AgentInstructionChange(action, scope, path, digest))
    return changes


def same_instruction_change(a: AgentInstructionChange, b: AgentInstructionChange) -> bool:
    return (a.action == b.action and a.scope == b.scope
            and a.path == b.path and a.digest == b.digest)


def _event_by_seq(session, seq: int):
    index = seq - 1
    if 0 <= index < len(session.events):
        return session.events[index]
    return None


def visible_instruction_changes(agent, authority_messages: List[Message]) -> dict:
    """收集会话表面与权威消息中当前可见的指令作用域 → 跃迁映射。"""
    visible_seqs = set(agent.session.surface["nodes"])
    visible: dict = {}
    for ev in agent.session.events:
        if ev.type != "user/message":
            continue
        if not isinstance(ev.data, Message):
            continue
        if not is_workspace_context_source(ev.data.source):
            continue
        # 仅计入当前表面可见的事件（用 seq 匹配）
        if ev.seq not in visible_seqs:
            continue
        for change in workspace_instruction_changes(ev.data.source):
            visible[change.scope] = change
    for message in authority_messages:
        if not is_workspace_context_source(message.source):
            continue
        for change in workspace_instruction_changes(message.source):
            visible[change.scope] = change
    return visible


def baseline_instruction_state(files: List):
    """把保留的基线文件转换为比较状态与按作用域键控的提供者版本。"""
    changes: dict = {}
    versions: dict = {}
    for f in files:
        digest = instruction_content_sha1(f.content)
        change = AgentInstructionChange("set", instruction_scope_key(f.display_path), f.display_path, digest)
        changes[change.scope] = change
        versions[change.scope] = InstructionVersionState(
            path=f.display_path, version=f.version,
            digest=digest, trimmedDigest=trimmed_instruction_digest(f.content))
    return types.SimpleNamespace(changes=changes, versions=versions)


def version_states_for(session, cache) -> dict:
    states = cache.get(session)
    if states is None:
        states = {}
        cache[session] = states
    return states


def retained_instruction_version_updates(updates: List[InstructionVersionUpdate],
                                         rendered_changes: List[AgentInstructionChange]) -> List[InstructionVersionUpdate]:
    return [u for u in updates if any(same_instruction_change(u.change, c) for c in rendered_changes)]


def apply_instruction_version_updates(session, updates: List[InstructionVersionUpdate], cache) -> None:
    if not updates:
        return
    states = version_states_for(session, cache)
    for u in updates:
        if u.state is None:
            states.pop(u.change.scope, None)
        else:
            states[u.change.scope] = u.state
    if not states:
        cache.pop(session, None)


def relative_scope(project_root: str, directory: str) -> str:
    rel = relative_display(project_root, directory)
    return "." if rel == "" else rel


def reconcile_instruction_context(
    agent,
    resolved,
    version_cache,
    options: dict,
) -> Optional[ReconciledInstructionContext]:
    """比较可见状态与提供者可见文件，渲染出跃迁。

    :param agent: 会话拥有者（其表面供给持久状态）。
    :param resolved: 归一化配置。
    :param version_cache: 按会话隔离的逐作用域元数据缓存。
    :param options: 权威已认领上下文、待处理作用域提示、被触碰路径、是否纳入基线作用域。
    """
    session = agent.session
    effective = visible_instruction_changes(agent, options["authority_messages"])
    cwd = session.header.cwd or os.getcwd()
    project_root = options.get("project_root") or find_project_root(
        cwd, resolved.project_root_markers)

    scopes = set()
    baseline_scopes = set()
    baseline_scopes.add(candidate_scope_key(USER_GLOBAL_DIRECTORY, USER_GLOBAL_FILE))
    for directory in ancestor_chain(project_root, cwd):
        rel = relative_scope(project_root, directory)
        for candidate in resolved.instruction_file_candidates:
            baseline_scopes.add(candidate_scope_key(rel, candidate))
        for candidate in resolved.local_instruction_file_candidates:
            baseline_scopes.add(candidate_scope_key(rel, candidate))

    if options["includeBaselineScopes"]:
        for s in baseline_scopes:
            scopes.add(s)

    for message in options["scope_messages"]:
        if not is_workspace_context(message):
            continue
        for change in workspace_instruction_changes(message.source):
            if not options["includeBaselineScopes"] and change.scope in baseline_scopes:
                continue
            scopes.add(change.scope)

    for scope in list(effective.keys()):
        if not options["includeBaselineScopes"] and scope in baseline_scopes:
            continue
        key = decode_scope_key(scope)
        if key["directory"] == USER_GLOBAL_DIRECTORY:
            scopes.add(candidate_scope_key(USER_GLOBAL_DIRECTORY, USER_GLOBAL_FILE))
        else:
            scopes.add(candidate_scope_key(key["directory"], key["candidateName"]))

    for touched in options["touchedPaths"]:
        for directory in descendant_dirs_between(cwd, touched):
            rel = relative_scope(project_root, directory)
            for candidate in resolved.instruction_file_candidates:
                scopes.add(candidate_scope_key(rel, candidate))

    versions = version_states_for(session, version_cache)
    seen_absolute_paths = set()
    kept_trimmed_by_dir: dict = {}
    items: List = []
    version_updates: List[InstructionVersionUpdate] = []

    def register_kept_trimmed(directory: str, digest: str) -> bool:
        digests = kept_trimmed_by_dir.setdefault(directory, set())
        if digest in digests:
            return True
        digests.add(digest)
        return False

    def push_removal(scope: str, path: str) -> None:
        change = AgentInstructionChange("remove", scope, path)
        items.append(_ChangeItem(change, _empty_loaded(path)))
        version_updates.append(InstructionVersionUpdate(change))

    scopes_by_directory: dict = {}
    for scope in scopes:
        scopes_by_directory.setdefault(decode_scope_key(scope)["directory"], []).append(scope)

    for directory, directory_scopes in scopes_by_directory.items():
        probed_scopes: List[str] = []
        for scope in directory_scopes:
            excluded = (options.get("excludedBaselineScopes") is not None
                        and scope in baseline_scopes
                        and scope in options["excludedBaselineScopes"])
            if excluded:
                previous = effective.get(scope)
                if previous is None or previous.action == "remove":
                    versions.pop(scope, None)
                else:
                    push_removal(scope, previous.path)
            else:
                probed_scopes.append(scope)

        item_start = len(items)
        version_update_start = len(version_updates)
        added_absolute_paths: List[str] = []
        prior_versions = {s: versions.get(s) for s in probed_scopes}

        for scope in probed_scopes:
            previous = effective.get(scope)
            probe = probe_scope_instruction(scope, project_root, resolved)
            if probe.kind == "unavailable":
                if previous is None or previous.action == "remove":
                    continue
                items[item_start:] = []
                version_updates[version_update_start:] = []
                for cand_scope, prior in prior_versions.items():
                    if prior is None:
                        versions.pop(cand_scope, None)
                    else:
                        versions[cand_scope] = prior
                for p in added_absolute_paths:
                    seen_absolute_paths.discard(p)
                kept_trimmed_by_dir.pop(directory, None)
                break
            if probe.kind == "absent":
                if previous is None or previous.action == "remove":
                    versions.pop(scope, None)
                else:
                    push_removal(scope, previous.path)
                continue
            probed_file = probe.file
            if probed_file.absolute_path in seen_absolute_paths:
                continue
            seen_absolute_paths.add(probed_file.absolute_path)
            added_absolute_paths.append(probed_file.absolute_path)
            # 始终读取文件实际内容并比对当前摘要：可见状态（previous）与缓存（cached）
            # 一致并不能证明文件未被外部编辑，必须直接比对 current_digest 才能发现
            # 文件变更（如 write 工具触碰后的增量刷新）。这也消除了原先“缓存一致即跳过读取”
            # 导致的漏更与随集合迭代顺序变化的非确定行为。
            file = read_scope_instruction(probed_file, resolved.max_source_bytes)
            if file is None:
                if previous is None or previous.action == "remove":
                    versions.pop(scope, None)
                else:
                    push_removal(scope, previous.path)
                continue
            current_digest = instruction_content_sha1(file.content)
            trimmed = trimmed_instruction_digest(file.content)
            if register_kept_trimmed(directory, trimmed):
                if previous is not None and previous.action != "remove":
                    push_removal(scope, previous.path)
                else:
                    versions.pop(scope, None)
                continue
            next_version = InstructionVersionState(
                path=file.display_path, version=probed_file.version,
                digest=current_digest, trimmedDigest=trimmed)
            if (previous is not None and previous.action != "remove"
                    and previous.path == file.display_path and previous.digest == current_digest):
                versions[scope] = next_version
                continue
            action = "set" if (previous is None or previous.action == "remove") else "replace"
            change = AgentInstructionChange(action, scope, file.display_path, current_digest)
            if os.environ.get("AI_DETAIL"):
                import sys
                print(f"[AI_DETAIL]   -> CHANGE action={action} scope={scope!r} cur_digest={current_digest!r}", file=sys.stderr)
            items.append(_ChangeItem(change, file))
            version_updates.append(InstructionVersionUpdate(change, next_version))

    if not items:
        return None
    rendered = render_instruction_changes(items, resolved.max_bytes)
    if rendered["text"] == "" or not rendered["changes"]:
        return None
    return ReconciledInstructionContext(
        context=workspace_context_hook(rendered["text"], rendered["changes"]),
        versionUpdates=retained_instruction_version_updates(version_updates, rendered["changes"]),
    )


class _ChangeItem:
    """一次状态跃迁 + 用于渲染的内容（内部结构）。"""

    def __init__(self, change: AgentInstructionChange, file) -> None:
        self.change = change
        self.file = file


class _EmptyLoaded:
    def __init__(self, display_path: str) -> None:
        self.absolute_path = f"removed:{display_path}"
        self.display_path = display_path
        self.content = ""


def _empty_loaded(path: str):
    return _EmptyLoaded(path)


def workspace_context_hook(text: str, changes: List[AgentInstructionChange]) -> Message:
    """构造一条 agent-instructions 来源的 user 消息。"""
    return create_user_message(
        [TextBlock(text)],
        source=MessageSource(
            "plugin", plugin=name, form="instructions",
            baseline=None, baselineIdentity="",
            changes=tuple(c.to_dict() for c in changes)),
    )

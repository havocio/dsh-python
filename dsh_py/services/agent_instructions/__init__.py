"""工作区指令加载器（agent-instructions，对标 dsh 的 ``context/agent-instructions``）。

基线指令在进入首个请求前进入耐久上下文；成功的 fs 工具触碰（read/write/edit）
会把项目内嵌套、变更或移除的指令增量刷新进 inbox。插件生命周期读取走可选
``ctx.fs`` 提供者；无提供者时禁用加载（对齐 dsh 的 no-op 语义）。

集成说明（与 dsh 的差异，已注明）：
- dsh_py 的 fs 服务不暴露 ``stat``/``streamText``/``version``，故发现与读取走主机
  文件系统（对齐 dsh 的 providerless node 兜底路径），``version`` 不暴露 → 版本快
  路径退化为每次重读；
- 单级 agent 无子 agent 嵌套，``exec.parent`` 恒为 None，文件触碰直接投影；
- 版本缓存按会话弱键隔离；投影刷新经 asyncio 任务链串行化（对齐 dsh 的
  projectionTails promise 链），保证同一 agent 的增量按到达顺序折叠。
"""

from __future__ import annotations

import asyncio
import os
import weakref
from typing import List, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.signal import CancelSignal
from dsh_py.services.agent_instructions.config import Config, resolve_config, workspace_baseline_identity
from dsh_py.services.agent_instructions.files import find_project_root, load_baseline_instruction_set
from dsh_py.services.agent_instructions.render import AgentInstructionChange
from dsh_py.services.agent_instructions.state import (
    InstructionVersionCache,
    apply_instruction_version_updates,
    baseline_instruction_state,
    is_workspace_context,
    is_workspace_context_source,
    name,
    reconcile_instruction_context,
    workspace_context_message,
)
from dsh_py.services.message import Message, MessageSource, TextBlock, create_user_message

logger = None  # 在 apply 中按 ctx 的 logger 解析


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def workspace_source(baseline: Optional[bool], baseline_identity: str,
                     changes: List[AgentInstructionChange]) -> MessageSource:
    """构造一条 agent-instructions 来源的 MessageSource。"""
    return MessageSource(
        "plugin", plugin=name, form="instructions",
        baseline=baseline, baselineIdentity=baseline_identity,
        changes=tuple(c.to_dict() if hasattr(c, "to_dict") else c for c in changes),
    )


def _file_path_from_execution(exec: dict) -> Optional[str]:
    """从一次工具执行中提取被触碰的文件路径（read/write/edit）。"""
    if exec.get("name") not in ("read", "write", "edit"):
        return None
    args = exec.get("arguments")
    if not isinstance(args, dict):
        return None
    path = args.get("file_path")
    if not isinstance(path, str):
        return None
    path = path.strip()
    return path or None


def _same_context_payload(left: Optional[Message], right: Optional[Message]) -> bool:
    """比较两个消息的内容 + 来源是否深度相等（对齐 dsh 的 isDeepStrictEqual）。"""
    if left is None or right is None:
        return False
    return left.content == right.content and left.source == right.source


def _event_by_seq(session, seq: int):
    index = seq - 1
    if 0 <= index < len(session.events):
        return session.events[index]
    return None


def _visible_baseline_source(agent, authority_messages: List[Message]) -> Optional[MessageSource]:
    """寻找当前可见的基线 agent-instructions 来源（权威消息优先，再回退表面）。"""
    for message in reversed(authority_messages):
        src = message.source
        if is_workspace_context_source(src) and src.baseline is True:
            return src
    visible_seqs = list(agent.session.surface["nodes"])
    for seq in reversed(visible_seqs):
        ev = _event_by_seq(agent.session, seq)
        if ev is not None and ev.type == "user/message" and isinstance(ev.data, Message):
            src = ev.data.source
            if is_workspace_context_source(src) and src.baseline is True:
                return src
    return None


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：挂工作区指令加载器（基线 + 动态协调 + fs 触碰刷新）。"""
    global logger
    logger = getattr(ctx, "logger", None)
    config = config or {}
    cfg = Config(
        dsh_home=config.get("dshHome"),
        project_root_markers=config.get("projectRootMarkers"),
        max_bytes=config.get("maxBytes"),
        max_source_bytes=config.get("maxSourceBytes"),
        instruction_file_candidates=config.get("instructionFileCandidates"),
        local_instruction_file_candidates=config.get("localInstructionFileCandidates"),
    )
    resolved = resolve_config(cfg)
    instruction_versions: InstructionVersionCache = weakref.WeakKeyDictionary()
    baseline_preparations: InstructionVersionCache = weakref.WeakKeyDictionary()
    projection_lifecycle = CancelSignal()
    projection_tails: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
    open_steps: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
    step_touches: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def file_system():
        return ctx.fs if ctx.has_service("fs") else None

    async def compose(agent, signal, claimed, pending, touched_paths: List[str] = None) -> Optional[Message]:
        touched_paths = touched_paths or []
        signal.throw_if_aborted()
        if resolved.max_bytes <= 0 or not _finite(resolved.max_bytes):
            return None
        if file_system() is None:
            return None
        if len(touched_paths) == 0 and len(pending) > 0:
            return pending[0]
        content: List = []
        changes: List[AgentInstructionChange] = []
        desired_baseline = False
        authority_messages = list(claimed)
        cwd = agent.session.header.cwd or os.getcwd()
        project_root = find_project_root(cwd, resolved.project_root_markers)
        identity = workspace_baseline_identity(resolved, cwd, project_root)
        visible_baseline = _visible_baseline_source(agent, authority_messages)
        baseline_present = visible_baseline is not None
        keep_visible_baseline = bool(visible_baseline and visible_baseline.baselineIdentity == identity)
        prepared = baseline_preparations.get(agent.session)
        excluded_baseline_scopes = (
            prepared["excludedScopes"] if (keep_visible_baseline and prepared
                                           and prepared["identity"] == identity) else None)
        next_preparation = None
        if not baseline_present or not keep_visible_baseline or excluded_baseline_scopes is None:
            instructions = load_baseline_instruction_set({
                "cwd": cwd,
                "dshHome": resolved.dsh_home,
                "projectRootMarkers": resolved.project_root_markers,
                "maxBytes": resolved.max_bytes,
                "maxSourceBytes": resolved.max_source_bytes,
                "instructionFileCandidates": resolved.instruction_file_candidates,
                "localInstructionFileCandidates": resolved.local_instruction_file_candidates,
                "projectRoot": project_root,
                "replacePreviousBaseline": baseline_present and not keep_visible_baseline,
            })
            baseline = baseline_instruction_state(instructions.included if instructions else [])
            observed_baseline = baseline_instruction_state(instructions.observed if instructions else [])
            excluded = set(observed_baseline.changes.keys())
            for s in baseline.changes.keys():
                excluded.discard(s)
            excluded_baseline_scopes = excluded
            next_preparation = {"identity": identity, "excludedScopes": excluded}
            version_states = instruction_versions.get(agent.session)
            if version_states is None and baseline.versions:
                version_states = {}
                instruction_versions[agent.session] = version_states
            for s, st in baseline.versions.items():
                if version_states is not None:
                    version_states[s] = st
            if not keep_visible_baseline and instructions is not None and instructions.rendered.text:
                baseline_content = workspace_context_message(instructions.rendered.text).content
                content.extend(baseline_content)
                replacement_scopes = set(baseline.changes.keys())
                replacement_removals = []
                if baseline_present and not keep_visible_baseline:
                    for change in (visible_baseline.changes if visible_baseline else ()):
                        if change.action == "remove" or change.scope in replacement_scopes:
                            continue
                        replacement_removals.append(AgentInstructionChange(
                            "remove", change.scope, change.path))
                baseline_changes = [*replacement_removals, *baseline.changes.values()]
                changes.extend(baseline_changes)
                authority_messages.append(create_user_message(
                    baseline_content,
                    source=workspace_source(True, identity, list(baseline_changes))))
                desired_baseline = True

        update = reconcile_instruction_context(agent, resolved, instruction_versions, {
            "authority_messages": authority_messages,
            "scope_messages": pending,
            "includeBaselineScopes": keep_visible_baseline,
            **({"excludedBaselineScopes": excluded_baseline_scopes}
               if excluded_baseline_scopes is not None else {}),
            "touchedPaths": touched_paths,
            "projectRoot": project_root,
        })
        if update is not None:
            content.extend(update.context.content)
            if is_workspace_context_source(update.context.source):
                changes.extend(update.context.source.changes)
            apply_instruction_version_updates(agent.session, update.versionUpdates, instruction_versions)
        if next_preparation is not None:
            baseline_preparations[agent.session] = next_preparation
        if not content:
            return None
        return create_user_message(
            list(content),
            source=workspace_source(True if desired_baseline else None,
                                   identity if desired_baseline else "", changes),
        )

    def sync_inbox(agent, claimed, desired: Optional[Message]) -> None:
        pending = [m for m in agent.inbox.next_step if is_workspace_context(m)]
        already_supplied = desired is not None and (
            any(_same_context_payload(m, desired) for m in claimed)
            or any(_same_context_payload(_surface_message(agent.session, seq), desired)
                   for seq in agent.session.surface["nodes"]))
        if desired is None or already_supplied:
            for m in pending:
                agent.inbox.remove(m.id)
            return
        reusable = next((m for m in pending if _same_context_payload(m, desired)), None)
        if reusable is not None:
            for m in pending:
                if m is not reusable:
                    agent.inbox.remove(m.id)
            return
        replaced = pending[0] if pending else None
        if replaced is None:
            agent.inbox.prepend("next-step", desired)
        else:
            agent.inbox.replace(replaced.id, desired)
        for m in pending[1:]:
            agent.inbox.remove(m.id)

    def _surface_message(session, seq):
        ev = _event_by_seq(session, seq)
        if ev is not None and ev.type == "user/message" and isinstance(ev.data, Message):
            return ev.data
        return None

    async def compose_and_sync(agent, signal, claimed, touched_paths: List[str] = None) -> None:
        touched_paths = touched_paths or []
        pending = [m for m in agent.inbox.next_step if is_workspace_context(m)]
        desired = await compose(agent, signal, claimed, pending, touched_paths)
        signal.throw_if_aborted()
        sync_inbox(agent, claimed, desired)

    def queue_projection(agent, touched_path: str) -> None:
        previous = projection_tails.get(agent) or asyncio.sleep(0, result=None)
        future = asyncio.ensure_future(_chain_projection(previous, agent, touched_path))
        projection_tails[agent] = future

    async def _chain_projection(previous, agent, touched_path: str) -> None:
        try:
            await previous
        except Exception:
            pass
        try:
            await compose_and_sync(agent, projection_lifecycle, [], [touched_path])
        except Exception as exc:  # noqa: BLE001
            if not projection_lifecycle.aborted:
                if logger is not None:
                    logger.warn("workspace instruction refresh failed: %s" % exc)
        finally:
            if projection_tails.get(agent) is not None:
                try:
                    projection_tails.pop(agent)
                except Exception:
                    pass

    async def wait_for_projections(agent) -> None:
        while True:
            projection = projection_tails.get(agent)
            if projection is None:
                break
            await projection

    def step_is_open(session) -> bool:
        known = open_steps.get(session)
        if known is not None:
            return known
        open_ = False
        for ev in session.events:
            if ev.type == "step/start":
                open_ = True
            elif ev.type in ("step/end", "turn/end"):
                open_ = False
        open_steps[session] = open_
        return open_

    def project_touch(touch: dict) -> None:
        session = touch["agent"].session
        if not step_is_open(session):
            queue_projection(touch["agent"], touch["path"])
            return
        pending = step_touches.get(session)
        if pending is None:
            step_touches[session] = [touch]
        else:
            pending.append(touch)

    @ctx.on("session/event")
    def on_session_event(session, event) -> None:
        if event.type == "step/start":
            open_steps[session] = True
            return
        if event.type == "turn/end":
            open_steps[session] = False
            return
        if event.type != "step/end":
            return
        open_steps[session] = False
        pending = step_touches.get(session)
        if pending is None:
            return
        step_touches.pop(session, None)
        for touch in pending:
            queue_projection(touch["agent"], touch["path"])

    @ctx.on("agent/pre-step")
    async def on_pre_step(event: dict, next):
        decision = await next()
        agent = event["agent"]
        await wait_for_projections(agent)
        pending = [m for m in agent.inbox.next_step if is_workspace_context(m)]
        desired = await compose(agent, event["signal"], event["messages"], pending)
        signal = event["signal"]
        signal.throw_if_aborted()
        if decision.get("kind") == "reject" or (event["step"] == 1 and len(decision.get("messages", [])) == 0):
            sync_inbox(agent, event["messages"], desired)
            return decision
        for m in pending:
            agent.inbox.remove(m.id)
        if desired is None or any(_same_context_payload(m, desired) for m in decision.get("messages", [])):
            return decision
        last_claimed_index = -1
        for i, m in enumerate(decision.get("messages", [])):
            if m in event["messages"]:
                last_claimed_index = i
        entered = list(decision.get("messages", []))
        if last_claimed_index >= 0:
            entered.insert(last_claimed_index + 1, desired)
        else:
            entered.append(desired)
        return {"kind": "enter", "messages": entered}

    @ctx.on("tools/result")
    def on_tools_result(exec: dict, result: dict) -> None:
        if result.get("isError") or exec.get("agent") is None:
            return
        signal = exec.get("signal")
        if signal is not None and getattr(signal, "aborted", False):
            return
        own_path = _file_path_from_execution(exec)
        if own_path is not None:
            project_touch({"agent": exec["agent"], "path": own_path})

    def dispose() -> None:
        projection_lifecycle.abort("agent-instructions disposed")
        instruction_versions.clear()
        baseline_preparations.clear()

    ctx.effect(dispose)


apply.Config = Config
apply.name = "agent-instructions"

"""子代理 seam（对标 dsh 的 ``@deepseek-ai/dsh-subagent``，进程内最小形态）。

dsh 的 workflow 引擎经 ``ctx.subagents.start(provider, config)`` 派生每个
子代理，provider 是具名路由（如 ``spawn``：全新会话 + 无父上下文）。dsh_py
此前只有 ``plugins/subagent.py`` 的**工具**，没有该 seam 服务——本模块补齐：

- :class:`SubagentRuntime`（``ctx.subagents``）：provider 注册表 + ``start()``；
- 内置 ``spawn`` provider：新会话 + ``ctx.agents.create_agent`` 派生子代理，
  跑完一轮后把子会话的 assistant 文本汇总为 output 块；
- **结构化输出差异（承重）**：dsh 的 spawn provider 经真实 ``response_format``
  兑现 ``outputSchema``；dsh_py 的 LLM 层尚无结构化输出，故用**尽力而为的
  JSON 提取**（整文解析 → fenced 块 → 首个花括号平衡块）。提取失败时
  ``structured`` 保持 ``None``，由运行时按子失败处理（``agent(schema)`` 返回
  ``null``）。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.agent import AgentOptions
from dsh_py.services.message import (
    MessageSource,
    TextBlock,
    ToolResultBlock,
    create_user_message,
    new_id,
)
from dsh_py.services.workflow.types import SessionId


@dataclass(frozen=True)
class SubagentCapabilities:
    """provider 的能力声明（引擎/工具据此门控）。"""

    output_schema: bool


@dataclass(frozen=True)
class SubagentProvider:
    """一个具名子代理路由的描述（``get_provider`` 返回）。"""

    name: str
    capabilities: SubagentCapabilities
    inherits_parent_context: bool = False


@dataclass(frozen=True)
class SubagentResult:
    """子代理的终态结果（进程内直传，无序列化往返）。

    :ivar output: 子的最终 assistant 输出块（list[dict]，``{"type":"text",...}``）。
    :ivar structured: 结构化值（请求带 outputSchema 且提取成功时才存在）。
    :ivar stopReason: 子运行为何结束（``completed`` 之外为失败）。
    """

    output: list
    structured: Any = None
    stopReason: str = "completed"


class SubagentRun:
    """一个已发布的子运行句柄（对标 dsh 的 ``SubagentRun``）。"""

    def __init__(self, session_id: str, task: asyncio.Task) -> None:
        self.id: SessionId = SessionId(session_id)
        self._task = task
        self._result_future: asyncio.Future = asyncio.get_running_loop().create_future()
        task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task) -> None:
        if self._result_future.done():
            return
        if task.cancelled():
            self._result_future.set_result(SubagentResult(output=[], stopReason="cancelled"))
            return
        exc = task.exception()
        if exc is not None:
            self._result_future.set_result(SubagentResult(output=[], stopReason="error"))
            return
        self._result_future.set_result(task.result())

    @property
    def result(self) -> Awaitable[SubagentResult]:
        """以子的终态解析；永不拒绝（失败折叠进 ``stopReason``）。"""
        return self._result_future

    async def dispose(self) -> None:
        """取消子任务并等待其终止（有界）。"""
        if not self._task.done():
            self._task.cancel()
        try:
            await asyncio.gather(self._task, return_exceptions=True)
        except Exception:  # noqa: BLE001 - 释放绝不抛错
            pass


# --------------------------------------------------------------------------- #
# 文本 → 块 / JSON 提取
# --------------------------------------------------------------------------- #


def blocks_to_text(blocks: Any) -> str:
    """把 prompt 内容块（dict 或 TextBlock）拼成纯文本。"""
    parts: list[str] = []
    for block in blocks or []:
        if isinstance(block, dict):
            parts.append(str(block.get("text", "")))
        elif hasattr(block, "text"):
            parts.append(str(block.text))
    return "".join(parts)


_FENCED_JSON = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def extract_structured(text: str) -> Any:
    """尽力而为地从子输出提取结构化 JSON 值（见模块 docstring 的差异说明）。

    依次尝试：整文解析 → fenced 代码块 → 首个花括号平衡块。全部失败返回 None。
    """
    candidate = text.strip()
    if not candidate:
        return None
    for attempt in (candidate, _first_brace_block(candidate)):
        try:
            value = json.loads(attempt)
            if isinstance(value, dict):
                return value
        except Exception:  # noqa: BLE001 - 继续下一个候选
            continue
    for match in _FENCED_JSON.finditer(text):
        try:
            value = json.loads(match.group(1).strip())
            if isinstance(value, dict):
                return value
        except Exception:  # noqa: BLE001 - 继续
            continue
    return None


def _first_brace_block(text: str) -> str:
    """截取从首个 ``{`` 到与之平衡的 ``}`` 的子串（尽力而为）。"""
    start = text.find("{")
    if start == -1:
        return ""
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


# --------------------------------------------------------------------------- #
# Runtime + 本地 provider
# --------------------------------------------------------------------------- #


class SubagentRuntime(Service):
    """子代理运行时（``ctx.subagents``）：具名 provider 注册表 + ``start()``。"""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "subagents")
        self._providers: dict[str, SubagentProvider] = {}
        self._backends: dict[str, Callable[[dict], SubagentRun]] = {}
        # --- 可续跑子代理（continuable）运行时状态 ---
        # 说明：dsh 的 followup/interrupt/listChildren/listDescendants 经由一个独立的
        # continuation 管理器（session 投影派生），本实现以「内存注册表 + 会话链接」
        # 给出零依赖等价物：父→子映射、子元信息、活在内存的子 Agent 句柄与后台驱动任务。
        self._children: dict[str, list[str]] = {}      # parent_id -> [child_id]
        self._parents: dict[str, str] = {}             # child_id -> parent_id
        self._child_info: dict[str, dict] = {}         # child_id -> 元信息（label/provider/mode/created_at/status）
        self._continuable_agents: dict[str, Any] = {}  # child_id -> 活在内存的 Agent 句柄
        self._drivers: dict[str, "asyncio.Task"] = {}  # child_id -> 后台驱动任务

    def register_provider(self, provider: SubagentProvider) -> None:
        """注册一个具名 provider（重复名覆盖）。"""
        self._providers[provider.name] = provider

    def register_backend(self, name: str, backend: Callable[[dict], Any]) -> None:
        """注册一个具名**外进程/外部**后端（重复名覆盖）。

        :param name: 路由名（与 provider 名一致）。
        :param backend: ``async callable(config) -> SubagentRun``——自行 spawn
            外部进程并发布句柄（如 subagent-acp 的 ACP 子代理驱动）；``start``
            命中时优先于进程内 ``_run_child`` 路径。
        """
        self._backends[name] = backend

    def get_provider(self, name: str) -> Optional[SubagentProvider]:
        """按名取 provider；未注册返回 None。"""
        return self._providers.get(name)

    async def start(self, provider: str, config: dict) -> SubagentRun:
        """在指定 provider 上启动一个子代理运行（发布即返回句柄）。

        :param provider: 具名 provider 路由。
        :param config: ``{"prompt": 块, "parent": Agent, "signal": CancelSignal,
            "outputSchema": 可选, "agentOptions": 可选 {provider, model}}``。
        :returns: 已发布的子运行句柄（``id`` 立即可用，``result`` 异步解析）。
        :raises WorkflowError: ``AGENT_START``（provider 未注册等启动失败）。
        """
        from dsh_py.services.workflow import WorkflowError

        registered = self._providers.get(provider)
        if registered is None:
            raise WorkflowError(f'no subagent provider registered for "{provider}"', "AGENT_START")
        # 外进程 backend 优先：命中时由 backend 自行发布句柄（含进程回收）。
        backend = self._backends.get(provider)
        if backend is not None:
            return await backend(config)
        sub_session = self.ctx.sessions.create()
        task = asyncio.create_task(self._run_child(sub_session, config, registered))
        signal = config.get("signal")
        if signal is not None:
            signal.add_listener(lambda: task.cancel())
        return SubagentRun(str(getattr(sub_session, "id", "") or sub_session.header.id), task)

    async def _run_child(self, sub_session: Any, config: dict, provider: SubagentProvider) -> SubagentResult:
        """派生子代理、跑完一轮、汇总输出（并尽力兑现结构化输出）。"""
        parent = config.get("parent")
        agent_options = config.get("agentOptions") or {}
        provider_route = agent_options.get("provider") or (
            parent.options.provider if parent is not None else ""
        ) or ""
        model_route = agent_options.get("model") or (
            parent.options.model if parent is not None else ""
        ) or ""
        child_agent = self.ctx.agents.create_agent(
            sub_session,
            AgentOptions(provider=provider_route, model=model_route, system=""),
        )
        await child_agent.run(blocks_to_text(config.get("prompt", [])))
        blocks: list[dict] = []
        texts: list[str] = []
        for ev in sub_session.events:
            if ev.type == "assistant/message":
                from dsh_py.services.message import as_text

                text = as_text(ev.data["message"].content)
                if text:
                    texts.append(text)
                    blocks.append({"type": "text", "text": text})
        structured = None
        if config.get("outputSchema") is not None and texts:
            structured = extract_structured("\n".join(texts))
        return SubagentResult(output=blocks, structured=structured, stopReason="completed")

    # ------------------------------------------------------------------ #
    # 可续跑子代理（continuable）：followup / interrupt / list
    # ------------------------------------------------------------------ #

    async def start_continuable(self, parent: Any, config: dict) -> str:
        """派生一个可续跑子代理并投递初始 prompt，返回子会话 id。

        子 Agent 一旦创建即常驻内存（其收件箱随会话持久），后续经由 ``followup``
        继续投递消息；``interrupt`` 中止其当前轮。父→子链接登记进内存注册表，供
        ``list_children`` / ``list_descendants`` 枚举（零依赖等价 dsh 的 session 投影派生）。

        :param parent: 派生的父 Agent（活实例，``parent.id`` 即父会话 id）。
        :param config: ``{"prompt": 块, "parent": Agent, "signal", "outputSchema",
            "agentOptions": {"provider","model"}, "label"}``。
        :returns: 子会话 id（``str``）。
        :raises WorkflowError: ``AGENT_START``（父缺失或 provider 未注册）。
        """
        from dsh_py.services.workflow import WorkflowError

        if parent is None:
            raise WorkflowError("continuable subagent 需要一个父 Agent", "AGENT_START")
        parent_id = str(getattr(parent, "id", parent))
        sub_session = self.ctx.sessions.create()
        child_id = str(getattr(sub_session, "id", "") or sub_session.header.id)
        agent_options = config.get("agentOptions") or {}
        provider_route = agent_options.get("provider") or (
            parent.options.provider if getattr(parent, "options", None) is not None else ""
        ) or ""
        model_route = agent_options.get("model") or (
            parent.options.model if getattr(parent, "options", None) is not None else ""
        ) or ""
        child_agent = self.ctx.agents.create_agent(
            sub_session, AgentOptions(provider=provider_route, model=model_route, system="")
        )
        # 注册父→子链接与元信息
        self._children.setdefault(parent_id, []).append(child_id)
        self._parents[child_id] = parent_id
        self._child_info[child_id] = {
            "label": config.get("label") or "",
            "provider": provider_route,
            "mode": "continuable",
            "created_at": time.time(),
            "status": "starting",
        }
        self._continuable_agents[child_id] = child_agent
        signal = config.get("signal")
        task = asyncio.create_task(
            self._drive_continuable(child_id, child_agent, blocks_to_text(config.get("prompt", [])), signal)
        )
        self._drivers[child_id] = task
        return child_id

    async def _drive_continuable(self, child_id: str, agent: Any, prompt: str, signal: Any) -> None:
        """后台驱动：投递初始 prompt 并等待其处理完；Agent 常驻，后续 followup 走收件箱。"""
        self._child_info[child_id]["status"] = "running"
        try:
            await agent.run(prompt)
        except Exception:  # noqa: BLE001 - 子失败折叠进状态，不向外抛
            self._child_info[child_id]["status"] = "error"
            return
        self._child_info[child_id]["status"] = "idle"

    async def followup(self, parent: Any, child_id: str, content: list, options: dict) -> str:
        """向一个可续跑子代理投递后续消息（作为下一轮 user 内容）。

        :param parent: 授权父 Agent（必须是该子的直接父，否则拒绝）。
        :param child_id: 子会话 id。
        :param content: 内容块列表（每个块为 ``{"type":"text","text":...}`` 或 TextBlock）。
        :param options: 来源/取消等（最小实现忽略无关字段）。
        :returns: 被投递消息的 id。
        :raises WorkflowError: ``UNAUTHORIZED``（父非直接父）或 ``NO_CHILD``（非可续跑子）。
        """
        from dsh_py.services.workflow import WorkflowError

        if self._parents.get(child_id) != str(getattr(parent, "id", parent)):
            raise WorkflowError(f"父 Agent 并非子 {child_id} 的直接父，followup 被拒", "UNAUTHORIZED")
        agent = self._continuable_agents.get(child_id)
        if agent is None:
            raise WorkflowError(f"子 {child_id} 不是可续跑子代理（无法 followup）", "NO_CHILD")
        blocks = []
        for block in content or []:
            if isinstance(block, dict) and block.get("type") == "text":
                blocks.append(TextBlock(str(block.get("text", ""))))
            elif isinstance(block, TextBlock):
                blocks.append(block)
        if not blocks:
            raise WorkflowError("followup 内容为空", "INVALID_ARGUMENT")
        msg = create_user_message(blocks, MessageSource("user"))
        agent.insert(msg)  # 触发异步 drain（fire-and-forget），下一轮随即处理
        return str(getattr(msg, "id", new_id()))

    async def report_from(self, child: Any, content: list, options: dict) -> str:
        """把一个可续跑子代理的选定内容投递给它的直接父 Agent（子为授权凭证）。

        :param child: 精确的活子 Agent 实例（其 ``id`` 即子会话 id）。
        :param content: 内容块列表（每个块为 ``{"type":"text","text":...}`` 或 TextBlock）。
        :param options: ``{"delivery": "wakeup"|"quiet"}``；``quiet`` 仅入父收件箱不唤醒。
        :returns: 父侧接受的消息 id。
        :raises WorkflowError: ``NO_CHILD``（非可续跑子）/ ``PARENT_NOT_LIVE``（父不在活注册表）
            / ``INVALID_ARGUMENT``（内容为空）。
        """
        from dsh_py.services.workflow import WorkflowError

        child_id = str(getattr(child, "id", child))
        parent_id = self._parents.get(child_id)
        if parent_id is None:
            raise WorkflowError(f"子 {child_id} 不是可续跑子代理（无法 report）", "NO_CHILD")
        delivery = (options or {}).get("delivery", "wakeup")
        parent_agent = None
        if self.ctx.has_service("agentLoop"):
            parent_agent = self.ctx.agentLoop.get(parent_id)
        if parent_agent is None:
            raise WorkflowError(f"子 {child_id} 的直接父 {parent_id} 不在活注册表（无法投递报告）", "PARENT_NOT_LIVE")
        blocks = []
        for block in content or []:
            if isinstance(block, dict) and block.get("type") == "text":
                blocks.append(TextBlock(str(block.get("text", ""))))
            elif isinstance(block, TextBlock):
                blocks.append(block)
        if not blocks:
            raise WorkflowError("report 内容为空", "INVALID_ARGUMENT")
        msg = create_user_message(blocks, MessageSource("user"))
        if delivery == "quiet":
            parent_agent.inbox.append("next-turn", msg)  # 仅入收件箱，不唤醒父 agent
        else:
            parent_agent.insert(msg)  # 唤醒父 agent 处理（默认）
        return str(getattr(msg, "id", new_id()))

    def interrupt(self, child_id: str, authority: Any) -> None:
        """中止一个可续跑子代理的当前轮（fire-and-return）。

        缺失目标（未知 id / 非可续跑子 / 非其祖先授权）是接受的 no-op；授权失败则抛错。
        """
        from dsh_py.services.workflow import WorkflowError

        if child_id not in self._continuable_agents:
            return  # absent target：no-op（对标 dsh）
        auth_id = str(getattr(authority, "id", authority))
        if self._parents.get(child_id) != auth_id and auth_id not in self._ancestors_of(child_id):
            raise WorkflowError(f"authority {auth_id} 无权中断子 {child_id}", "UNAUTHORIZED")
        agent = self._continuable_agents[child_id]
        try:
            agent.cancel("interrupted by control tool")
        except Exception:  # noqa: BLE001
            pass
        self._child_info[child_id]["status"] = "interrupted"

    def _ancestors_of(self, child_id: str) -> set[str]:
        """返回 child_id 的全部祖先 parent id 集合（用于中断授权）。"""
        seen: set[str] = set()
        cur = self._parents.get(child_id)
        while cur is not None and cur not in seen:
            seen.add(cur)
            cur = self._parents.get(cur)
        return seen

    async def list_children(self, parent_id: str) -> list[dict]:
        """枚举 parent 的直接可续跑子代理（按 created_at 升序，其次 id）。"""
        child_ids = self._children.get(parent_id, [])
        entries = []
        for cid in child_ids:
            info = self._child_info.get(cid, {})
            entries.append({
                "id": cid,
                "parentId": parent_id,
                "label": info.get("label", ""),
                "provider": info.get("provider", ""),
                "mode": info.get("mode", "continuable"),
                "status": info.get("status", "unknown"),
                "createdAt": info.get("created_at", 0.0),
            })
        entries.sort(key=lambda e: (e["createdAt"], e["id"]))
        return entries

    async def list_descendants(self, root_id: str) -> list[dict]:
        """枚举 root 的完整可续跑子树（稳定前序，带 parentId 与 depth）。"""
        result: list[dict] = []
        stack = [(root_id, 0)]
        visited: set[str] = set()
        while stack:
            node, depth = stack.pop(0)
            if node in visited:
                continue
            visited.add(node)
            for cid in self._children.get(node, []):
                info = self._child_info.get(cid, {})
                result.append({
                    "id": cid,
                    "parentId": node,
                    "label": info.get("label", ""),
                    "provider": info.get("provider", ""),
                    "mode": info.get("mode", "continuable"),
                    "status": info.get("status", "unknown"),
                    "createdAt": info.get("created_at", 0.0),
                    "depth": depth + 1,
                })
                stack.append((cid, depth + 1))
        result.sort(key=lambda e: (e["parentId"], e["createdAt"], e["id"]))
        return result

    async def dispose_continuable(self, child_id: str) -> None:
        """释放一个可续跑子代理（取消驱动任务 + 注销链接）。"""
        task = self._drivers.pop(child_id, None)
        if task is not None and not task.done():
            task.cancel()
        agent = self._continuable_agents.pop(child_id, None)
        if agent is not None:
            try:
                agent.cancel("disposed")
            except Exception:  # noqa: BLE001
                pass
        parent_id = self._parents.pop(child_id, None)
        if parent_id is not None:
            kids = self._children.get(parent_id)
            if kids and child_id in kids:
                kids.remove(child_id)
        self._child_info.pop(child_id, None)


# --------------------------------------------------------------------------- #
# 插件入口
# --------------------------------------------------------------------------- #


def apply(ctx: AppContext, config: Any = None) -> None:
    """注册 ``ctx.subagents`` 服务与内置 ``spawn`` provider。

    配置（可选）：
    - ``provider``：默认具名路由（默认 ``spawn``）；可额外声明
      ``providers: {名称: {"outputSchema": bool, "inheritsParentContext": bool}}``。
    """
    config = config or {}
    runtime = SubagentRuntime(ctx)
    runtime.register_provider(
        SubagentProvider(
            name="spawn",
            capabilities=SubagentCapabilities(output_schema=True),
            inherits_parent_context=False,
        )
    )
    for name, desc in (config.get("providers") or {}).items():
        runtime.register_provider(
            SubagentProvider(
                name=name,
                capabilities=SubagentCapabilities(
                    output_schema=bool(desc.get("outputSchema", True))
                ),
                inherits_parent_context=bool(desc.get("inheritsParentContext", False)),
            )
        )


apply.name = "subagents"
apply.provides = ["subagents"]

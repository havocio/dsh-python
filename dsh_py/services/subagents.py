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
from dataclasses import dataclass
from typing import Any, Awaitable, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.agent import AgentOptions
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

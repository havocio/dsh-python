"""Agent 主循环（agent seam，对标 dsh 的 ``dsh-agent`` + ``dsh-agent-loop``）。

一次对话由若干 ``turn``（轮）组成，每轮包含若干 ``step``（步）。每个 step 是一
次模型调用 + 它请求的工具执行：

1. ``pre-step`` 瀑布流：收集合成本步要进入的用户消息（默认决策是 ``enter``，
   插件可在此注入 system 之外的上下文，如长记忆召回）。
2. 构建请求：``session.derive_messages()`` + system + tools → ``ctx.llm.stream``。
3. 组装分块为 assistant 消息；若有工具调用则执行并把结果回填，继续下一 step；
   否则本 turn 结束。

「智能体循环本身也是插件」：循环实现被拆成注册表（:class:`AgentRegistry`，
``ctx.agents``）与具体实现（:class:`AgentLoop``，``ctx.agentLoop``）两层。循环
插件在构造时用 ``set_factory`` 把自己挂进注册表；换循环只需提供另一个实现并
再次 ``set_factory``，所有调用方一律经 ``ctx.agents.create_agent``，不感知实现。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.core.signal import CancelSignal, SignalCancelledError
from dsh_py.services.inbox import Inbox
from dsh_py.services.llm import (
    ChunkType,
    GenerateOptions,
    LlmError,
    LlmService,
    StreamChunk,
)
from dsh_py.services.message import (
    Message,
    MessageSource,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    create_assistant_message,
    create_user_message,
)
from dsh_py.services.session import Session, SessionService


@dataclass
class AgentOptions:
    """Agent 创建选项：路由与系统提示。"""
    provider: str = ""
    model: str = ""
    system: str = ""
    max_tokens: Optional[int] = None
    max_steps: int = 32
    # 单步内最大并行工具调用数（对标 dsh 的 maxParallelToolCalls；1 = 串行）
    max_parallel_tool_calls: int = 1


class BlockAssembler:
    """消费 :class:`StreamChunk` 流，组装出内容块、用量与结束原因。"""

    def __init__(self) -> None:
        self._text: Optional[tuple[int, str]] = None      # (index, text)
        self._reasoning: Optional[tuple[int, str]] = None  # (index, text)
        self._tool_calls: dict[int, dict] = {}
        self._order: list[tuple[str, int]] = []
        self._next_index = 0
        self.blocks: list[Any] = []
        self.usage: Optional[dict] = None
        self.finish: dict = {"kind": "stop"}

    def _open(self, kind: str) -> int:
        idx = self._next_index
        self._next_index += 1
        self._order.append((kind, idx))
        return idx

    def push(self, chunk: StreamChunk) -> None:
        """吞入一帧分块，维护打开中的块状态。"""
        t = chunk.type
        if t == ChunkType.BLOCK_START:
            kind = chunk.block_type
            if kind == "text":
                self._text = (chunk.index or self._open("text"), "")
            elif kind == "reasoning":
                self._reasoning = (chunk.index or self._open("reasoning"), "")
            elif kind == "tool-call":
                idx = chunk.index if chunk.index is not None else self._open("tool")
                if idx not in self._tool_calls:
                    self._tool_calls[idx] = {"id": "", "name": None, "arguments": ""}
                    self._order.append(("tool", idx))
        elif t == ChunkType.TEXT_DELTA:
            if self._text is None:
                self._text = (chunk.index or self._open("text"), "")
            self._text = (self._text[0], self._text[1] + (chunk.text or ""))
        elif t == ChunkType.REASONING_DELTA:
            if self._reasoning is None:
                self._reasoning = (chunk.index or self._open("reasoning"), "")
            self._reasoning = (self._reasoning[0], self._reasoning[1] + (chunk.reasoning or ""))
        elif t == ChunkType.TOOL_CALL_DELTA:
            idx = chunk.index if chunk.index is not None else 0
            tc = self._tool_calls.setdefault(idx, {"id": "", "name": None, "arguments": ""})
            if chunk.tool_call_id is not None:
                tc["id"] = chunk.tool_call_id
            if chunk.tool_call_name is not None:
                tc["name"] = chunk.tool_call_name
            tc["arguments"] += chunk.arguments_delta or ""
        elif t == ChunkType.BLOCK_END:
            self._close(chunk)
        elif t == ChunkType.USAGE:
            self.usage = chunk.usage
        elif t == ChunkType.FINISH:
            self.finish = chunk.finish or {"kind": "stop"}
            self._flush_open()

    def _close(self, chunk: StreamChunk) -> None:
        blk = chunk.block
        if isinstance(blk, TextBlock):
            self.blocks.append(blk)
            self._text = None
        elif isinstance(blk, ReasoningBlock):
            self.blocks.append(blk)
            self._reasoning = None
        elif isinstance(blk, ToolCallBlock):
            self.blocks.append(blk)
            self._tool_calls.pop(chunk.index, None)

    def _flush_open(self) -> None:
        """安全兜底：finish 时若有尚未关闭的块，强制收尾。"""
        if self._text is not None:
            self.blocks.append(TextBlock(self._text[1]))
            self._text = None
        if self._reasoning is not None:
            self.blocks.append(ReasoningBlock(self._reasoning[1]))
            self._reasoning = None
        for _idx, tc in sorted(self._tool_calls.items()):
            if tc["id"] or tc.get("name") is not None or tc["arguments"]:
                self.blocks.append(ToolCallBlock(id=tc["id"], name=tc["name"] or "", arguments=tc["arguments"]))
        self._tool_calls = {}


class Agent:
    """驱动一个会话跑完对话的默认 Agent（对标 dsh 的 ReactLoopAgent）。

    消息经 :class:`Inbox` 投递（``next-turn`` 队列，可持久化重放）；取消经
    :class:`CancelSignal`（对标 AbortSignal）。``run(text)`` 是同步语义（投递 +
    等到处理完）；``insert()`` / ``cancel()`` / ``when_idle()`` 供异步投递场景。

    构造即「发布」：向 ``agent/session-start`` 广播本 agent 及其来源
    （``startup`` / ``resume``）。取消信号可融合多源（调用方 + 生命周期 + 工厂
    teardown，对标 dsh 的 ``AbortSignal.any``）。
    """

    def __init__(self, ctx: AppContext, session: Session, options: AgentOptions,
                 source: str = "startup", signal: Optional[CancelSignal] = None) -> None:
        self.ctx = ctx
        self.session = session
        self.id = session.header.id  # agent 与所属会话绑定，id 取其会话 id
        self.options = options
        self._source = source
        # 收件箱：投递/取出用户输入，变更落 agent/inbox/spliced 事件
        self.inbox = Inbox(session, {
            "inserted": lambda m: self.ctx.emit("agent/inbox/inserted", self, m),
            "discarded": lambda m: self.ctx.emit("agent/inbox/discarded", self, m),
            "claimed": lambda m, turn: self.ctx.emit("agent/inbox/claimed", self, m, turn),
        })
        # 取消信号：三源融合（调用方 cancel / 生命周期卸载 / 工厂 teardown），
        # 融合后供主循环与 LLM 适配器检查（对标 dsh 的 AbortSignal.any）
        self._signal = CancelSignal.any([signal]) if signal is not None else CancelSignal()
        self._running = False
        self._activity: Optional[asyncio.Task] = None
        # 发布：广播本 agent 已进入会话（来源 startup / resume）
        self.ctx.emit("agent/session-start", {"agent": self, "source": self._source})

    # -- 输入 ---------------------------------------------------------------- #
    def insert(self, message: Message, target: str = "next-turn") -> None:
        """投递一条消息到收件箱；空闲时自动启动异步处理（fire-and-forget）。"""
        self.inbox.append(target, message)
        if not self._running and (self._activity is None or self._activity.done()):
            self._activity = asyncio.create_task(self._drain())

    async def run(self, user_text: str) -> None:
        """用一条用户消息开启一个 turn 并等待处理完（含全部步与工具循环）。"""
        user_msg = create_user_message([TextBlock(user_text)], MessageSource("user"))
        self.inbox.append("next-turn", user_msg)
        await self._drain()

    async def _drain(self) -> None:
        """处理收件箱直到空或取消（防重入）。"""
        if self._running:
            return
        self._running = True
        # 整个 agent 生命周期状态（对标 dsh 的 ``agent/status``：SDK 网关据此
        # 推送 session.status，客户端靠它判断一轮 run 是否结束）
        self.ctx.emit("agent/status", {"agent": self, "status": "running"})
        try:
            while self.inbox.has_pending:
                self._signal.throw_if_aborted()  # 每轮检查取消
                turn = self._next_turn_number()
                claimed = self.inbox.claim("next-turn", turn)
                if not claimed:
                    break
                await self._turn(claimed)
        except SignalCancelledError:
            pass  # 取消原因已由 _turn 记录
        finally:
            self._running = False
            self.ctx.emit("agent/status", {"agent": self, "status": "idle"})

    # -- 取消 / 等待 ---------------------------------------------------------- #
    def cancel(self, cause: Any = None) -> None:
        """取消当前活动（对标 dsh 的 ``agent.cancel(cause)``）。"""
        self._signal.abort(cause)

    async def when_idle(self) -> None:
        """等待当前异步活动结束（若存在）。"""
        if self._activity is not None and not self._activity.done():
            await self._activity

    def run_maintenance(self, job: Any) -> Any:
        """同步检查空闲后，启动一个非 turn 维护任务（如手工压缩）。

        仅当 agent 空闲时允许；活跃时同步抛 ``RuntimeError``（调用方应转译为
        自己的 busy 语义）。维护任务持有融合了本 agent 取消信号的独立信号，
        供任务体检查/传播。返回任务协程（调用方 ``await`` 之）。
        """
        if self._running:
            raise RuntimeError("agent 活跃中，无法运行维护任务")
        maintenance_signal = CancelSignal.any([self._signal])

        async def _run() -> Any:
            return await job(maintenance_signal)

        return _run()

    async def _turn(self, first_user_messages: list[Message]) -> None:
        turn = self._next_turn_number()
        self.session.append("turn/start", {"turn": turn})
        step = 0
        pending: list[Message] = list(first_user_messages)
        turn_ends: Optional[dict] = None
        try:
            while True:
                self._signal.throw_if_aborted()  # 每步检查取消
                if step >= self.options.max_steps:
                    turn_ends = {"kind": "max-tokens"}
                    break
                step += 1

                # 默认决策生产者必须是 async（waterfall 监听器均为 async，
                # 插件用 ``await next()`` 取其返回值）
                async def default_decision():
                    return {"kind": "enter", "messages": list(pending)}

                decision = await self.ctx.waterfall(
                    "agent/pre-step",
                    {"agent": self, "messages": pending, "turn": turn, "step": step, "signal": self._signal},
                    inner=default_decision,
                )
                if decision["kind"] == "reject":
                    turn_ends = {"kind": "blocked"}
                    break
                entered = decision["messages"]
                # 进入的消息写入会话日志，成为模型可见历史
                for message in entered:
                    self.session.append("user/message", message)
                self.session.append("step/start", {"turn": turn, "step": step})
                step_result = await self._step(turn, step)
                self.session.append("step/end", {"turn": turn, "step": step})
                pending = []
                turn_ends, additional = step_result
                # 注入执行后富化的额外上下文（如 repeat-tool-reminder 的重复提醒）：
                # 作为 user/message 进入历史，下一 step 的 derive_messages 即携带它们
                for msg in additional:
                    self.session.append("user/message", msg)
                if turn_ends is None:
                    # 工具调用已执行并产生工具结果，进入下一 step 处理
                    pending = self._collect_tool_results(turn, step)
                    continue
                break
        except SignalCancelledError as exc:
            # 取消：以 cancelled 原因收尾本 turn（dsh 的取消语义）
            turn_ends = {"kind": "cancelled", "reason": exc.reason}
        finally:
            self.session.append("turn/end", {"turn": turn, "reason": turn_ends or {"kind": "completed"}})

    def _next_turn_number(self) -> int:
        last = 0
        for ev in self.session.events:
            if ev.type == "turn/start":
                last = ev.data["turn"]
        return last + 1

    def _collect_tool_results(self, turn: int, step: int) -> list[Message]:
        """收集本 step 产生的工具结果消息，作为下一 step 的待处理用户输入。"""
        results: list[Message] = []
        for ev in self.session.events:
            if ev.type == "tool/result" and ev.data["turn"] == turn and ev.data["step"] == step:
                results.append(ev.data["message"])
        return results

    async def _step(self, turn: int, step: int) -> Optional[dict]:
        """执行一个 step：构建请求、流式调用、组装消息、执行工具。

        返回 ``None`` 表示需继续（出现了工具调用），否则返回结束原因。
        """
        await self.ctx.parallel("agent/request", {"agent": self, "turn": turn, "step": step, "signal": self._signal})
        messages = self.session.derive_messages()
        # system 提示：优先用 systemPrompt 服务组装渲染（对标 dsh：请求从
        # 注册的提示片段构建）；未挂载该服务时退回 AgentOptions.system
        if self.ctx.has_service("systemPrompt"):
            from dsh_py.services.system_prompt import render_prompt
            assembly = await self.ctx.systemPrompt.assemble({
                "agent": self, "session": self.session,
                "signal": self._signal, "turn": turn, "step": step,
            })
            system = render_prompt(assembly) or None
        else:
            system = self.options.system or None
        options = GenerateOptions(
            provider=self.options.provider,
            model=self.options.model,
            messages=messages,
            system=system,
            tools=self.ctx.tools.list_schemas() if self.ctx.has_service("tools") else None,
            max_tokens=self.options.max_tokens,
            signal=self._signal,  # 取消信号传入适配器（对标 AbortSignal）
        )
        # epoch 级调用配置记录到 header（对齐 dsh：请求从 header 构建，续跑复用）
        from dsh_py.services.call_config import call_config_from_options
        call_config = call_config_from_options(options)
        self.session.header.request = call_config
        # 最近路由请求的完整 header（config/system/tools）：compaction 摘要前缀复用
        self.session.request_header = {
            "config": {"provider": call_config.get("provider"), "model": call_config.get("model")},
            "system": system,
            "tools": options.tools,
        }

        assembler = BlockAssembler()
        # 请求失败可经 agent/request-error 瀑布流恢复（如 compaction 的上下文溢出
        # 恢复返回 {"kind": "retry"} 决策 → 从替换后的表面重试本步）
        while True:
            try:
                stream: AsyncIterator[StreamChunk] = self.ctx.llm.stream(options)
                async for chunk in stream:
                    # 逐帧广播，便于上层流式展示（对标 dsh 的 assistant/chunk）
                    self.session.append("assistant/chunk", {"turn": turn, "step": step, "chunk": chunk})
                    assembler.push(chunk)
                break
            except LlmError as exc:
                async def default_recovery() -> Any:
                    return None

                decision = await self.ctx.waterfall(
                    "agent/request-error",
                    {"agent": self, "failure": exc, "signal": self._signal},
                    inner=default_recovery,
                )
                if isinstance(decision, dict) and decision.get("kind") == "retry":
                    continue  # 监听器已（可能）压缩表面；从新表面重试
                raise

        finish = assembler.finish
        if finish.get("kind") == "error":
            return finish, []

        assistant_msg = create_assistant_message(
            assembler.blocks, provider=self.options.provider, model=self.options.model
        )
        self.session.append(
            "assistant/message",
            {"turn": turn, "step": step, "message": assistant_msg, "usage": assembler.usage},
        )

        if finish.get("kind") == "max-tokens":
            return {"kind": "max-tokens"}, []

        tool_calls = [b for b in assembler.blocks if isinstance(b, ToolCallBlock)]
        if not tool_calls:
            return {"kind": "completed"}, []

        # 执行工具并把结果回填（有界并行，结果按原调用顺序回填）
        additional = await self._execute_tool_calls(tool_calls, turn, step)
        return None, additional  # 需继续下一 step 处理工具结果

    async def _execute_tool_calls(self, tool_calls: list[ToolCallBlock], turn: int, step: int) -> list:
        """有界并行执行一批工具调用（对标 dsh 的 executeToolCalls + maxParallelToolCalls）。

        - 并发度受 ``max_parallel_tool_calls`` 限制（信号量）；
        - 结果按**原调用顺序**回填（tool/call + tool/result 事件稳定，历史可预测）；
        - 每个工具的错误都作为文本结果回流（工具执行永不向上抛）；
        - 经 ``tools/execute`` + ``tools/post-execute`` 瀑布流，收集各工具产生的
          ``additionalContexts``（如 repeat-tool-reminder 的重复提醒），返回给
          调用方注入下一 step。

        返回 ``additional_contexts``（Message 列表）。
        """
        semaphore_limit = max(1, self.options.max_parallel_tool_calls or 1)
        # 运行时设置优先：settings 命名空间挂载后，改配置即时生效（对标 dsh）
        loop = getattr(self.ctx, "agentLoop", None)
        if loop is not None and hasattr(loop, "current_parallel_limit"):
            live = loop.current_parallel_limit()
            if live:
                semaphore_limit = max(1, live)
        semaphore = asyncio.Semaphore(semaphore_limit)
        additional: list = []

        async def run_one(tc: ToolCallBlock) -> tuple[ToolCallBlock, tuple[str, bool, list]]:
            async with semaphore:
                return tc, await self.ctx.tools.execute_with_agent(
                    tc.name, tc.arguments, agent=self, signal=self._signal)

        results = await asyncio.gather(*(run_one(tc) for tc in tool_calls))
        for tc, (result_text, is_error, extra) in results:  # gather 保序 → 按原顺序回填
            self.session.append(
                "tool/call",
                {"turn": turn, "step": step, "callId": tc.id, "name": tc.name, "arguments": tc.arguments},
            )
            tr_msg = create_user_message(
                [ToolResultBlock(tool_call_id=tc.id, content=(TextBlock(result_text),), is_error=is_error)],
                source=MessageSource("tool"),
            )
            self.session.append(
                "tool/result",
                {"turn": turn, "step": step, "message": tr_msg},
            )
            additional.extend(extra)
        return additional

    def followup(self, message: Message) -> None:
        """把一个插件来源的消息作为「后续跟进」投递并触发下一轮（对标 dsh 的 ``agent.followup``）。

        与 :meth:`insert` 同语义（入收件箱 + 空闲时启动异步处理），区别在于投递的
        消息通常是 plugin 来源（不会被 repeat-tool-reminder 判定为用户打断而重置
        重复计数）。schedule 的提醒即经此通道注入。
        """
        self.insert(message)


class AgentRegistry(Service):
    """``agents`` 注册表服务：持有 agent 工厂，``ctx.agents``（对标 dsh 的 AgentRegistry）。

    智能体循环本身也是可替换的插件：具体实现（如 :class:`AgentLoop`）通过
    :meth:`set_factory` 把自己注册为工厂。换循环 = 注册另一个实现，调用方一律
    走 ``ctx.agents.create_agent``，不感知具体实现。
    """

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "agents")
        self._factory: Optional[Any] = None

    def set_factory(self, factory: Any) -> None:
        """注册 agent 工厂（循环实现）。后注册者覆盖前者——这就是「换循环」的入口。"""
        self._factory = factory

    def has_factory(self) -> bool:
        """当前是否已挂载循环实现。"""
        return self._factory is not None

    def create_agent(self, session: Session, options: Optional[AgentOptions] = None) -> Any:
        """经当前工厂创建一个 Agent；未注册工厂时给出明确指引。"""
        if self._factory is None:
            raise RuntimeError(
                "未注册 agent 工厂：请先加载默认循环插件 dsh_py.services.agent:apply_loop，"
                "或用 ctx.agents.set_factory(你的实现) 注册自定义循环"
            )
        return self._factory.create_agent(session, options)


class AgentLoop(Service):
    """``agentLoop`` 服务：默认智能体循环实现，``ctx.agentLoop``。

    构造时把自己注册进 ``agents`` 注册表（``set_factory``），因此只要先加载
    注册表再加载本插件，``ctx.agents.create_agent`` 即指向默认循环。用户可随时
    用 ``set_factory`` 覆盖为自定义实现——替换发生在运行期，装配无需改动。
    """

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "agentLoop")
        if not ctx.has_service("agents"):
            raise RuntimeError(
                "agents 注册表未就绪：请先加载 dsh_py.services.agent:apply_registry，"
                "再加载本循环插件"
            )
        self._agents: dict[str, Agent] = {}
        # 运行时设置源（settings 命名空间挂载后由 apply_loop 注入）：thunk -> dict
        self._settings_source: Any = None
        # 工厂级 teardown 信号：工厂（本服务）卸载时 abort，传导到所有经
        # resume/create 融合了它的 agent（对标 dsh 的 FactoryOwnership.signal）
        self._teardown = CancelSignal()
        ctx.effect(lambda: lambda: self._teardown.abort("agent loop is not active"))
        ctx.agents.set_factory(self)

    def create_agent(self, session: Session, options: Optional[AgentOptions] = None,
                     source: str = "startup", signal: Optional[CancelSignal] = None) -> Agent:
        """基于一个会话创建默认 Agent；``source`` 为发布来源（startup/resume）。"""
        agent = Agent(self.ctx, session, options or AgentOptions(), source=source, signal=signal)
        self._agents[session.header.id] = agent
        return agent

    def get(self, session_id: str) -> Optional[Agent]:
        """按会话 id 取当前已创建的 Agent（对齐 dsh 的 ``ctx.agents.get(id)``）。"""
        return self._agents.get(session_id)

    def roots(self) -> list:
        """返回当前所有「根」Agent（对齐 dsh 的 ``ctx.agents.roots()``）。

        dsh_py 当前没有子 agent 嵌套，所有经工厂创建的 Agent 均为根，故返回全集。
        """
        return list(self._agents.values())

    def resume(self, session_id: str, options: Optional[AgentOptions] = None,
               signal: Optional[CancelSignal] = None) -> Agent:
        """从持久化恢复一个 agent（会话历史 + 循环），发布来源为 ``resume``。

        取消三源融合：调用方信号 + 本循环工厂 teardown 信号（对标 dsh 的
        ``AbortSignal.any([caller, factory, owner])``）。会话不存在或未挂持久化
        后端时，由 ``sessions.resume`` 抛出明确错误。
        """
        session = self.ctx.sessions.resume(session_id)
        fused = CancelSignal.any([signal, self._teardown])
        return self.create_agent(session, options, source="resume", signal=fused)

    def current_parallel_limit(self) -> Optional[int]:
        """从运行时设置读取当前最大并行工具调用数（无设置源返回 None）。"""
        if self._settings_source is not None:
            try:
                value = self._settings_source().get("max_parallel_tool_calls")
                if value:
                    return int(value)
            except Exception:  # noqa: BLE001 - 设置读取失败回退默认
                return None
        return None


def apply_registry(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``agents`` 注册表服务（循环工厂的间接层）。"""
    AgentRegistry(ctx)


apply_registry.provides = ["agents"]  # 声明：本插件提供 agents 注册表服务


# apply_loop 的配置 schema：声明式 agents（对标 dsh agent-loop 的 Config.agents）
apply_loop_agent_schema = z.object({
    "id": z.string().default(""),
    "provider": z.string().default(""),
    "model": z.string().default(""),
    "system": z.string().default(""),
    "sessionId": z.string().optional(),
    "resumeSessionId": z.string().optional(),
    "cwd": z.string().optional(),
    "maxTokens": z.integer().optional(),
})


def validate_configured_agents(agents: list[dict]) -> None:
    """拒绝自包含的身份冲突（对标 dsh 的 ``validateConfiguredAgents``）。

    - ``sessionId`` 与 ``resumeSessionId`` 互斥（不能同时指定）；
    - 不同 agent 不得使用重复的精确会话身份（同一 id 会被第二次挂载覆盖）。
    """
    exact_identities: dict[str, str] = {}
    for entry in agents:
        agent_id = entry.get("id", "") or ""
        session_id = entry.get("sessionId")
        resume_id = entry.get("resumeSessionId")
        has_resume = resume_id is not None and resume_id != ""
        if session_id is not None and has_resume:
            raise RuntimeError(f"agent {agent_id!r}: sessionId 与 resumeSessionId 互斥")
        exact_identity = resume_id if has_resume else session_id
        if exact_identity is None:
            continue
        first_id = exact_identities.get(exact_identity)
        if first_id is not None:
            raise RuntimeError(
                f"agents {first_id!r} 与 {agent_id!r} 使用重复的精确会话身份 {exact_identity!r}"
            )
        exact_identities[exact_identity] = agent_id


def apply_loop(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册默认智能体循环（``agentLoop``）并挂为工厂。

    配置（可选）：
    - ``agents``：声明式 Agent 列表（对标 dsh 的 ``Config.agents``），每项含
      ``id/provider/model/system`` 与 ``sessionId``（新建）/ ``resumeSessionId``
      （从持久化恢复），启动时即创建；
    - ``max_steps``：默认步数上限（缺省 32）。
    """
    config = config or {}
    loop = AgentLoop(ctx)
    default_max_steps = config.get("max_steps")
    default_parallel = config.get("max_parallel_tool_calls")
    # 声明式 agents 的自包含身份校验：互斥键 / 重复精确身份（启动即失败）
    validate_configured_agents(config.get("agents", []))
    # 运行时设置命名空间（对标 dsh 的 AGENT_LOOP_SETTINGS_NAMESPACE）：
    # settings 服务挂载后，max_parallel_tool_calls 可在运行期被用户修改
    if ctx.has_service("settings"):
        from dsh_py.services.settings import install_settings_section, settings_namespace

        entry = {"max_parallel_tool_calls": default_parallel or 1}
        source = {"thunk": lambda: entry}

        def _set_source(current):
            source["thunk"] = current

        install_settings_section(
            ctx,
            settings_namespace("agent-loop"),
            z.object({"max_parallel_tool_calls": z.integer().default(1)}),
            entry,
            {"set_source": _set_source, "on_change": lambda: None},
        )
        loop._settings_source = lambda: source["thunk"]()
    for entry in config.get("agents", []):
        options = AgentOptions(
            provider=entry.get("provider", ""),
            model=entry.get("model", ""),
            system=entry.get("system", ""),
            max_steps=default_max_steps,
            max_parallel_tool_calls=default_parallel or 1,
            max_tokens=entry.get("maxTokens"),
        )
        if entry.get("resumeSessionId"):
            session = ctx.sessions.resume(entry["resumeSessionId"])
        elif entry.get("sessionId"):
            # 固定会话 id：prepare 时即指定（须在 enter 前）
            session = ctx.sessions.prepare(session_id=entry["sessionId"], cwd=entry.get("cwd"))
            ctx.sessions.enter(session)
        else:
            session = ctx.sessions.create(cwd=entry.get("cwd"))
        ctx.agents.create_agent(session, options)


apply_loop.Config = z.object({
    "agents": z.array(apply_loop_agent_schema).default([]),
    "max_steps": z.integer().optional(),
    "max_parallel_tool_calls": z.integer().optional(),
})
apply_loop.provides = ["agentLoop"]   # 声明：本插件提供 agentLoop 服务
apply_loop.inject = ["agents"]        # 依赖：agents 注册表必须先就绪（拓扑自动排序）


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：一次性注册注册表 + 默认循环。"""
    apply_registry(ctx, config)
    apply_loop(ctx, config)

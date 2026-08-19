"""进程内 SDK（对标 ``@deepseek-ai/dsh-sdk`` 的 client 侧编程式 API）。

dsh 的 SDK 是「进程外 runtime + JSON-RPC/WebSocket 客户端」的分布式形态
（``DeepSeekHarness`` 拉起子进程、``HarnessSession.run`` 经协议订阅事件直到
``session.status=idle`` 才返回 ``RunResult``）。dsh_py 在同一进程内等价翻译：

- :class:`DeepSeekHarness` —— 装配一个 ``AppContext``（可选自定义 profile），
  对标 dsh 同名类的 ``session()`` / ``run()`` / ``close()``；
- :class:`HarnessSession` —— 一个具名会话的句柄，``run(input, options)``
  跑一轮并返回 :class:`RunResult`（对标 dsh 的 ``HarnessSession.run``）；
- :class:`RunResult` —— ``session_id / final_response / events`` 三字段与 dsh
  逐一对齐（``finalResponse`` 从会话事件推导最终 assistant 文本）。

与 dsh SDK 一致：``session_id`` 缺省时自动生成（``session-<uuid>``），
``run()`` 首次调用会惰性装配（``start()`` 幂等），``close()`` 卸载全部插件
（dispose 各 Fiber，对标 ``AsyncDisposable``）。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, boot, load_profile
from dsh_py.services.agent import AgentOptions
from dsh_py.services.message import as_text, create_user_message
from dsh_py.services.session import Session

# 用户层 profile 的默认装配点（与 cli 的唯一装配点一致）
_DEFAULT_PROFILE = os.path.join(os.path.dirname(__file__), "configs", "profile.py")

# profile 形参：None（默认装配点）/ .py 文件路径 / 内联 profile 列表
ProfileLike = Union[None, str, list]


@dataclass
class RunResult:
    """一次 ``run()`` 的结果（对标 dsh sdk 的 ``RunResult``）。"""

    session_id: str          # 会话 id（对齐 dsh 的 sessionId）
    final_response: str      # 最终 assistant 文本（对齐 dsh 的 finalResponse，无则 ""）
    events: list             # 会话内全部事件（对齐 dsh 的 events）
    session: Session         # 进程内会话对象（分布式形态没有，本翻译的便利扩展）


def final_response(events: list) -> str:
    """从会话事件中提取最终 assistant 文本（对标 dsh 的 ``finalResponse``）。

    取最后一个 ``assistant/message`` 事件的文本；没有任何 assistant 消息时
    返回空字符串（与 dsh 的 ``''`` 兜底一致）。
    """
    text = ""
    for event in events:
        if event.type == "assistant/message":
            message = event.data.get("message")
            if message is not None:
                text = as_text(message.content)
    return text


def normalize_input(value: Union[str, list]) -> list:
    """归一化 run 输入：字符串 → 单个文本块（对标 dsh 的 ``normalizeInput``）。"""
    if isinstance(value, str):
        return [value]
    return list(value)


class HarnessSession:
    """一个具名会话的编程式句柄（对标 dsh 的 ``HarnessSession``）。"""

    def __init__(self, harness: "DeepSeekHarness", session_id: str) -> None:
        self.harness = harness
        self.id = session_id

    async def run(
        self,
        input: Union[str, list],
        options: Optional[dict] = None,
    ) -> RunResult:
        """往本会话投递一条消息并跑完，返回 :class:`RunResult`。"""
        return await self.harness._run_in_session(self.id, input, options)


class DeepSeekHarness:
    """进程内 Harness 句柄（对标 dsh 的 ``DeepSeekHarness``）。

    :param profile: 用户层 profile——``None`` 用内置唯一装配点
        （``configs/profile.py``），字符串为 .py 文件路径，列表为内联插件清单。
    :param patches: overlay patch 文件路径列表（叠加在用户层之上）。
    """

    def __init__(
        self,
        profile: ProfileLike = None,
        patches: Optional[list[str]] = None,
    ) -> None:
        self._profile = profile
        self._patches = list(patches or [])
        self._ctx: Optional[AppContext] = None
        self._handles: list = []
        self._sessions: dict[str, Session] = {}
        self._closed = False

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #
    @property
    def ctx(self) -> AppContext:
        """装配后的上下文（未装配时抛错，提醒先调用 start()）。"""
        if self._ctx is None:
            raise RuntimeError("DeepSeekHarness 尚未装配：请先 await harness.start()")
        return self._ctx

    def _resolve_user_layer(self) -> list:
        """把 profile 形参解析成用户层插件清单。"""
        if self._profile is None:
            if not os.path.exists(_DEFAULT_PROFILE):
                return []
            return _load_profile_module(_DEFAULT_PROFILE)
        if isinstance(self._profile, str):
            if not os.path.exists(self._profile):
                raise FileNotFoundError(f"profile 文件不存在：{self._profile}")
            return _load_profile_module(self._profile)
        return list(self._profile)

    async def start(self) -> "DeepSeekHarness":
        """幂等装配：bundle 层（核心服务）→ 用户层 → overlays。"""
        if self._ctx is not None:
            return self
        ctx = AppContext()
        layers = [CORE_PROFILE]
        user_layer = self._resolve_user_layer()
        if user_layer:
            layers.append(user_layer)
        for patch_file in self._patches:
            if not os.path.exists(patch_file):
                raise FileNotFoundError(f"--patch 文件不存在：{patch_file}")
            layers.append(_load_profile_module(patch_file))
        self._handles = boot(ctx, *layers)
        self._ctx = ctx
        return self

    # ------------------------------------------------------------------ #
    # 会话与运行
    # ------------------------------------------------------------------ #
    def session(self, session_id: Optional[str] = None) -> HarnessSession:
        """取一个具名会话句柄；缺省 id 时自动生成 ``session-<uuid>``。

        与 dsh 的 ``session(sessionId?)`` 语义一致——只创建/复用句柄，不发起
        装配；真正的装配在首次 ``run()`` 时惰性发生。
        """
        if session_id is None:
            session_id = f"session-{uuid.uuid4().hex}"
        return HarnessSession(self, session_id)

    async def run(
        self,
        input: Union[str, list],
        options: Optional[dict] = None,
    ) -> RunResult:
        """便捷入口：等价于 ``self.session(options.get('sessionId')).run(input, options)``。"""
        options = dict(options or {})
        session_id = options.pop("sessionId", None)
        return await self.session(session_id).run(input, options)

    async def _run_in_session(
        self,
        session_id: str,
        input: Union[str, list],
        options: Optional[dict],
    ) -> RunResult:
        """在指定会话内跑一轮（HarnessSession.run 的落地实现）。"""
        if self._closed:
            raise RuntimeError("DeepSeekHarness 已关闭（close 后不可再 run）")
        await self.start()

        # 会话复用：已有同 id 会话则续跑，否则新建（prepare 直接指定 id，
        # 避免 create 后再改 header 导致持久化文件名不一致）
        session = self._sessions.get(session_id)
        if session is None:
            session = self.ctx.sessions.prepare(session_id=session_id)
            self.ctx.sessions.enter(session)
            persistence = getattr(self.ctx.sessions, "_persistence", None)
            if persistence is not None:
                persistence.create(session.header)  # 对齐 create(persist=True) 的落盘登记
            self._sessions[session_id] = session

        options = dict(options or {})
        agent_options = AgentOptions(
            provider=options.get("provider", ""),
            model=options.get("model", ""),
            system=options.get("system", ""),
            max_tokens=options.get("max_tokens"),
            max_steps=options.get("max_steps", 32),
        )
        agent = self.ctx.agents.create_agent(session, agent_options)
        for block in normalize_input(input):
            await agent.run(block)

        return RunResult(
            session_id=session_id,
            final_response=final_response(session.events),
            events=list(session.events),
            session=session,
        )

    # ------------------------------------------------------------------ #
    # 拆除
    # ------------------------------------------------------------------ #
    async def close(self) -> None:
        """卸载全部插件并回收资源（对标 ``AsyncDisposable`` 的关闭语义）。"""
        if self._closed:
            return
        self._closed = True
        for handle in reversed(self._handles):
            handle.dispose()
        self._handles = []
        self._ctx = None
        self._sessions.clear()

    async def dispose(self) -> None:
        """别名：与 ``close`` 相同（对标 dsh 的 ``Symbol.asyncDispose``）。"""
        await self.close()


def _load_profile_module(path: str) -> list:
    """从 .py 文件加载 PROFILE 列表（返回列表本身，装配交给 boot）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("profile_mod", path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module.PROFILE

"""subagent-acp 插件：把外进程 ACP agent 注册为 ``ctx.subagents`` 的具名路由
（对标 dsh 的 ``@deepseek-ai/dsh-subagent-acp``）。

每个子代理拥有自己的进程、会话、模型与工具，不共享任何 Cordis 上下文，也不
宣传父强制的启动能力；它从父请求读取的唯一一件事是会话的 workspace cwd
（见 :func:`resolve_cwd`）。配置：

- ``providerName``：``ctx.subagents`` 上的路由名（默认 ``acp``）。
- ``command`` / ``args``：每次运行 spawn 的子代理可执行文件与参数。
- ``cwd``：子进程与 ACP 会话的工作目录覆盖；省略时继承父会话 cwd（父会话
  无 cwd 时启动失败）。
- ``permission``：如何自动应答子的 ``request/permission`` 提示——``reject``
  （默认，拒绝每个提示）或 ``allow``（经首个 ``allow_once``/``allow_always``
  选项批准）。任何提示都不向人类展示。
- ``env``：追加给子进程的环境（如子 harness 自己的 ``DEEPSEEK_API_KEY``），
  叠加在凭证擦除的父环境副本之上。
- ``disposeEofGraceMs`` / ``disposeGraceMs``：stdin EOF 静默与终止升级宽限。
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

from dsh_py.core.context import AppContext
from dsh_py.services.subagent_acp import (
    AcpRunSpec,
    DEFAULT_DISPOSE_EOF_GRACE_MS,
    DEFAULT_DISPOSE_GRACE_MS,
    start_acp_run,
)
from dsh_py.services.subagents import (
    SubagentCapabilities,
    SubagentProvider,
    SubagentRun,
)

#: 单次 Node 计时器拥有的回收层级上限（对齐 dsh 的 MAX_TIMER_DELAY_MS）。
_MAX_TIMER_DELAY_MS = 2_147_483_647


def _warn(ctx: AppContext, message: str) -> None:
    """防御性告警：无 logger 服务（裸 ctx）时静默。"""
    try:
        ctx.logger.warn(message)
    except (AttributeError, Exception):  # noqa: BLE001
        pass


def assert_positive_finite(name: str, value: float) -> None:
    """dispose 宽限必须落在单个计时器可承载的有界正数内。"""
    if not (isinstance(value, (int, float)) and value > 0 and value <= _MAX_TIMER_DELAY_MS):
        raise RuntimeError(
            f"subagent-acp: {name} must be a positive finite number no greater than {_MAX_TIMER_DELAY_MS}"
        )


def is_directory(path: str) -> bool:
    """``path`` 是否是可进入的既有目录（含搜索权限探针——子进程 cwd 需要 X_OK）。"""
    try:
        if not os.path.isdir(path):
            return False
        if not os.access(path, os.X_OK):
            return False
        return True
    except OSError:
        return False


def assert_usable_cwd(label: str, cwd: str) -> str:
    """断言 ``cwd`` 真能承载子进程：绝对路径（兼作 ACP 会话 workspace，相对
    路径会重锚到服务器进程启动目录）且为可进入目录（在进程边界之前失败）。"""
    if not os.path.isabs(cwd):
        raise RuntimeError(f"subagent-acp: {label} must be an absolute path: {cwd}")
    if not is_directory(cwd):
        raise RuntimeError(f"subagent-acp: {label} is not an accessible directory: {cwd}")
    return cwd


def resolve_cwd(configured: Optional[str], request: dict) -> str:
    """解析子进程工作目录：配置 ``cwd`` 覆盖优先，否则父会话的 workspace cwd。

    两者皆缺时明确失败——回退到 harness 进程 cwd 会把子代理悄悄绑到服务器
    启动目录，而非委托会话的工作区（一个服务器进程服务多个会话，各有 cwd）。
    """
    if configured is not None:
        return configured
    parent = request.get("parent")
    header = getattr(getattr(parent, "session", None), "header", None)
    parent_cwd = getattr(header, "cwd", None) if header is not None else None
    if not parent_cwd:
        raise RuntimeError(
            "subagent-acp: no working directory for the child — configure `cwd` "
            "or delegate from a parent session that has one"
        )
    return assert_usable_cwd("parent session cwd", parent_cwd)


class AcpProvider:
    """ACP 子代理 provider：宣传**零**启动能力（外进程子代理无法兑现
    outputSchema 等——服务会在 ``start`` 前拒绝需要它们的请求）。"""

    name: str
    capabilities: SubagentCapabilities
    inherits_parent_context = False

    def __init__(self, ctx: AppContext, config: dict) -> None:
        self.name = config.get("providerName") or "acp"
        self.capabilities = SubagentCapabilities(output_schema=False)
        self.ctx = ctx
        self.config = config

    async def start(self, request: dict) -> SubagentRun:
        """按启动请求发起一次 ACP 子进程运行（spawn 走 ``ctx.subprocess``）。"""
        spec = AcpRunSpec(
            command=self.config["command"],
            args=self.config.get("args") or (),
            cwd=resolve_cwd(self.config.get("cwd"), request),
            permission=self.config.get("permission") or "reject",
            env=self.config.get("env"),
            dispose_eof_grace_ms=self.config.get("disposeEofGraceMs") or DEFAULT_DISPOSE_EOF_GRACE_MS,
            dispose_grace_ms=self.config.get("disposeGraceMs") or DEFAULT_DISPOSE_GRACE_MS,
            spawn=lambda sp: self.ctx.subprocess.spawn(sp),
            on_error=lambda error, stop_reason: _warn(
                self.ctx,
                f'subagent-acp "{self.name}": child run failed ({stop_reason}): {error}',
            ),
        )
        return await start_acp_run(request, spec)


def apply(ctx: AppContext, config: Any = None) -> None:
    """注册 ``acp`` 子代理路由（provider 描述 + 外进程 backend）。"""
    config = config or {}
    provider_name = config.get("providerName") or "acp"
    command = config.get("command")
    if not command:
        raise RuntimeError("subagent-acp: command is required")
    dispose_eof = config.get("disposeEofGraceMs", DEFAULT_DISPOSE_EOF_GRACE_MS)
    dispose_grace = config.get("disposeGraceMs", DEFAULT_DISPOSE_GRACE_MS)
    assert_positive_finite("disposeEofGraceMs", dispose_eof)
    assert_positive_finite("disposeGraceMs", dispose_grace)
    # 空串会悄悄重新引入启动目录回退——显式拒绝。
    cwd = config.get("cwd")
    if cwd == "":
        raise RuntimeError(
            "subagent-acp: config cwd must not be empty — omit the key to inherit the parent session cwd"
        )
    # 相对配置 cwd 在加载时按启动目录解析一次；坏目录在这里失败而非每次 start。
    if cwd is not None:
        cwd = assert_usable_cwd("config cwd", os.path.abspath(cwd))
    resolved = dict(config)
    resolved["cwd"] = cwd
    resolved["args"] = tuple(config.get("args") or ())
    resolved["disposeEofGraceMs"] = dispose_eof
    resolved["disposeGraceMs"] = dispose_grace

    subagents = ctx.subagents
    provider = AcpProvider(ctx, resolved)
    subagents.register_provider(
        SubagentProvider(
            name=provider_name,
            capabilities=provider.capabilities,
            inherits_parent_context=provider.inherits_parent_context,
        )
    )
    subagents.register_backend(provider_name, provider.start)


apply.name = "subagent-acp"
apply.inject = ["subagents", "subprocess"]


__all__ = [
    "AcpProvider", "apply", "assert_positive_finite", "is_directory",
    "assert_usable_cwd", "resolve_cwd",
]

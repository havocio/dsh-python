"""subagent-acp：外进程 ACP 子代理运行驱动（对标 dsh 的 ``@deepseek-ai/dsh-subagent-acp/run``）。

每个 ACP 子代理拥有自己的进程、会话、模型与工具，不共享任何 Cordis 上下文；
它从父请求读取的唯一一件事是会话的 workspace cwd（见插件的 ``resolve_cwd``）。
本模块只做「驱动一次 ACP 子进程运行」：spawn → ACP 握手（initialize +
sessions/new）→ prompt → 收集 ``agent_message_chunk`` 文本 → 折叠停止原因；
dispose 走「stdin EOF 静默 → 终止升级」的整树回收阶梯。

与 dsh 的差异（详见 README §11）：
- 客户端连接用 :class:`dsh_py.services.acp.AcpClientConnection`（自写 stdio
  ndjson JSON-RPC），不依赖第三方 ACP SDK；
- 输出折叠 :class:`OutputFold` 为 dsh ``AssistantOutputFold`` 的本地对应
  （push_text / collect 累积文本块）；
- 句柄 :class:`AcpSubagentRun` 在既有 :class:`SubagentRun` 上覆写
  ``dispose``，追加进程回收阶梯；启动/运行失败一律折叠进
  ``stopReason``（dsh_py seam 约定 result 永不拒绝），原始错误经
  ``spec.on_error`` 保留给诊断。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

from dsh_py.services.acp import AcpClientConnection
from dsh_py.services.subagents import SubagentResult, SubagentRun
from dsh_py.services.subprocess import SubprocessSpawnSpec, SubprocessStdio
from dsh_py.services.workflow.types import SessionId

#: 权限自动应答策略：``reject`` 拒绝每个提示（默认），``allow`` 经首个
#: ``allow_once``/``allow_always`` 选项批准。任何提示都不向人类展示。
PermissionPolicy = Literal["allow", "reject"]

#: stdin EOF 静默宽限：子代理刷新持久化并回收自身嵌套进程的窗口（毫秒）。
DEFAULT_DISPOSE_EOF_GRACE_MS = 6_000

#: 终止升级宽限：POSIX SIGTERM → SIGKILL 之间等待（毫秒）；Windows 直接强制终止。
DEFAULT_DISPOSE_GRACE_MS = 3_000


@dataclass(frozen=True)
class AcpRunSpec:
    """已解析的 ACP 子进程 spawn 规格（无默认值——见插件 Config）。"""

    command: str
    args: tuple = ()
    cwd: str = ""
    permission: PermissionPolicy = "reject"
    env: Optional[dict] = None
    dispose_eof_grace_ms: int = DEFAULT_DISPOSE_EOF_GRACE_MS
    dispose_grace_ms: int = DEFAULT_DISPOSE_GRACE_MS
    spawn: Any = None
    on_error: Optional[Callable[[Exception, str], None]] = None


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #


def acp_stop_reason(reason: Any) -> str:
    """把 ACP ``stopReason`` 映射为 harness 的子代理停止原因。

    ``max_turn_requests``（子代理触达回合请求预算）与任何未知未来变体都映射
    为 ``error``——不洁停止绝不报告为 ``completed``。
    """
    if reason == "end_turn":
        return "completed"
    if reason == "max_tokens":
        return "max-tokens"
    if reason == "refusal":
        return "refusal"
    if reason == "cancelled":
        return "aborted"
    return "error"


def acp_content_text(block: Any) -> str:
    """取 ACP 内容块文本；非 text 块贡献空串。"""
    if isinstance(block, dict):
        return block.get("text", "") if block.get("type") == "text" else ""
    if getattr(block, "type", None) == "text":
        return str(getattr(block, "text", ""))
    return ""


def to_acp_prompt(prompt: Any) -> list[dict]:
    """把 harness 提示块翻译为 ACP prompt 块（仅 text；其余丢弃）。"""
    blocks: list[dict] = []
    for block in prompt or []:
        if isinstance(block, dict):
            if block.get("type") == "text":
                blocks.append({"type": "text", "text": block.get("text", "")})
        elif getattr(block, "type", None) == "text":
            blocks.append({"type": "text", "text": str(getattr(block, "text", ""))})
    return blocks


class OutputFold:
    """ACP 输出折叠：把流式 ``agent_message_chunk`` 文本**拼接**为完整输出。

    对齐 dsh 的 ``AssistantOutputFold``——每次读取都返回最新累积（部分答案
    在后续取消/错误时仍存活），chunk 按到达顺序拼接成单一文本块。
    """

    def __init__(self) -> None:
        self._text = ""

    def push_text(self, text: str) -> None:
        if text:
            self._text += text

    def collect(self) -> list[dict]:
        return [{"type": "text", "text": self._text}] if self._text else []


# --------------------------------------------------------------------------- #
# 进程回收阶梯
# --------------------------------------------------------------------------- #


async def tree_exits_within(child: Any, ms: int) -> bool:
    """有界等待整树退出：``ms`` 毫秒内退出返回 True，超时返回 False。"""
    try:
        return bool(await asyncio.wait_for(child.wait_for_exit(), timeout=ms / 1000))
    except asyncio.TimeoutError:
        return False


async def _await_protocol_streams(child: Any, timeout: float = 10.0) -> tuple:
    """等待实现异步填充管道流（dsh_py 的 ``LocalSubprocessRuntime.spawn``
    同步返回占位句柄，stdin/stdout 由后台任务稍后填充）。

    spawn 级失败（``child.done`` 已以异常结算）直接传播原始错误；超时返回
    ``(None, None)`` 让调用方按「丢流」报错。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    done = getattr(child, "done", None)
    while getattr(child, "stdin", None) is None or getattr(child, "stdout", None) is None:
        if done is not None and done.done():
            if not done.cancelled():
                exc = done.exception()
                if exc is not None:
                    raise exc
            break
        if loop.time() >= deadline:
            break
        await asyncio.sleep(0.02)
    return getattr(child, "stdin", None), getattr(child, "stdout", None)


async def dispose_acp_child(child: Any, eof_grace_ms: int) -> None:
    """协作式回收阶梯（仅用 seam 的公开动词）：stdin EOF → 等树退出 →
    超时后 terminate() 升级 → 等退出。只在整树静默后解析。

    :param child: 已 spawn 的 ACP 子进程句柄。
    :param eof_grace_ms: 第一级窗口——stdin EOF 后给子代理刷新持久化与
        回收自身嵌套子进程的宽限。
    """
    if child.pid <= 0:
        # spawn 失败没有进程可回收；观察拒绝以免 finally 中未处理。
        try:
            await child.done
        except Exception:  # noqa: BLE001
            pass
        return
    stdin = getattr(child, "stdin", None)
    if stdin is not None:
        try:
            stdin.write_eof()
        except (NotImplementedError, OSError, RuntimeError):  # noqa: PERF203
            try:
                stdin.close()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
    if await tree_exits_within(child, eof_grace_ms):
        return
    # terminate() 拥有有界的 SIGTERM→SIGKILL 计时器；其无界等待是进程所有者的
    # 退出证明，不是可溢出的第二层宽限。
    child.terminate()
    await child.wait_for_exit()


# --------------------------------------------------------------------------- #
# 运行驱动
# --------------------------------------------------------------------------- #


class AcpSubagentRun(SubagentRun):
    """外进程 ACP 运行的句柄：dispose 在取消任务后追加进程回收阶梯。"""

    def __init__(self, session_id: str, task: asyncio.Task,
                 on_dispose: Callable[[], Any]) -> None:
        super().__init__(session_id, task)
        self._on_dispose = on_dispose

    async def dispose(self) -> None:
        await super().dispose()
        try:
            await self._on_dispose()
        except Exception:  # noqa: BLE001 - 释放绝不抛错
            pass


async def start_acp_run(request: dict, spec: AcpRunSpec) -> SubagentRun:
    """在初始化与会话建立后启动并发布一个 ACP 子代理运行。

    子代理级失败经运行结果解析；启动失败在进程回收后抛出。dispose 取消、
    终止并回收子进程。

    :param request: 启动请求 ``{"prompt": 块, "signal": CancelSignal, ...}``；
        其 signal 是取消通道。
    :param spec: 已解析的 spawn 规格（command/args/cwd、permission 策略、
        dispose 宽限与可选错误槽）。
    :returns: 就绪的运行句柄。
    """
    signal = request.get("signal")
    if signal is not None and getattr(signal, "aborted", False):
        raise RuntimeError("subagent request was aborted before the ACP child started")
    # ACP 会话 id 只在子服务器内唯一；生命周期 id 在父命名空间铸造，使新进程
    # 之间、与恰好使用同 id 的本地 agent 之间都不会碰撞。
    run_id = SessionId(str(uuid.uuid4()))

    if spec.spawn is None:
        raise RuntimeError("subagent-acp: no spawn function in spec")
    child = spec.spawn(SubprocessSpawnSpec(
        argv=(spec.command, *spec.args),
        cwd=spec.cwd,
        stdio=SubprocessStdio(stdin="pipe", stdout="pipe", stderr="inherit"),
        graceMs=spec.dispose_grace_ms,
        env=spec.env,
    ))
    child_stdin, child_stdout = await _await_protocol_streams(child)
    if child_stdin is None or child_stdout is None:
        raise RuntimeError("subagent-acp: subprocess implementation dropped a piped protocol stream")

    # spawn 级失败以拒绝进入启动竞速；干净退出绝不能赢得它，故成功臂永久挂起。
    spawn_failed: asyncio.Future = asyncio.get_running_loop().create_future()

    async def _watch_spawn() -> None:
        try:
            await child.done
            await asyncio.Event().wait()  # 挂起：干净退出不赢得竞速
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - spawn 级失败
            if not spawn_failed.done():
                spawn_failed.set_exception(exc)

    watch_task = asyncio.create_task(_watch_spawn())

    # 启动回滚与已发布句柄共享同一次进程回收（幂等）。
    process_disposal: Optional[asyncio.Future] = None

    def dispose_process() -> asyncio.Future:
        nonlocal process_disposal
        if process_disposal is None:
            async def _dispose() -> None:
                watch_task.cancel()
                await dispose_acp_child(child, spec.dispose_eof_grace_ms)
            process_disposal = asyncio.ensure_future(_dispose())
        return process_disposal

    fold = OutputFold()
    flags = {"cancelled": False}
    cancel_settled: asyncio.Future = asyncio.get_running_loop().create_future()
    session_id: Optional[str] = None

    def _on_session_update(params: Any) -> None:
        update = params.get("update") or {}
        if update.get("sessionUpdate") == "agent_message_chunk":
            fold.push_text(acp_content_text(update.get("content")))
        # 其他更新（thoughts/工具调用/计划）消费但不上浮——子代理只返回最终答案。

    conn = AcpClientConnection(
        reader=child_stdout,
        writer=child_stdin,
        on_session_update=_on_session_update,
        permission=spec.permission,
    )

    async def _best_effort_cancel() -> None:
        sid = session_id
        if sid is not None:
            try:
                await conn.cancel(sid)
            except Exception:  # noqa: BLE001 - 子代理已消失/无会话
                pass

    def request_cancel() -> None:
        if flags["cancelled"]:
            return
        flags["cancelled"] = True
        if not cancel_settled.done():
            cancel_settled.set_result(None)
        # 尽力而为的 ACP cancel；进程回收仍具权威。
        asyncio.create_task(_best_effort_cancel())

    remove_abort: Any = None
    if signal is not None and hasattr(signal, "add_listener"):
        remove_abort = signal.add_listener(request_cancel)

    def _remove_abort() -> None:
        if remove_abort is not None:
            try:
                remove_abort()
            except Exception:  # noqa: BLE001
                pass

    # 建立远端会话后才发布句柄；任何失败拥有仍私有的进程，回收后才抛出。
    try:
        async def _startup() -> None:
            await conn.initialize(client_capabilities={})
            session = await conn.new_session(cwd=spec.cwd, mcp_servers=[])
            sid = session.get("sessionId") if isinstance(session, dict) else getattr(session, "sessionId", None)
            if not isinstance(sid, str):
                raise RuntimeError("ACP child published without a session id")
            nonlocal session_id
            session_id = sid
            if flags["cancelled"]:
                raise RuntimeError("subagent cancelled before the ACP session started")

        async def _cancel_racer() -> None:
            # shield：本 racer 被取消时不得连带取消 cancel_settled（asyncio 的
            # Task.cancel() 会传播到正在 await 的 future），否则后续阶段会误判已取消。
            await asyncio.shield(cancel_settled)
            raise RuntimeError("subagent cancelled before the ACP session started")

        startup_task = asyncio.create_task(_startup())
        cancel_task = asyncio.create_task(_cancel_racer())
        done, pending = await asyncio.wait(
            {startup_task, spawn_failed, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for item in pending:
            item.cancel()
        failure: Optional[Exception] = None
        for item in done:
            if item.cancelled():
                continue
            exc = item.exception()
            if exc is not None and failure is None:
                failure = exc
        if failure is not None:
            raise failure
    except asyncio.CancelledError:
        _remove_abort()
        await dispose_process()
        raise
    except Exception as exc:  # noqa: BLE001
        _remove_abort()
        await dispose_process()
        if flags["cancelled"]:
            raise RuntimeError("subagent request was aborted before the ACP child started") from exc
        raise

    if session_id is None:  # pragma: no cover - 启动事务已校验
        raise RuntimeError("unreachable: ACP startup fulfilled without a session id")
    remote_session_id = session_id

    async def _prompt() -> SubagentResult:
        prompt_result = await conn.prompt(
            remote_session_id, to_acp_prompt(request.get("prompt") or [])
        )
        return SubagentResult(
            output=fold.collect(),
            stopReason=acp_stop_reason(prompt_result.get("stopReason")),
        )

    async def _cancel_result() -> SubagentResult:
        # 同上 shield：prompt 正常完成后对 pending racer 的取消不得污染 cancel_settled。
        await asyncio.shield(cancel_settled)
        return SubagentResult(output=fold.collect(), stopReason="aborted")

    async def _main() -> SubagentResult:
        try:
            prompt_task = asyncio.create_task(_prompt())
            cancel_task = asyncio.create_task(_cancel_result())
            done, pending = await asyncio.wait(
                {prompt_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for item in pending:
                item.cancel()
            if prompt_task in done and not prompt_task.cancelled() and prompt_task.exception() is None:
                return prompt_task.result()
            if flags["cancelled"]:
                return SubagentResult(output=fold.collect(), stopReason="aborted")
            # 发布后传输失败折叠为 error，同时保留诊断（错误槽绝不拒绝 result）。
            try:
                exc = prompt_task.exception()
                if exc is not None and spec.on_error is not None:
                    spec.on_error(exc, "error")
            except Exception:  # noqa: BLE001
                pass
            return SubagentResult(output=fold.collect(), stopReason="error")
        finally:
            _remove_abort()

    task = asyncio.create_task(_main())
    return AcpSubagentRun(str(run_id), task, on_dispose=dispose_process)


__all__ = [
    "AcpRunSpec", "PermissionPolicy", "DEFAULT_DISPOSE_EOF_GRACE_MS",
    "DEFAULT_DISPOSE_GRACE_MS", "OutputFold", "acp_stop_reason",
    "acp_content_text", "to_acp_prompt", "dispose_acp_child",
    "tree_exits_within", "AcpSubagentRun", "start_acp_run",
]

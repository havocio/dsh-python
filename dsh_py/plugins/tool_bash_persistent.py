"""持久 bash 工具（tool-bash-persistent，对标 dsh 的 ``dsh-tool-bash-persistent``）：
把 ``ctx.terminals`` 的持久 shell 会话暴露为模型可调用的 ``bash`` 工具——**每个
owner（调用方 agent）一个持久 shell，工作目录与环境变量跨调用保留**。

与一次性 ``tool-bash``（每条命令都起新进程）的区别：本工具复用同一会话，因此
``cd`` / ``export`` 等状态在多次调用间持续。

实现要点（零外部依赖，复用 ``ctx.terminals`` 持久会话）：
- 用**唯一标记**（nonce 起止串）包裹命令，从合并了 stderr 的输出里抽取命令
  stdout 与退出码（dsh_py 的终端会话把 stderr 并入 stdout，故不单列 stderr）；
- 轮询 ``read_available`` 直到命中结束标记，超时则重置 shell 并返回部分输出；
- 输出超长按 ``maxOutputChars`` 截断；
- 每个 owner 串行执行（asyncio.Lock），避免同一 shell 上的命令交错。

注：持久 shell 本质是长时间存活的 bash 进程，故要求 POSIX shell（Windows 上
探测到 Git Bash 即使用 ``bash``，否则回退 ``cmd``；``cmd`` 不支持本工具的
``eval``/标记语法，需显式配置 ``shell: bash``）。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.services.shell_env import collect_for, merge_env

#: 单页滚动缓冲读取的轮询间隔（毫秒，转秒后使用）。
_POLL_INTERVAL_S = 0.025
#: 一次命令允许的最大挂起时间（默认 5s 兜底，实际以 timeoutMs 为准）。
_MAX_SETTLE_S = 5.0

TRUNCATED_MESSAGE = (
    "<response clipped><NOTE>为节省上下文仅展示部分输出；"
    "若需更多内容请用更精确的命令（如 grep -n）定位行号后重试。</NOTE>"
)
LOST_PREFIX_MESSAGE = (
    "<response clipped><NOTE>命令开头部分输出因终端滚动上限被丢弃，以下为最早保留的片段。</NOTE>\n"
)
SHELL_RESET_MESSAGE = (
    "持久 bash shell 已被重置；下一次 bash 调用将从工作区以全新的当前目录与环境开始。"
)
SHELL_PROMPT = "__DSH_PERSISTENT_BASH_PROMPT__ "
TIMEOUT_CODE = "PERSISTENT_BASH_TIMEOUT"

DEFAULT_DESCRIPTION = (
    "在持久 bash shell 中运行命令。状态（含当前目录与导出的环境变量）在本 agent 的多次调用间持续保留。"
)


def _markers() -> dict[str, str]:
    """为一次命令生成唯一起止标记（防止用户输出里出现同名串）。"""
    nonce = uuid.uuid4().hex
    return {
        "start": f"__DSH_PB_START_{nonce}__",
        "end": f"__DSH_PB_END_{nonce}:",
    }


def _quote_for_bash(value: str) -> str:
    """把任意字符串转成单引号安全、支持换行的 bash 字面量（``$'...'`` 形式）。"""
    escaped = (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f"$'{escaped}'"


def _wrap_command(command: str, marker: dict[str, str]) -> str:
    """把命令包进起止标记并捕获 ``$?``（整行写入，避免交互式 bash 的 PS2 泄漏提示符）。"""
    return (
        f"printf '%s\\n' {_quote_for_bash(marker['start'])}; "
        f"eval -- {_quote_for_bash(command)}; __dsh_pb_status=$?; "
        f"printf '%s%s\\n' {_quote_for_bash(marker['end'])} \"$__dsh_pb_status\""
    )


def _strip_prompt(text: str) -> str:
    result = text
    if result.endswith("\n"):
        result = result[:-1]
    while result.endswith(SHELL_PROMPT):
        result = result[: -len(SHELL_PROMPT)]
    return result[:-1] if result.endswith("\n") else result


def _command_output(snapshot: str, marker: dict[str, str]) -> Optional[dict]:
    """从累计输出中解析命令 stdout 与退出码；未命中结束标记返回 None。"""
    end = snapshot.rfind(marker["end"])
    if end < 0:
        return None
    after_end = snapshot[end + len(marker["end"]):]
    status_match = __import__("re").match(r"^(\d+)\r?\n", after_end)
    if status_match is None:
        return None
    status = int(status_match.group(1))
    start = snapshot.rfind(marker["start"], 0, end)
    body = snapshot[start + len(marker["start"]):end] if start >= 0 else ""
    body = body[1:] if body.startswith("\n") else body
    return {
        "text": _strip_prompt(body),
        "incomplete": start < 0,
        "exit_code": status,
    }


def _maybe_truncate(content: str, max_output_chars: int, incomplete: bool = False) -> str:
    if len(content) <= max_output_chars and not incomplete:
        return content
    if len(content) <= max_output_chars:
        return content + TRUNCATED_MESSAGE
    return content[:max_output_chars] + TRUNCATED_MESSAGE


def _partial_output(snapshot: str, marker: dict[str, str], fallback: str, fallback_truncated: bool) -> dict:
    """超时/未完整时的部分输出：优先用带起止标记的文本，否则退回 fallback。"""
    start = snapshot.rfind(marker["start"])
    if start >= 0:
        text = snapshot[start + len(marker["start"]):]
        text = text[1:] if text.startswith("\n") else text
        return {"text": _strip_prompt(text), "incomplete": False}
    fb_start = fallback.rfind(marker["start"])
    after = fallback[fb_start + len(marker["start"]):] if fb_start >= 0 else fallback
    after = after[1:] if after.startswith("\n") else after
    fb_end = after.rfind(marker["end"])
    before = after[:fb_end] if fb_end >= 0 else after
    return {"text": _strip_prompt(before.replace(SHELL_PROMPT, "")), "incomplete": fallback_truncated or fb_start < 0}


def _render_captured(output: dict, max_output_chars: int) -> str:
    rendered = _maybe_truncate(output["text"], max_output_chars, output["incomplete"])
    if output["incomplete"] and output["text"]:
        rendered = LOST_PREFIX_MESSAGE + rendered
    if output.get("exit_code") is not None and output["exit_code"] != 0:
        rendered = f"{rendered}\n[exit code: {output['exit_code']}]" if rendered else f"[exit code: {output['exit_code']}]"
    return rendered


class _PersistentShells:
    """按 owner 维护持久 shell 会话（每 owner 一个）并提供重置。"""

    def __init__(self, ctx: AppContext, config: dict) -> None:
        self.ctx = ctx
        self.config = config
        self._live: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _owner_id(self, owner: Any) -> str:
        return str(getattr(owner, "id", owner))

    def _lock_for(self, owner_id: str) -> asyncio.Lock:
        lock = self._locks.get(owner_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[owner_id] = lock
        return lock

    def _spawn(self, owner: Any, env: Optional[dict] = None) -> Any:
        cwd = None
        try:
            cwd = owner.session.header.cwd
        except Exception:  # noqa: BLE001 - owner 无会话时回退 None（进程 cwd）
            cwd = None
        session = self.ctx.terminals.spawn(cwd=cwd, env=env)
        return session

    def get(self, owner: Any, env: Optional[dict] = None) -> Any:
        """返回 owner 的持久 shell（不存在则新建）。

        :param env: 合并后的完整环境（含受信任 ``DSH_*`` 快照）；仅在**新建**会话时
            生效——持久 shell 的语义就是环境跨调用保留，不逐次重注入。

        注意：本方法不加锁——调用方（``_execute_command``）须持有对应 owner 的锁，
        以保证「检查-创建-执行」整体串行（``asyncio.Lock`` 不可重入）。
        """
        owner_id = self._owner_id(owner)
        session = self._live.get(owner_id)
        if session is None or session.closed:
            if session is not None:
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    pass
            session = self._spawn(owner, env)
            self._live[owner_id] = session
        return session

    async def reset(self, owner: Any, reason: str) -> None:
        """关闭并丢弃 owner 的持久 shell（重置）。

        注意：本方法**不加锁**——调用方（``_execute_command_locked``）已持有对应 owner
        的锁，避免与自身重入死锁（``asyncio.Lock`` 不可重入）。
        """
        owner_id = self._owner_id(owner)
        session = self._live.pop(owner_id, None)
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - 重置绝不抛错
                pass


async def _collect_until(session: Any, end_marker: str, timeout_s: float) -> tuple[str, bool]:
    """轮询读取直到命中结束标记或超时；返回 (累计输出, 是否超时)。"""
    deadline = asyncio.get_event_loop().time() + timeout_s
    buf = session.read_available()  # 先清空上一轮的残留
    while True:
        if end_marker in buf or session.closed:
            return buf, False
        if asyncio.get_event_loop().time() >= deadline:
            return buf, True
        await asyncio.sleep(_POLL_INTERVAL_S)
        buf += session.read_available()


async def _execute_command(
    shells: _PersistentShells, owner: Any, command: str, config: dict, signal: Any,
    env: Optional[dict] = None,
) -> str:
    """在 owner 的持久 shell 上执行一条命令并返回渲染后的文本。

    同一 owner 串行（``asyncio.Lock`` 不可重入，故锁在此处整体持有，``get`` 不再加锁）。
    """
    owner_id = shells._owner_id(owner)
    async with shells._lock_for(owner_id):
        return await _execute_command_locked(shells, owner, command, config, signal, env)


async def _execute_command_locked(
    shells: _PersistentShells, owner: Any, command: str, config: dict, signal: Any,
    env: Optional[dict] = None,
) -> str:
    """持有 owner 锁后执行（见 ``_execute_command``）。"""
    session = shells.get(owner, env)
    marker = _markers()
    wrapped = _wrap_command(command, marker) + "\n"
    fallback = ""
    fallback_truncated = False

    # 取消信号：提前中止并重置会话。
    if signal is not None and getattr(signal, "aborted", False):
        await shells.reset(owner, "persistent bash aborted before send")
        return "错误：命令在开始前已被取消"

    try:
        session.write(wrapped)
    except RuntimeError as exc:
        await shells.reset(owner, "persistent bash write failed")
        return f"错误：{exc}"

    try:
        snapshot, timed_out = await _collect_until(session, marker["end"], config["timeoutMs"] / 1000.0)
    except Exception as exc:  # noqa: BLE001
        await shells.reset(owner, "persistent bash collect failed")
        return f"错误：{exc}"

    if timed_out:
        partial = _render_captured(
            _partial_output(snapshot, marker, fallback, fallback_truncated), config["maxOutputChars"]
        )
        await shells.reset(owner, "persistent bash command timed out")
        seconds = round(config["timeoutMs"] / 1000.0)
        return f"命令在约 {seconds} 秒后超时（或遭遇 OOM）。以下是部分输出：\n{partial}\n{SHELL_RESET_MESSAGE}"

    if session.closed:
        # 命令把 shell 本身搞挂了：按 shell 退出处理并重置。
        captured = _command_output(snapshot, marker) or {"text": "", "incomplete": False, "exit_code": None}
        await shells.reset(owner, "persistent bash shell exited")
        msg = _render_captured(captured, config["maxOutputChars"])
        return f"{msg}\n[SHELL_EXITED]\n{SHELL_RESET_MESSAGE}"

    captured = _command_output(snapshot, marker)
    if captured is None:
        # 理论上 collect_until 命中了 end 标记，不该走到这里；兜底返回原始输出。
        return _render_captured({"text": snapshot, "incomplete": False, "exit_code": None}, config["maxOutputChars"])
    return _render_captured(captured, config["maxOutputChars"])


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 owner 范围的持久 ``bash`` 工具（依赖 ``ctx.terminals``）。"""
    config = config or {}
    resolved = {
        "backendType": str(config.get("backendType") or "shell"),
        "timeoutMs": int(config.get("timeoutMs") if config.get("timeoutMs") is not None else 300_000),
        "maxOutputChars": int(config.get("maxOutputChars") if config.get("maxOutputChars") is not None else 16_000),
        "description": str(config.get("description") or DEFAULT_DESCRIPTION),
    }
    if not resolved["backendType"].strip():
        raise ValueError("tool-bash-persistent: backendType 不能为空")
    if not (resolved["timeoutMs"] > 0):
        raise ValueError("tool-bash-persistent: timeoutMs 必须是正整数")
    if not (resolved["maxOutputChars"] > 0):
        raise ValueError("tool-bash-persistent: maxOutputChars 必须是正整数")
    if not resolved["description"].strip():
        raise ValueError("tool-bash-persistent: description 不能为空")

    shells = _PersistentShells(ctx, resolved)

    async def bash_handler(args: dict, exec: dict) -> tuple:
        command = args.get("command", "")
        if not command.strip():
            return "错误：command 必须是非空字符串", True
        owner = exec.get("agent")
        if owner is None:
            return "错误：bash 需要一个持有会话的调用方 agent", True
        signal = exec.get("signal")
        # 受信任 DSH_* 快照：合并进完整进程环境后注入（shellEnv 未挂载则 None，继承父进程）。
        snapshot = collect_for(ctx, exec)
        spawn_env = merge_env(dict(os.environ), snapshot) if snapshot is not None else None
        # 同一 owner 串行由 _execute_command 内部持锁保证（避免与 get 重入死锁）。
        try:
            result = await _execute_command(shells, owner, command, resolved, signal, spawn_env)
        except Exception as exc:  # noqa: BLE001
            return f"错误：{exc}", True
        # _execute_command 已在超时/退出时返回带重置说明的文本（视为正常结果）。
        return result, False

    ctx.tools.register("bash", resolved["description"], {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要运行的 bash 命令（非空）。相对路径优先。"},
        },
        "required": ["command"],
    }, bash_handler)


apply.provides = ["toolBashPersistent"]
apply.inject = ["tools", "terminals"]

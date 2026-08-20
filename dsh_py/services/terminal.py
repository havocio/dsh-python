"""终端服务（terminal seam，对标 dsh 的 ``dsh-terminal`` 本地子集）：持有者范围的
持久 shell 会话注册表。

dsh 用 PTY 会话（``terminal-bash`` 的伪终端）；本实现为零依赖本地替代——持久
``Popen`` 会话 + daemon reader 线程的行缓冲读写，同一会话内多次 ``send`` 保持
工作目录与环境。差异（PTY 信号、交互式提示符检测）在文档中注明。

- :meth:`TerminalService.spawn` —— 启动一个会话（返回 id 与句柄）；
- :meth:`TerminalSession.send` —— 写入一条命令并等待输出静默，返回新输出；
- :meth:`TerminalSession.close` —— 终止会话并回收。

会话注册表按会话 id 管理，``ctx.terminals`` 提供服务。
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service


def _detect_shell() -> str:
    """探测交互 shell：Windows 优先 Git Bash，回退 cmd；POSIX 用 /bin/bash。"""
    if os.name == "nt":
        bash = shutil.which("bash")
        return bash or "cmd"
    return "/bin/bash"


class TerminalSession:
    """一次持久 shell 会话（Popen + daemon reader 线程行缓冲）。"""

    def __init__(self, session_id: str, cwd: Optional[str] = None, shell: Optional[str] = None) -> None:
        self.id = session_id
        self.cwd = cwd
        self.shell = shell or _detect_shell()
        self._output: queue.Queue = queue.Queue()  # 行缓冲（reader 线程产出）
        self._closed = False
        # 会话内累计输出（含已消费部分，供调试/快照）
        self.buffer: list[str] = []
        self._lock = threading.Lock()
        self._proc = subprocess.Popen(
            [self.shell],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        """daemon 读取线程：逐行读 stdout 并入队。"""
        try:
            for line in self._proc.stdout:
                self._output.put(line)
        except (ValueError, OSError):
            pass

    def send(self, command: str, wait_ms: int = 400, settle_rounds: int = 3) -> str:
        """写入一条命令并等待输出静默，返回本次产生的新输出。

        :param wait_ms: 每轮轮询等待时长。
        :param settle_rounds: 连续 N 轮无新输出视为静默（命令结束）。
        """
        if self._closed or self._proc.poll() is not None:
            raise RuntimeError("终端会话已关闭")
        self._proc.stdin.write(command + "\n")
        self._proc.stdin.flush()
        lines: list[str] = []
        quiet_rounds = 0
        deadline = time.monotonic() + max(wait_ms * settle_rounds * 2, 5000) / 1000.0
        while time.monotonic() < deadline:
            got = False
            while True:
                try:
                    line = self._output.get_nowait()
                except queue.Empty:
                    break
                got = True
                lines.append(line)
                with self._lock:
                    self.buffer.append(line)
            if got:
                quiet_rounds = 0
            else:
                quiet_rounds += 1
                if quiet_rounds >= settle_rounds:
                    break
            time.sleep(wait_ms / 1000.0)
        return "".join(lines)

    def read_available(self) -> str:
        """读取当前全部可用输出（不等待静默）。"""
        lines: list[str] = []
        while True:
            try:
                line = self._output.get_nowait()
            except queue.Empty:
                break
            lines.append(line)
            with self._lock:
                self.buffer.append(line)
        return "".join(lines)

    def close(self) -> None:
        """终止会话并回收（幂等）。"""
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            self._proc.stdout.close()
            self._proc.stdin.close()
        except (ValueError, OSError):
            pass

    @property
    def closed(self) -> bool:
        return self._closed or self._proc.poll() is not None

    def snapshot(self) -> dict:
        with self._lock:
            return {"id": self.id, "shell": self.shell, "cwd": self.cwd,
                    "closed": self.closed, "lines": len(self.buffer)}


class TerminalService(Service):
    """``terminals`` 服务：持久终端会话注册表（``ctx.terminals``）。"""

    def __init__(self, ctx: AppContext, shell: Optional[str] = None) -> None:
        super().__init__(ctx, "terminals")
        self._shell = shell or _detect_shell()
        self._sessions: dict[str, TerminalSession] = {}

    def spawn(self, cwd: Optional[str] = None) -> TerminalSession:
        """启动一个持久 shell 会话并注册。"""
        session_id = uuid.uuid4().hex
        session = TerminalSession(session_id, cwd=cwd, shell=self._shell)
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> TerminalSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"终端会话不存在: {session_id}")
        return session

    def close(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            session.close()

    def close_all(self) -> None:
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``terminals`` 服务（持久 shell 会话；``shell`` 可覆盖探测）。"""
    config = config or {}
    TerminalService(ctx, shell=config.get("shell"))


apply.provides = ["terminals"]  # 声明：本插件提供 terminals 服务

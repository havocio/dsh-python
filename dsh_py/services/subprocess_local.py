"""subprocess 本地实现（对标 dsh 的 ``@deepseek-ai/dsh-subprocess-local``）。

每次 spawn 是一棵分离进程树，带 spec 的逐流 stdio 处置。正常释放终止并加入
活树。

**与 dsh 差异**：

- 树级终止：POSIX 用 ``start_new_session`` 分离进程组 + ``os.killpg``；
  Windows 用 ``CREATE_NEW_PROCESS_GROUP`` + ``taskkill /T /F``。
- dsh 的 ``spawn`` 是 Node 同步返回；dsh_py 的 asyncio 子进程创建是异步的——
  句柄先同步返回（pid 暂为 -1），后台任务完成创建后接线（spawn 失败经
  ``done`` 拒绝，pid 保持 -1，终止为 no-op）。
- 终端原语：dsh 用 ``node-pty`` 分配真实 PTY；dsh_py 无该依赖，提供**非 PTY
  近似**（Popen + 管道）：前台组检查返回 None、前台发信号退化为对直接子进程
  发信号——与 ``services/terminal.py`` 同一信任前提，差异已注明。
"""

from __future__ import annotations

import asyncio
import os
import signal as signal_mod
import stat
import subprocess as subprocess_mod
import sys
import tempfile
import uuid
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service

from .subprocess import (
    SubprocessOutcome,
    SubprocessRuntime,
    SubprocessSpawnSpec,
    SubprocessTerminalForeground,
    SubprocessTerminalHandle,
    SubprocessTerminalSpawnSpec,
    scrubbed_parent_env,
)

try:  # Windows：创建新进程组（树根）
    CREATE_NEW_PROCESS_GROUP = getattr(subprocess_mod, "CREATE_NEW_PROCESS_GROUP", 0)
except Exception:  # noqa: BLE001
    CREATE_NEW_PROCESS_GROUP = 0


def _is_windows() -> bool:
    return sys.platform == "win32"


def child_env(extra: Optional[dict] = None) -> dict:
    """构建子环境：显式调用方条目覆盖 scrub 父基底，用目标平台的环境键语义。
    字符串显式恢复/覆盖条目；显式 ``None`` 墓碑移除普通 ambient 条目。"""
    env = scrubbed_parent_env()
    if not _is_windows():
        merged = dict(env)
        if extra:
            for key, value in extra.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
        return merged
    entries: list = list(env.items())
    for key, value in (extra or {}).items():
        normalized = key.upper()
        entries = [(k, v) for (k, v) in entries if k.upper() != normalized]
        if value is not None:
            entries.append((key, value))
    return dict(entries)


# --------------------------------------------------------------------------- #
# 输出收集（tail-keep + spill）
# --------------------------------------------------------------------------- #


class OutputCollector:
    """有界内存尾部收集一路流。带 spill 上限时，首次溢出创建一个 spill 文件并
    把每个块（含已收集的）追加进去，只要完整流仍在上限内；不带则只保留内存
    尾部（诊断尾部形状）。tail-keep 依据：错误与最终结果聚集在命令输出末尾。
    """

    def __init__(self, max_bytes: int, max_spill_bytes: Optional[int], label: str, spill_dir: str) -> None:
        self.max_bytes = max_bytes
        self.max_spill_bytes = max_spill_bytes
        self.label = label
        self.spill_dir = spill_dir
        self.chunks: list[bytes] = []
        self.bytes = 0
        self.dropped = False
        self.spill_file: Optional[str] = None
        self._spill_fd: Optional[int] = None
        self.spill_disabled = max_spill_bytes is None
        self.total = 0

    def push(self, chunk: bytes) -> None:
        """吞入一个流块，计入整流总量。首次溢出内存上限时打开 spill 文件并
        追加每个块（含已收集的）；内存尾部随后从头部丢弃直到适配上限。"""
        self.total += len(chunk)
        overflows = self.bytes + len(chunk) > self.max_bytes
        if not self.spill_disabled and (overflows or self._spill_fd is not None):
            self._spill_all(chunk)
        self.chunks.append(chunk)
        self.bytes += len(chunk)
        while self.bytes > self.max_bytes:
            head = self.chunks[0]
            excess = self.bytes - self.max_bytes
            if len(head) <= excess:
                self.chunks.pop(0)
                self.bytes -= len(head)
            else:
                self.chunks[0] = head[excess:]
                self.bytes -= excess
            self.dropped = True

    def _spill_all(self, chunk: bytes) -> None:
        if self.max_spill_bytes is not None and self.total > self.max_spill_bytes:
            self._discard_spill()
            return
        if self._spill_fd is None:
            self.spill_file = os.path.join(
                self.spill_dir,
                f"dsh-subprocess-{os.getpid()}-{uuid.uuid4().hex[:12]}-{self.label}.log",
            )
            self._spill_fd = os.open(self.spill_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            for prior in self.chunks:
                os.write(self._spill_fd, prior)
        os.write(self._spill_fd, chunk)

    def _discard_spill(self) -> None:
        fd, file = self._spill_fd, self.spill_file
        self._spill_fd = None
        self.spill_file = None
        self.spill_disabled = True
        if fd is not None:
            try:
                os.close(fd)
            except OSError:  # noqa: BLE001 - 保留描述符以便 finalize 重试关闭
                self._spill_fd = fd
        if file is not None:
            try:
                os.unlink(file)
            except OSError:  # noqa: BLE001 - 最多留下 maxSpillBytes
                pass

    def read_from(self, from_byte: int) -> dict:
        """整流字节坐标的增量读：返回自 ``from_byte`` 以来推送的一切。偏移已滑
        出内存尾部窗口时读是 ``lossy``——返回整个保留尾部。"""
        window_start = self.total - self.bytes
        buffer = b"".join(self.chunks)
        lossy = from_byte < window_start
        slice_bytes = buffer if lossy else buffer[from_byte - window_start :]
        return {
            "text": slice_bytes.decode("utf-8", errors="replace"),
            "nextOffset": self.total,
            "lossy": lossy,
            **({"spillPath": self.spill_file} if self.spill_file is not None else {}),
        }

    def seal(self) -> None:
        """流结束后关闭 spill 文件（幂等；关闭失败即停止宣传该路径）。"""
        if self._spill_fd is None:
            return
        try:
            os.close(self._spill_fd)
        except OSError:  # noqa: BLE001 - 延迟写回故障使 spill 不可靠
            self.spill_file = None
        self._spill_fd = None

    def finalize(self) -> dict:
        self.seal()
        return {
            "text": b"".join(self.chunks).decode("utf-8", errors="replace"),
            "truncated": self.dropped,
            **({"spillPath": self.spill_file} if self.spill_file is not None else {}),
        }


def _private_spill_dir() -> str:
    """OS tmpdir 下的私有（0700）每进程 spill 目录，惰性创建。"""
    directory = tempfile.mkdtemp(prefix="dsh-subprocess-")
    try:
        os.chmod(directory, 0o700)
    except OSError:  # noqa: BLE001 - Windows 无 chmod 语义
        pass
    return directory


# --------------------------------------------------------------------------- #
# 进程树工具
# --------------------------------------------------------------------------- #


def _signal_name(code: int) -> Optional[str]:
    """把负退出码（信号）映射为信号名（如 -15 → 'SIGTERM'）。"""
    try:
        return signal_mod.Signals(-code).name
    except (ValueError, AttributeError):  # noqa: BLE001
        return None


def _taskkill_tree(pid: int) -> None:
    """Windows 进程树强制终止（``taskkill /T /F``），包含式。"""
    if pid <= 0:
        return
    try:
        subprocess_mod.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess_mod.DEVNULL,
            stderr=subprocess_mod.DEVNULL,
        )
    except OSError:  # noqa: BLE001 - 缺失二进制等
        pass


def _kill_group(pid: int, sig: int) -> None:
    """向分离的 POSIX 进程组投递信号；永不抛。"""
    if pid <= 0:
        return
    try:
        os.killpg(pid, sig)
    except OSError:  # noqa: BLE001 - ESRCH 等
        pass


def _group_alive(pid: int) -> bool:
    """POSIX 进程组存活探测。"""
    if pid <= 0:
        return False
    try:
        os.killpg(pid, 0)
        return True
    except OSError:
        return False


def _tree_alive(pid: int, proc: Any, platform_win: bool, settled: bool = False) -> bool:
    """树根（或 POSIX 组）是否仍活着。POSIX 组若只含未被回收的僵尸，``killpg(0)``
    仍会成功——它们无法执行工作也无法被信号驯服；直接子已终止后仅当进程组仍有
    活成员才算活着（Linux 经 /proc 探测；其他 POSIX 平台退化为组信号探测）。"""
    if platform_win:
        return proc is not None and getattr(proc, "returncode", None) is None
    if not _group_alive(pid):
        return False
    if settled and linux_group_has_live_members(pid) is False:
        return False
    return True


def linux_group_has_live_members(process_group_id: int) -> Optional[bool]:
    """Linux 进程组是否仍有**执行中**成员。``False`` 表示组内只剩僵尸/死亡条目；
    ``None`` 表示进程表无法证明任一结论（或非 Linux 平台）。"""
    if sys.platform != "linux":
        return None
    try:
        entries = os.listdir("/proc")
    except OSError:  # noqa: BLE001 - 不可读 /proc
        return None
    matched = False
    for entry in entries:
        if not entry.isdigit():
            continue
        stat = _read_linux_stat(int(entry))
        if stat is None or stat["pgrp"] != process_group_id:
            continue
        matched = True
        if stat["state"] not in ("Z", "X", "x"):
            return True
    return False if matched else None


def _read_linux_stat(pid: int) -> Optional[dict]:
    """读 Linux ``/proc/<pid>/stat`` 的组/会话/启动字段（含括号 comm 文本）。"""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as f:
            return parse_proc_stat(f.read())
    except OSError:  # noqa: BLE001 - 不可读条目
        return None


def parse_proc_stat(text: str) -> Optional[dict]:
    """解析 Linux ``/proc/<pid>/stat`` 行（含括号 comm 文本）为字段字典。

    字段：``pid`` / ``parentPid`` / ``pgrp`` / ``session`` / ``state`` /
    ``tpgid`` / ``started``（启动时钟节拍）。畸形输入返回 None。纯函数，
    可在任意平台单测。
    """
    open_idx = text.find("(")
    close_idx = text.rfind(")")
    if open_idx <= 0 or close_idx <= open_idx:
        return None
    pid = int(text[:open_idx].strip() or 0)
    rest = text[close_idx + 2 :].strip().split()
    if len(rest) < 20:
        return None
    state = rest[0] or ""
    parent_pid = int(rest[1] or 0)
    pgrp = int(rest[2] or 0)
    session = int(rest[3] or 0)
    tpgid = int(rest[5] or 0)
    started = rest[19]
    if state and len(state) != 1:
        return None
    if started is None or started == "":
        return None
    return {
        "pid": pid,
        "parentPid": parent_pid,
        "pgrp": pgrp,
        "session": session,
        "state": state,
        "tpgid": tpgid,
        "started": started,
    }


# --------------------------------------------------------------------------- #
# 活句柄
# --------------------------------------------------------------------------- #


class _LocalSubprocessHandle:
    """本地进程树句柄（含宿主退出同步强杀，dsh_py 尽力而为）。"""

    def __init__(
        self,
        pid_box: dict,
        proc_box: dict,
        stdin: Any,
        stdout: Any,
        stderr: Any,
        collected: Any,
        done: Any,
        terminate: Any,
        wait_for_exit: Any,
        terminate_for_host_exit: Any,
    ) -> None:
        self._pid_box = pid_box
        self._proc_box = proc_box
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.collected = collected
        self.done = done
        self.terminate = terminate
        self.wait_for_exit = wait_for_exit
        self.terminate_for_host_exit = terminate_for_host_exit

    @property
    def pid(self) -> int:
        return self._pid_box["pid"]


async def spawn_subprocess(spec: SubprocessSpawnSpec, spill_dir: Optional[str] = None) -> _LocalSubprocessHandle:
    """spawn 一棵分离进程树（实现层）。运行时退出以 :class:`SubprocessOutcome`
    解析 ``done``；仅 spawn 失败拒绝（经后台创建任务转发）。"""
    if not isinstance(spec.graceMs, int) or spec.graceMs <= 0:
        raise ValueError("subprocess graceMs must be a positive integer")
    if spec.signal is not None and getattr(spec.signal, "aborted", False):
        raise RuntimeError(f"aborted before spawn: {getattr(spec.signal, 'reason', 'aborted')}")
    argv = list(spec.argv)
    if not argv or not argv[0]:
        raise ValueError("invalid argv: expected a non-empty program name at argv[0]")
    program, args = argv[0], argv[1:]
    spill_dir = spill_dir or _private_spill_dir()
    platform_win = _is_windows()
    loop = asyncio.get_running_loop()

    out_mode, err_mode, stdin_mode = spec.stdio.stdout, spec.stdio.stderr, spec.stdio.stdin
    is_collect = lambda mode: not (isinstance(mode, str) and mode in ("pipe", "inherit"))  # noqa: E731

    kwargs: dict = {
        "cwd": spec.cwd,
        "env": child_env(spec.env),
        "stdin": subprocess_mod.DEVNULL if stdin_mode == "ignore" else subprocess_mod.PIPE,
        "stdout": subprocess_mod.DEVNULL if out_mode == "inherit" else subprocess_mod.PIPE,
        "stderr": subprocess_mod.DEVNULL if err_mode == "inherit" else subprocess_mod.PIPE,
    }
    if platform_win:
        kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True  # 分离进程组 = 树根

    proc = await asyncio.create_subprocess_exec(program, *args, **kwargs)
    pid = proc.pid or -1
    pid_box = {"pid": pid}
    proc_box = {"proc": proc}
    grace_seconds = spec.graceMs / 1000.0

    # -- 收集 ---------------------------------------------------------------- #
    stdout_collector = (
        OutputCollector(out_mode.maxBytes, (out_mode.spill or {}).get("maxBytes"), "stdout", spill_dir)
        if is_collect(out_mode) and proc.stdout is not None
        else None
    )
    stderr_collector = (
        OutputCollector(err_mode.maxBytes, (err_mode.spill or {}).get("maxBytes"), "stderr", spill_dir)
        if is_collect(err_mode) and proc.stderr is not None
        else None
    )

    async def pump(stream: Any, collector: Optional[OutputCollector]) -> None:
        if collector is None:
            return
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            collector.push(chunk)

    pump_tasks = []
    if stdout_collector is not None:
        pump_tasks.append(asyncio.create_task(pump(proc.stdout, stdout_collector)))
    if stderr_collector is not None:
        pump_tasks.append(asyncio.create_task(pump(proc.stderr, stderr_collector)))

    # -- 终止状态机 ---------------------------------------------------------- #
    done: asyncio.Future = loop.create_future()
    settled = False
    grace_timer: Any = None
    tree_exit_observed = False
    tree_observation: Any = None
    abort_remove: Any = None

    def settle(exit_code: Optional[int], sig: Optional[str]) -> None:
        nonlocal settled
        if settled or done.done():
            return
        settled = True
        stdout_collector.seal() if stdout_collector else None
        stderr_collector.seal() if stderr_collector else None
        if not done.done():
            done.set_result(SubprocessOutcome(exitCode=exit_code, signal=sig))

    async def observe_tree_exit() -> None:
        nonlocal tree_exit_observed, grace_timer
        while True:
            direct_settled = getattr(proc, "returncode", None) is not None
            if not _tree_alive(pid, proc, platform_win, settled=direct_settled):
                break
            await asyncio.sleep(0.015)
        tree_exit_observed = True
        if grace_timer is not None:
            grace_timer.cancel()
            grace_timer = None

    def tree_observation_start() -> Any:
        nonlocal tree_observation
        if tree_observation is None:
            tree_observation = asyncio.create_task(observe_tree_exit())
        return tree_observation

    def kill(sig: int) -> None:
        if tree_exit_observed or not _tree_alive(pid, proc, platform_win):
            return
        if platform_win:
            _taskkill_tree(pid)
        else:
            _kill_group(pid, sig)

    def terminate() -> None:
        nonlocal grace_timer
        if tree_exit_observed or grace_timer is not None or done.done():
            return
        tree_observation_start()
        if tree_exit_observed:
            return
        if platform_win:
            _taskkill_tree(pid)
            return
        _kill_group(pid, signal_mod.SIGTERM)
        # SIGKILL 升级必须活过直接子终止（leader 死 ≠ 树死）；保留 ref 以承诺
        # 强杀一个 trap 的幸存者。
        grace_timer = loop.call_later(grace_seconds, lambda: kill(signal_mod.SIGKILL))

    def terminate_for_host_exit() -> None:
        kill(signal_mod.SIGKILL)

    def on_abort() -> None:
        terminate()

    if spec.signal is not None:
        abort_remove = spec.signal.add_listener(on_abort)

    # 批处理 stdin：写入并关闭（错误尽力而为）
    if isinstance(stdin_mode, dict) and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_mode.get("data", "").encode("utf-8", errors="replace"))
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, OSError):  # noqa: BLE001 - stdin 写入尽力而为
            pass

    async def wait_for_exit(abort_signal: Any = None) -> bool:
        observed = tree_observation_start()
        if tree_exit_observed:
            return True
        if abort_signal is not None and getattr(abort_signal, "aborted", False):
            return False
        if abort_signal is None:
            await observed
            return True
        aborted = loop.create_future()
        remove = abort_signal.add_listener(lambda: aborted.set_result(False))
        try:
            done_task = asyncio.ensure_future(observed)
            aborted_task = asyncio.ensure_future(aborted)
            result = await asyncio.wait(
                {done_task, aborted_task}, return_when=asyncio.FIRST_COMPLETED
            )
            return True if done_task in result[0] else False
        finally:
            remove()

    # -- 结果等待 ------------------------------------------------------------ #
    async def _wait_result() -> None:
        try:
            code = await proc.wait()
        except Exception:  # noqa: BLE001 - 进程对象失败按 spawn 级处理
            if not done.done():
                done.set_exception(RuntimeError("subprocess wait failed"))
            return
        # 有界排空：幸存后代持有的继承管道不得让结果无限挂起。
        if pump_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pump_tasks, return_exceptions=True), timeout=grace_seconds
                )
            except asyncio.TimeoutError:
                for task in pump_tasks:
                    task.cancel()
        settle(
            code if code >= 0 else None,
            _signal_name(-code) if code < 0 else None,
        )
        tree_observation_start()

    wait_task = asyncio.create_task(_wait_result())

    handle = _LocalSubprocessHandle(
        pid_box=pid_box,
        proc_box=proc_box,
        stdin=proc.stdin if isinstance(stdin_mode, str) and stdin_mode == "pipe" else None,
        stdout=proc.stdout if isinstance(out_mode, str) and out_mode == "pipe" else None,
        stderr=proc.stderr if isinstance(err_mode, str) and err_mode == "pipe" else None,
        collected=type(
            "Collected",
            (),
            {
                "stdout": stdout_collector if stdout_collector is not None else None,
                "stderr": stderr_collector if stderr_collector is not None else None,
            },
        )(),
        done=done,
        terminate=terminate,
        wait_for_exit=wait_for_exit,
        terminate_for_host_exit=terminate_for_host_exit,
    )
    # 释放时移除 abort 监听（done 终止后）
    done.add_done_callback(lambda _f: abort_remove() if abort_remove is not None else None)
    return handle


# --------------------------------------------------------------------------- #
# 终端（非 PTY 近似）
# --------------------------------------------------------------------------- #


class _LocalTerminalHandle(SubprocessTerminalHandle):
    """非 PTY 终端近似：Popen + 管道，无真实终端分配（见模块 docstring）。"""

    def __init__(self, spec: SubprocessTerminalSpawnSpec, grace_ms: int) -> None:
        self._spec = spec
        self._grace_ms = grace_ms
        env = child_env(spec.env)
        self._proc = subprocess_mod.Popen(
            list(spec.argv),
            cwd=spec.cwd,
            env=env,
            stdin=subprocess_mod.PIPE,
            stdout=subprocess_mod.PIPE,
            stderr=subprocess_mod.STDOUT,
        )
        self.pid = self._proc.pid or -1
        self._done_future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._terminated = False
        # 输出可读流（线程读 → asyncio 队列，简化：直接暴露字节队列由调用方消费）
        self._output = asyncio.Queue()
        self._output_reader = asyncio.create_task(self._read_output())

    async def _read_output(self) -> None:
        try:
            while True:
                chunk = await asyncio.to_thread(self._proc.stdout.read, 4096)
                if not chunk:
                    break
                await self._output.put(chunk)
        except (OSError, ValueError):  # noqa: BLE001
            pass
        finally:
            await self._output.put(None)  # EOF 哨兵

    @property
    def output(self) -> Any:
        return self._output

    async def write(self, data: str) -> None:
        if self._proc.stdin is None or self._terminated:
            raise RuntimeError("terminal is closed")
        try:
            self._proc.stdin.write(data.encode("utf-8", errors="replace"))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):  # noqa: BLE001
            raise RuntimeError("terminal write failed") from None

    async def inspect_foreground(self) -> Optional[SubprocessTerminalForeground]:
        # 非 PTY：无法解析前台组
        return None

    async def signal_foreground(self, sig: str) -> int:
        # 退化：向直接子进程发信号
        if self._proc.poll() is None:
            try:
                os.kill(self._proc.pid, getattr(signal_mod, sig, signal_mod.SIGTERM))
            except OSError:  # noqa: BLE001
                pass
        return self._proc.pid

    async def terminate(self) -> None:
        if self._terminated:
            return
        self._terminated = True
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=self._grace_ms / 1000.0)
            except subprocess_mod.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        except OSError:  # noqa: BLE001 - 进程已消失
            pass
        finally:
            await self._finish_done()

    async def _finish_done(self) -> None:
        if not self._done_future.done():
            code = self._proc.returncode
            self._done_future.set_result(
                SubprocessOutcome(exitCode=code if code is not None and code >= 0 else None,
                                  signal=_signal_name(-code) if code is not None and code < 0 else None)
            )

    @property
    def done(self) -> Any:
        return self._done_future


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #


class LocalSubprocessRuntime(SubprocessRuntime):
    """本地 subprocess 服务：分离进程树、Node 形状的 stdio 处置、凭据 scrub
    环境、树级发信号与 SIGTERM→grace→SIGKILL 升级。
    """

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self.live: set = set()
        self._spill_dir = _private_spill_dir()

    async def resolve_executable(
        self, command: str, env: Optional[dict] = None, signal: Any = None
    ) -> str:
        if not command:
            raise RuntimeError("subprocess-local: executable must be non-empty")
        if signal is not None and getattr(signal, "aborted", False):
            raise RuntimeError("aborted")
        environment = child_env(env)
        if os.path.isabs(command):
            candidates = [command]
        elif "/" in command or (_is_windows() and "\\" in command):
            raise RuntimeError(
                f"subprocess-local: command {command!r} is a relative path; use an absolute path or a bare PATH name"
            )
        else:
            path = environment.get("PATH", "") or ""
            candidates = [os.path.join(directory, command) for directory in path.split(os.pathsep)]
        for candidate in candidates:
            if signal is not None and getattr(signal, "aborted", False):
                raise RuntimeError("aborted")
            try:
                info = os.stat(candidate)
                if not stat.S_ISREG(info.st_mode):
                    continue
                if not os.access(candidate, os.X_OK):
                    continue
                return candidate
            except OSError:  # noqa: BLE001 - 尝试下一个 PATH 候选
                continue
        raise RuntimeError(
            f"subprocess-local: command {command!r} was not found on PATH"
        )

    def spawn(self, spec: SubprocessSpawnSpec) -> _LocalSubprocessHandle:
        loop = asyncio.get_running_loop()
        done: asyncio.Future = loop.create_future()
        pid_box = {"pid": -1}
        proc_box: dict = {"proc": None}
        handle = _LocalSubprocessHandle(
            pid_box=pid_box,
            proc_box=proc_box,
            stdin=None,
            stdout=None,
            stderr=None,
            collected=type("C", (), {"stdout": None, "stderr": None})(),
            done=done,
            terminate=lambda: None,
            wait_for_exit=lambda signal=None: asyncio.sleep(0),
            terminate_for_host_exit=lambda: None,
        )
        self.live.add(handle)

        async def _create() -> None:
            try:
                real = await spawn_subprocess(spec, self._spill_dir)
            except Exception as error:  # noqa: BLE001 - spawn 级失败拒绝 done
                if not done.done():
                    done.set_exception(error)
                self.live.discard(handle)
                return
            pid_box["pid"] = real.pid
            proc_box["proc"] = real._proc_box.get("proc")
            handle.stdin = real.stdin
            handle.stdout = real.stdout
            handle.stderr = real.stderr
            handle.collected = real.collected
            handle.terminate = real.terminate
            handle.wait_for_exit = real.wait_for_exit
            handle.terminate_for_host_exit = real.terminate_for_host_exit
            # done 接力：真实句柄的结果转发到本句柄
            async def _relay() -> None:
                try:
                    outcome = await real.done
                    if not done.done():
                        done.set_result(outcome)
                except Exception as error:  # noqa: BLE001
                    if not done.done():
                        done.set_exception(error)
                finally:
                    self.live.discard(handle)

            asyncio.create_task(_relay())

        asyncio.create_task(_create())
        return handle

    async def spawn_terminal(self, spec: SubprocessTerminalSpawnSpec) -> SubprocessTerminalHandle:
        file = spec.argv[0] if spec.argv else ""
        if not file:
            raise RuntimeError("subprocess-local: terminal argv must contain a program")
        if spec.signal is not None and getattr(spec.signal, "aborted", False):
            raise RuntimeError("aborted")
        terminal = _LocalTerminalHandle(spec, spec.graceMs)
        self.live.add(terminal)
        done = terminal.done
        done.add_done_callback(lambda _f: self.live.discard(terminal))
        return terminal

    async def dispose(self) -> None:
        """终止并等待每棵活树退出（含终端近似）。"""
        pending = []
        for handle in list(self.live):
            try:
                if hasattr(handle, "terminate") and handle is not None:
                    if hasattr(handle, "done"):
                        try:
                            handle.terminate()
                        except Exception:  # noqa: BLE001
                            pass
                        pending.append(handle.done)
            except Exception:  # noqa: BLE001
                pass
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.live.clear()


# --------------------------------------------------------------------------- #
# 插件入口
# --------------------------------------------------------------------------- #


def apply(ctx: AppContext, config: Any = None) -> None:
    """注册 ``ctx.subprocess``（本地实现）。无配置：每个处置与上限随 spec 到达。"""
    runtime = LocalSubprocessRuntime(ctx)

    def _dispose() -> None:
        asyncio.get_event_loop().create_task(runtime.dispose())

    ctx.effect(_dispose, label="local subprocess teardown")


apply.name = "subprocess-local"
apply.provides = ["subprocess"]


def apply_local_invariant(ctx: AppContext) -> None:
    """注册 subprocess-local 不变式伴生（对标 dsh 的 ``subprocess-local/invariant``）。

    无运行时不变式：本包不暴露独立事件序列或可变数据关系，契约由所属 seam
    强制；此处仅保留包名额。
    """
    if ctx.has_service("invariants"):
        ctx.invariants.register("dsh-subprocess-local", lambda _ctx, _fail: None)


apply_local_invariant.name = "subprocess-local-invariant"
apply_local_invariant.inject = ["invariants"]

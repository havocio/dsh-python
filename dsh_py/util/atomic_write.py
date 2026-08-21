"""原子文件替换与写入协调（util/atomic-write，对标 dsh 的 ``dsh-atomic-write``）。

- :func:`write_file_atomic` —— 随机后缀兄弟文件 + 独占创建（``wx``）+ 权限位，
  然后 rename 覆盖目标：读者只会看到旧或新的完整内容，被替换的文件带确切 mode。
  独占打开拒绝跟随种植在临时路径上的符号链接；同目录兄弟保证 rename 在同一
  文件系统内；失败时移除临时文件并重抛。
- :func:`with_file_lock` —— 经 ``wx`` 创建的 ``<file>.lock`` 兄弟文件串行化
  跨进程写者：读-改-写循环绝不会复活另一个写者刚替换的状态；读者保持无锁
  （rename 提交本身是原子的）。竞争指数退避，超时（2s）报错；竞争失败者绝不
  移除已有锁（文件年龄无法证明其所有者已停止；孤儿恢复是运维动作）。

crash 耐久（fsync）不在本原语范围内（对齐 dsh 的 TODO 标注）。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

# 写者锁协议常量（协议鲁棒性不变量，非部署可调参数）
LOCK_RETRY_INITIAL_MS = 20
LOCK_RETRY_MAX_MS = 200
LOCK_TIMEOUT_MS = 2_000


def write_file_atomic(
    filename: str,
    content: str | bytes,
    mode: int,
    dir_mode: Optional[int] = None,
) -> None:
    """一步原子替换 ``filename`` 的内容，自动创建父目录。

    :param mode: 盖在新临时 inode 上并经 rename 携带的权限位（受进程 umask
        影响，同每个新 inode）。
    :param dir_mode: 本调用创建的父目录权限位；缺省用 mkdir 默认（私有数据树
        传 ``0o700``）。
    """
    parent = os.path.dirname(os.path.abspath(filename))
    os.makedirs(parent, mode=dir_mode or 0o777, exist_ok=True)
    temp = f"{filename}.{uuid.uuid4().hex}.tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(temp, flags, mode)
        try:
            if isinstance(content, str):
                content = content.encode("utf-8")
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                view = view[written:]
        finally:
            os.close(fd)
        os.replace(temp, filename)
    except Exception:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise


def with_file_lock(filename: str, operation: Callable[[], Awaitable[Any]]) -> Any:
    """持有 ``filename`` 的跨进程写者锁执行一次异步操作。

    锁是 ``wx`` 创建的 ``<filename>.lock`` 兄弟；配合 :func:`write_file_atomic`
    的 rename 提交，读者无锁、只有写者竞争。竞争指数退避（非阻塞等待）并在
    截止后以超时错误失败。父目录必须存在。

    :returns: operation 的结果；锁在两种结局下都会释放。
    """
    return _acquire_and_run(filename, operation)


async def _acquire_and_run(filename: str, operation: Callable[[], Awaitable[Any]]) -> Any:
    lock_path = f"{filename}.lock"
    deadline_ms = time.monotonic() * 1000 + LOCK_TIMEOUT_MS
    delay = LOCK_RETRY_INITIAL_MS
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    while True:
        try:
            fd = os.open(lock_path, flags, 0o600)
            try:
                os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
            finally:
                os.close(fd)
            break
        except FileExistsError:
            pass
        if time.monotonic() * 1000 >= deadline_ms:
            raise TimeoutError(f"atomic-write: 等待写者锁超时：{lock_path}")
        await asyncio.sleep(delay / 1000.0)
        delay = min(delay * 2, LOCK_RETRY_MAX_MS)
    try:
        return await operation()
    finally:
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass

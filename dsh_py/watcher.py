"""profile 热重载（对标 dsh 的 ``watchUserPatches``，轻量实现）。

以固定间隔轮询一个或多个 profile 文件的 mtime；任一文件变化时触发
``reload_fn``（通常：dispose 旧句柄 → 用新内容重新 :func:`dsh_py.loader.boot`）。
不引入第三方依赖（无 watchdog），轮询间隔可配置。
"""

from __future__ import annotations

import asyncio
import os
from typing import Callable, Optional


class ProfileWatcher:
    """监控 profile 文件的变更并触发重载。

    :param paths: 要监控的文件路径列表。
    :param reload_fn: 无参回调；任一文件变化时被调用（应负责重建插件树）。
    :param interval: 轮询间隔（秒）。
    """

    def __init__(
        self,
        paths: list[str],
        reload_fn: Callable[[], None],
        interval: float = 1.0,
    ) -> None:
        self.paths = paths
        self.reload_fn = reload_fn
        self.interval = interval
        self._task: Optional[asyncio.Task] = None
        self._mtimes: dict[str, float] = {}

    def _snapshot(self) -> dict[str, int]:
        snap = {}
        for path in self.paths:
            try:
                # 用纳秒 mtime，避免 Windows 上 getmtime 的浮点精度丢失
                snap[path] = os.stat(path).st_mtime_ns
            except OSError:
                snap[path] = -1  # 文件缺失也视为一种状态（出现时触发重载）
        return snap

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            current = self._snapshot()
            changed = any(current.get(p) != self._mtimes.get(p) for p in self.paths)
            if changed:
                self._mtimes = current
                self.reload_fn()

    async def start(self) -> None:
        """启动后台轮询任务（重复调用为 no-op）。

        基准快照在**启动瞬间**同步采集，避免任务首次执行前文件已变化
        导致「把变化后的状态当成基准」而漏检。
        """
        if self._task is not None and not self._task.done():
            return
        self._mtimes = self._snapshot()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """停止轮询任务。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


async def watch_profile(
    paths: list[str],
    reload_fn: Callable[[], None],
    interval: float = 1.0,
) -> ProfileWatcher:
    """便捷函数：创建并启动一个 :class:`ProfileWatcher`。"""
    watcher = ProfileWatcher(paths, reload_fn, interval=interval)
    await watcher.start()
    return watcher

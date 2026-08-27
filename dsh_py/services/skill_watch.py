"""skill-filesystem 的可选目录监视器（``watchdog`` 懒加载）。

对齐 dsh 的 SkillWatchManager（chokidar 版）的最小子集：观察已存在的技能根，
任一相关事件（add / change / unlink / …）触发 ``invalidate()``。

适配（dsh_py 差异，已注明）：
- dsh 的祖先模式（根不存在时观察最近存在祖先）、双探针稳定性校验、宿主变更
  观测等健壮性设施未移植——缺失根不监视（目录出现后下次手动失效 / 重扫可见）；
- 事件粒度不区分 addDir/unlinkDir/SKILL.md，任何事件都失效（保守重扫）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional


class SkillWatchManager:
    """有界宿主目录监视器：目录发现照旧在 provider 侧，本类只负责失效通知。"""

    def __init__(self, invalidate: Callable[[], None], config: Optional[dict] = None) -> None:
        config = config or {}
        self._invalidate = invalidate
        self._stability_threshold_ms = config.get("watchStabilityThresholdMs", 200)
        self._poll_interval_ms = config.get("watchPollIntervalMs", 100)
        try:
            from watchdog.observers import Observer  # 懒加载：可选依赖

            self._observer = Observer()
            self._observer.daemon = True
        except ImportError:
            raise ImportError("watchdog 未安装：skill-filesystem 的目录监视不可用") from None
        self._watches: dict[str, Any] = {}
        self._closing = False

    async def observe_roots(self, root_paths: list[str]) -> None:
        """对齐当前根集合：新根（目录存在）起监视，消失的根停监视。"""
        if self._closing:
            return
        wanted = {os.path.abspath(p) for p in root_paths if Path(p).is_dir()}
        for path in list(self._watches):
            if path not in wanted:
                watch = self._watches.pop(path)
                try:
                    self._observer.unschedule(watch)
                except Exception:  # noqa: BLE001 -- 拆除 best-effort
                    pass
        if not self._observer.is_alive():
            self._observer.start()
        for path in wanted:
            if path in self._watches:
                continue
            try:
                watch = self._observer.schedule(
                    _InvalidationHandler(self._invalidate), path, recursive=False,
                )
                self._watches[path] = watch
            except Exception:  # noqa: BLE001 -- 单个根监视失败不阻断其余
                pass

    def observe_host_mutation(self, path: str) -> None:
        """宿主写 / 编辑后的同步失效入口（dsh_py 无 fs/observed 事件，保守触发）。"""
        if self._closing:
            return
        normalized = os.path.abspath(path)
        if any(
            normalized == root or normalized.startswith(root + os.sep)
            for root in self._watches
        ):
            self._invalidate()

    async def dispose(self) -> None:
        """停掉观察线程并回收。"""
        self._closing = True
        try:
            self._observer.stop()
            self._observer.join(timeout=2)
        except Exception:  # noqa: BLE001 -- 拆除 best-effort
            pass
        self._watches.clear()


class _InvalidationHandler:
    """任一文件系统事件都触发失效（保守重扫）。"""

    def __init__(self, invalidate: Callable[[], None]) -> None:
        self._invalidate = invalidate

    def dispatch(self, event: Any) -> None:  # noqa: ANN001 -- watchdog Event
        try:
            self._invalidate()
        except Exception:  # noqa: BLE001 -- 失效通知绝不阻断 watchdog 线程
            pass


__all__ = ["SkillWatchManager"]

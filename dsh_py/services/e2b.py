"""E2B 沙箱共享所有权（对标 dsh 的 ``@deepseek-ai/dsh-e2b``）。

一套能力适配器（fs-e2b / subprocess-e2b）共享同一个 E2B SDK 句柄，从而共享一个远端
Linux 世界里的文件系统与进程。本模块负责创建并持有该句柄，超时或销毁时删除沙箱。

由于 E2B SDK 需要账号与网络，本模块**仅为 seam + 占位实现**：核心契约（配置校验、
cwd 创建、运行时根目录预留、惰性获取、超时/销毁回收）完整落地；与远端交互的 SDK
调用被延迟导入，缺 SDK 时仅在真实使用时报错，模块本身可正常导入。

外部导出 ``quote_e2b_shell_arg``（参数转义）与 ``e2b_control_envs``（隔离 HOME 的 env 构造），
供 fs-e2b / subprocess-e2b 适配器复用。
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import uuid
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service


def quote_e2b_shell_arg(value: str) -> str:
    """转义一个不透明参数，供 SDK 不可避免的 ``/bin/bash -l -c`` 层使用（无插值）。"""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def e2b_control_envs(overrides: Optional[dict[str, str]] = None) -> dict[str, str]:
    """把 E2B 硬编码的登录 shell 隔离到一个新鲜的随机 HOME 路径之后。

    :param overrides: 内部命令追加的环境项。
    :returns: 一个可变的、供 E2B SDK 扩展的映射。
    """
    base: dict[str, str] = dict(overrides or {})
    base["HOME"] = f"/.dsh-e2b-control-{uuid.uuid4().hex}"
    return base


class E2BConfig:
    """共享 E2B 沙箱持有者的配置。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        cwd: str = "/home/user/workspace",
        timeout_ms: int = 300_000,
    ) -> None:
        self.api_key = api_key
        self.cwd = cwd
        self.timeout_ms = timeout_ms


class E2BRuntime(Service):
    """创建可被惰性消费的 E2B SDK 句柄，并在超时或销毁时删除沙箱。

    创建在首次 ``get_sandbox()`` 时惰性开始；适配器在第一次操作前 ``await get_sandbox()``。
    """

    service_name = "e2b"

    def __init__(self, ctx: AppContext, config: E2BConfig | dict | None = None) -> None:
        super().__init__(ctx, self.service_name)
        cfg = config if isinstance(config, E2BConfig) else E2BConfig(**(config or {}))
        # 未显式给 key 时回落到环境变量（绝不透传进沙箱）。
        api_key = cfg.api_key or _read_env_key()
        self.config = E2BConfig(api_key=api_key, cwd=cfg.cwd, timeout_ms=cfg.timeout_ms)
        self.validate()
        self.cwd: str = self.config.cwd
        # 适配器专属进程 / 终端状态的远端目录
        self.runtime_root: str = posixpath.join(self.cwd, ".dsh-e2b")
        self._disposed = False
        # 惰性创建：避免在同步构造时触碰事件循环。首次 get_sandbox 时建立并打开。
        self._ready: "Optional[asyncio.Future[Any]]" = None
        self.ctx.effect(self._teardown_sync, "e2b sandbox teardown")

    def _ensure_open(self) -> "asyncio.Future[Any]":
        """惰性启动沙箱打开任务，返回可用于 await 的就绪 future。"""
        if self._ready is None:
            self._ready = asyncio.get_event_loop().create_future()
            asyncio.ensure_future(self._open())
        return self._ready

    async def get_sandbox(self) -> Any:
        """返回共享的存活 SDK 句柄。

        :returns: cwd 已就绪后的沙箱。
        :raises RuntimeError: E2B 拒绝创建或服务正在销毁时。
        """
        if self._disposed:
            raise RuntimeError("E2B sandbox service is disposing")
        ready = self._ensure_open()
        sandbox = await ready
        # 等待就绪会让出执行，销毁可能在之后才竞争到；二次检查。
        if self._disposed:
            raise RuntimeError("E2B sandbox service is disposing")
        return sandbox

    def validate(self) -> None:
        if len(self.config.api_key) == 0:
            raise ValueError("dsh-e2b: configure apiKey or set E2B_API_KEY")
        if not posixpath.isabs(self.config.cwd):
            raise ValueError(f"dsh-e2b: cwd must be an absolute Linux path: {self.config.cwd}")
        if not (isinstance(self.config.timeout_ms, (int, float)) and self.config.timeout_ms > 0):
            raise ValueError("dsh-e2b: timeoutMs must be a positive finite number")

    async def _open(self) -> Any:
        """创建沙箱（延迟导入 SDK，缺 SDK 时抛出可读错误）。"""
        try:
            from e2b import Sandbox, FileType, SandboxNotFoundError  # noqa: F401
        except ImportError as exc:  # pragma: no cover - 依赖可选
            self._ready.set_exception(
                RuntimeError("dsh-e2b: e2b SDK not installed; install the e2b package to use this")
            )
            return None
        sandbox = await Sandbox.create(
            apiKey=self.config.api_key,
            timeoutMs=self.config.timeout_ms,
            secure=True,
            lifecycle={"onTimeout": "kill"},
        )
        try:
            await sandbox.files.makeDir(self.cwd)
            await sandbox.files.makeDir(self.runtime_root)
            info = await sandbox.files.getInfo(self.runtime_root)
            if info.type != FileType.DIR or info.symlinkTarget is not None:
                raise RuntimeError(f"dsh-e2b: runtime root must be a real directory: {self.runtime_root}")
            await sandbox.commands.run(
                f"chmod 700 -- {quote_e2b_shell_arg(self.runtime_root)}",
                envs=e2b_control_envs(),
            )
            self._ready.set_result(sandbox)
            return sandbox
        except Exception as error:
            try:
                await sandbox.kill()
            except Exception:  # noqa: BLE001 - 回滚失败暂不重试
                pass
            self._ready.set_exception(error)
            raise

    def _teardown_sync(self) -> None:
        """同步清理入口：尽力 await 沙箱销毁（无运行中的事件循环时回退 asyncio.run）。"""
        async def _kill() -> None:
            if self._ready is None or not self._ready.done():
                return
            try:
                sandbox = self._ready.result()
            except Exception:
                return
            self._disposed = True
            try:
                from e2b import SandboxNotFoundError  # noqa: F401
                await sandbox.kill()
            except SandboxNotFoundError:
                pass
            except Exception as error:  # noqa: BLE001
                self.ctx.logger.warn(f"dsh-e2b: sandbox teardown error: {error!r}")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_kill())
            return
        # 运行中的循环：fire-and-forget（dispose 为同步路径，不阻塞）。
        loop.create_task(_kill())


def _read_env_key() -> str:
    """从环境变量读取 E2B_API_KEY（不进入沙箱）。"""
    return os.environ.get("E2B_API_KEY", "")


def apply(ctx: AppContext, config: E2BConfig | dict | None = None) -> None:
    """插件入口：创建并注册共享的 E2B 沙箱持有者（``e2b`` 服务）。"""
    E2BRuntime(ctx, config)

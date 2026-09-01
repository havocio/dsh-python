"""沙箱化文件系统后端（fs-sandbox，对标 dsh 的 ``@deepseek-ai/dsh-fs-sandbox``）。

``SandboxedFileSystem`` 继承本地 :class:`dsh_py.services.fs.FileSystem`，在每次**变更**
操作（写 / 编辑）前对目标路径施加按调用解析的沙箱围栏；读操作不受限。围栏依赖
注入的 ``ctx.sandboxPolicy``（会话级策略解析器，见 :mod:`dsh_py.services.sandbox_policy`）：

- ``read-only``         —— 拒绝全部变更；
- ``workspace-write``   —— 仅允许落在 ``writableRoots``（工作区根 + 后端临时区）内的路径；
- ``danger-full-access``—— 透传（不做围栏，由调用方显式升级获得）。

围栏失败抛结构化 :class:`FsError`(``FS_SANDBOX_DENIED``)，便于工具层精确翻译。
策略缺失时 **fail-closed** 回退为 ``read-only``（绝不默认放行）。
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.services.fs import FS_SANDBOX_DENIED, FsError, FileSystem
from dsh_py.services.sandbox import writableRoots
from dsh_py.services.sandbox_policy import SandboxPolicyResolver, owner_of


def is_path_under(path: str, root: str, *, case_sensitive: Optional[bool] = None) -> bool:
    """判断 ``path`` 是否位于 ``root`` 之下（对标 dsh 的 ``containment.isPathUnder``）。

    先走**词法快路径**（规范绝对化 + 前缀匹配），再回退到 **inode 身份比较**——
    识别 Windows 8.3 短名 / 符号链接 / 别名指向同一真实对象的情况，避免「换名绕过」。
    Windows 默认大小写不敏感。
    """
    if case_sensitive is None:
        case_sensitive = not (sys.platform == "win32")
    path_abs = os.path.abspath(path)
    root_abs = os.path.abspath(root)
    if not case_sensitive:
        path_abs = os.path.normcase(path_abs)
        root_abs = os.path.normcase(root_abs)
    if path_abs == root_abs:
        return True
    if path_abs.startswith(root_abs + os.sep):
        return True
    # inode 身份回退：同一真实文件可能被不同路径名引用
    try:
        left = os.lstat(path)
        right = os.lstat(root)
    except OSError:
        return False
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


class SandboxedFileSystem(FileSystem):
    """本地文件系统的沙箱化包装：变更前施加 per-call MODE 围栏。

    注册为 ``ctx.fs``（替代本地 ``fs`` 后端），复用父类的原子写 / 行窗口读 /
    意图瀑布 / 观测态广播，只在写与编辑入口插入围栏检查。
    """

    def __init__(
        self,
        ctx: AppContext,
        sandbox_policy: Any = None,
        root: Optional[str] = None,
    ) -> None:
        super().__init__(ctx, root)
        # fail-closed：未注入策略时回退为 read-only 解析器（绝不默认放行）
        self._sandbox_policy = sandbox_policy or SandboxPolicyResolver(
            default_mode="read-only", workspace_root="."
        )

    # ------------------------------------------------------------------ #
    # 围栏
    # ------------------------------------------------------------------ #
    def checked_target(self, absolute: str, session: Any = None) -> None:
        """对绝对路径施加沙箱围栏；拒绝时抛 :class:`FsError`(``FS_SANDBOX_DENIED``)。

        :param session: 归属会话（由 ``actor`` 推导），用于解析该调用的策略覆盖。
        """
        policy = self._sandbox_policy.resolve(session=session)
        if policy.mode == "danger-full-access":
            return
        if policy.mode == "read-only":
            raise FsError(FS_SANDBOX_DENIED, f"沙箱策略 read-only 拒绝文件写入：{absolute}")
        # workspace-write：目标必须落在可写根（工作区根 + 后端临时区）之内
        roots = writableRoots(policy.as_execution_policy())
        if not any(is_path_under(absolute, r) for r in roots):
            raise FsError(
                FS_SANDBOX_DENIED,
                f"沙箱策略 workspace-write 拒绝写入越界路径：{absolute}（不在可写根 {roots} 内）",
            )

    # ------------------------------------------------------------------ #
    # 变更入口：先围栏，再委托父类（父类内部仍走意图瀑布 + 版本守卫 + 观测广播）
    # ------------------------------------------------------------------ #
    def write_text(self, path: str, content: str, actor: Any = None) -> dict:
        absolute = self.resolve(path)
        self.checked_target(absolute, session=owner_of(actor))
        return super().write_text(absolute, content, actor=actor)

    def edit_text(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        actor: Any = None,
    ) -> dict:
        absolute = self.resolve(path)
        self.checked_target(absolute, session=owner_of(actor))
        return super().edit_text(
            absolute, old_string, new_string, replace_all=replace_all, actor=actor
        )


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册沙箱化 ``fs`` 后端（替代本地 ``fs``；注入 ``sandboxPolicy``）。"""
    config = config or {}
    SandboxedFileSystem(ctx, sandbox_policy=ctx.sandboxPolicy, root=config.get("root"))


apply.provides = ["fs"]            # 声明：本插件提供 fs 服务（替代本地后端，供 loader 拓扑排序）
apply.inject = ["sandboxPolicy"]   # 依赖：会话级沙箱策略解析器必须先就绪

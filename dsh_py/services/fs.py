"""文件系统服务（fs seam，对标 dsh 的 ``dsh-fs`` 本地子集）：一个执行世界的文件读写。

后端拥有稳定目标身份、路径规范化、文本读取、原子变更。读取窗口与观察策略
留在消费方与策略插件。本实现为零依赖本地后端（``os``/``pathlib``）：

- :meth:`FileSystem.resolve` —— 路径规范化（相对路径基于 ``cwd`` 解析）；
- :meth:`FileSystem.read_text` —— UTF-8 文本读取，行窗口（``offset``/``limit``）
  与行长/字节上限由调用方（工具层）设定；
- :meth:`FileSystem.write_text` —— 原子写（临时文件 + rename）；
- :meth:`FileSystem.edit_text` —— 字面匹配替换（版本检查 + 唯一性守卫）；
- :meth:`FileSystem.list` / ``exists`` / ``info`` —— 目录项与元信息。

写/编辑/观察分别广播 ``fs/write-intent``、``fs/edit-intent``（waterfall 守卫，
首个返回者拥有决策）与 ``fs/observed``（记录型监听器）。
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Callable, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service

# 读工具默认行窗口上限（对齐 dsh tool-fs 的 READ_LIMIT）
READ_LIMIT = 2000
# 单行最大字符数（超长截断，对齐 READ_MAX_LINE_LENGTH）
READ_MAX_LINE_LENGTH = 2000
# 单次读返回的最大字节数（对齐 READ_MAX_BYTES）
READ_MAX_BYTES = 1024 * 1024

# --------------------------------------------------------------------------- #
# 结构化错误（对标 dsh 的 ``FsError``）：携带错误码，便于工具层精确翻译
# --------------------------------------------------------------------------- #
class FsError(Exception):
    """文件系统结构化错误：携带 ``code``（与 dsh 对齐），工具层据此翻译提示。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.name = "FsError"


#: 沙箱策略拒绝了本应变更的操作（read-only / workspace-write 越界）。
FS_SANDBOX_DENIED = "FS_SANDBOX_DENIED"
#: ``fs/edit-intent`` 决策前从未对该目标做过 ``fs/observed`` 观察。
FS_NOT_OBSERVED = "FS_NOT_OBSERVED"
#: 操作要求目标已存在（或观测为存在），但实际不存在 / 观测为缺失。
FS_NOT_FOUND = "FS_NOT_FOUND"
#: 观测到的版本与磁盘当前版本不一致（目标在两次操作间被改动）。
FS_STALE_VERSION = "FS_STALE_VERSION"
#: 操作要求目标不存在（如 create），但已存在。
FS_PATH_EXISTS = "FS_PATH_EXISTS"


class FileSystem(Service):
    """``fs`` 服务：本地文件系统能力（``ctx.fs``）。

    :param root: 可选执行根（沙箱）；非 None 时所有路径解析被限制在根内。
    """

    def __init__(self, ctx: AppContext, root: Optional[str] = None) -> None:
        super().__init__(ctx, "fs")
        self._root = os.path.abspath(root) if root is not None else None
        # 每个绝对路径的磁盘当前版本号（写/编辑成功后自增）；供 fs-observation-policy
        # 的版本守卫比对（观测版本 vs 磁盘版本），外部直写会令其「过期」从而触发
        # FS_STALE_VERSION（与 dsh 的「仅保护经观测事件追踪的改动」模型一致）。
        self._versions: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # 路径
    # ------------------------------------------------------------------ #
    def resolve(self, path: str, cwd: Optional[str] = None) -> str:
        """规范化一个路径为绝对路径（相对路径基于 ``cwd`` 或进程工作目录）。"""
        if not path:
            raise ValueError("路径不能为空")
        base = cwd or os.getcwd()
        absolute = os.path.abspath(os.path.join(base, path))
        if self._root is not None:
            if not (absolute == self._root or absolute.startswith(self._root + os.sep)):
                raise PermissionError(f"路径 {absolute!r} 越出执行根 {self._root!r}")
        return absolute

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def read_text(
        self,
        path: str,
        offset: int = 1,
        limit: int = READ_LIMIT,
        max_line_length: int = READ_MAX_LINE_LENGTH,
        max_bytes: int = READ_MAX_BYTES,
        actor: Any = None,
    ) -> dict:
        """按行窗口读取 UTF-8 文本（对齐 dsh tool-fs 的 read）。

        返回 ``{"path", "total_lines", "lines": [(行号, 文本), ...], "truncated"}``；
        行长超限截断，字节上限用尽即停（``truncated=True``）。读后广播
        ``fs/observed``（存在 / 缺失），供 fs-observation-policy 维护观测态。
        """
        absolute = self.resolve(path)
        if os.path.isdir(absolute):
            raise IsADirectoryError(f"{absolute} 是目录，不能按文本读取")
        if not os.path.exists(absolute):
            # 记录「缺失」观测态（owner 由 actor 推导）
            self.ctx.emit("fs/observed", {"path": absolute, "present": False, "actor": actor})
            raise FileNotFoundError(f"文件不存在: {absolute}")
        with open(absolute, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        all_lines = content.split("\n")
        total = len(all_lines)
        selected = all_lines[offset - 1:offset - 1 + limit]
        lines: list[tuple[int, str]] = []
        budget = max_bytes
        truncated = False
        for index, raw in enumerate(selected, start=offset):
            text = raw
            if len(text) > max_line_length:
                text = text[:max_line_length] + "…"
                truncated = True
            encoded = text.encode("utf-8")
            if len(encoded) > budget:
                truncated = True
                break
            budget -= len(encoded)
            lines.append((index, text))
        # 记录「存在」观测态（携带当前磁盘版本，使 gate 的观测版本与磁盘保持一致）
        self.ctx.emit("fs/observed", {
            "path": absolute,
            "present": True,
            "version": self._versions.get(absolute),
            "actor": actor,
        })
        return {"path": absolute, "total_lines": total, "lines": lines, "truncated": truncated}

    # ------------------------------------------------------------------ #
    # 写入 / 编辑
    # ------------------------------------------------------------------ #
    def write_text(self, path: str, content: str, actor: Any = None) -> dict:
        """原子写文本（临时文件 + rename；写前经 ``fs/write-intent`` 瀑布流守卫）。

        瀑布流无监听器（或监听器返回 ``None``）时回退为无条件写入，行为与旧版一致；
        注册了 fs-observation-policy 时，监听器返回 ``{createIfAbsent,
        replaceIfVersion}`` 决策，本方法据之做「缺失拒绝 / 版本守卫」。写成功后
        自增磁盘版本并广播 ``fs/observed``（存在，新版本）。

        返回 ``{"path", "bytes"}``。
        """
        absolute = self.resolve(path)
        decision = self.ctx.waterfall(
            "fs/write-intent",
            {"path": absolute, "actor": actor},
            inner=lambda: None,
        )
        if decision is not None:
            replace_if = decision.get("replaceIfVersion")
            create_if = decision.get("createIfAbsent", True)
            current = self._versions.get(absolute)
            if replace_if is not None:
                if current is None and not os.path.exists(absolute):
                    raise FsError(FS_NOT_FOUND, f"fs/write-intent 要求替换版本 {replace_if}，但目标已不存在：{absolute}")
                if current != replace_if:
                    raise FsError(FS_STALE_VERSION, f"fs/write-intent 版本守卫失败：观测版本 {replace_if} ≠ 磁盘版本 {current}（{absolute} 已被改动）")
            elif not create_if and not os.path.exists(absolute):
                raise FsError(FS_NOT_FOUND, f"fs/write-intent 禁止新建，但目标不存在：{absolute}")

        parent = os.path.dirname(absolute) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".dsh-write-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, absolute)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        # 自增磁盘版本并广播存在观测态
        new_version = (self._versions.get(absolute, 0)) + 1
        self._versions[absolute] = new_version
        self.ctx.emit("fs/observed", {
            "path": absolute,
            "present": True,
            "version": new_version,
            "actor": actor,
        })
        return {"path": absolute, "bytes": len(content.encode("utf-8"))}

    def edit_text(self, path: str, old_string: str, new_string: str,
                  replace_all: bool = False, actor: Any = None) -> dict:
        """字面匹配替换（对齐 dsh tool-fs 的 edit）。

        - 旧文本必须唯一匹配（``replace_all=False``）或允许全部替换；
        - 先经 ``fs/edit-intent`` 瀑布流守卫（未观测 → FS_NOT_OBSERVED，观测为缺失
          → FS_NOT_FOUND，观测为存在 → 返回版本守卫）；再重读、校验、重写。

        返回 ``{"count": 匹配数, "replaced": bool, "bytes"}``。
        """
        absolute = self.resolve(path)
        if not old_string:
            raise ValueError("old_string 不能为空")
        decision = self.ctx.waterfall(
            "fs/edit-intent",
            {"path": absolute, "actor": actor},
            inner=lambda: None,
        )
        if decision is not None and decision.get("version") is not None:
            current = self._versions.get(absolute)
            if current is None and not os.path.exists(absolute):
                raise FsError(FS_NOT_FOUND, f"fs/edit-intent 要求编辑已存在的目标，但目标已不存在：{absolute}")
            if current != decision["version"]:
                raise FsError(FS_STALE_VERSION, f"fs/edit-intent 版本守卫失败：观测版本 {decision['version']} ≠ 磁盘版本 {current}（{absolute} 已被改动）")

        with open(absolute, "r", encoding="utf-8") as f:
            content = f.read()
        count = content.count(old_string)
        if count == 0:
            return {"count": 0, "replaced": False, "bytes": len(content.encode("utf-8"))}
        if count > 1 and not replace_all:
            raise ValueError(f"old_string 匹配 {count} 处（非唯一）；请提供更多上下文或设置 replace_all=True")
        updated = content.replace(old_string, new_string)
        self.write_text(absolute, updated, actor=actor)
        return {"count": count, "replaced": True, "bytes": len(updated.encode("utf-8"))}

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def list(self, path: str = ".") -> list[dict]:
        """列出目录项（内容无关的稳定列表）。"""
        absolute = self.resolve(path)
        if not os.path.isdir(absolute):
            raise NotADirectoryError(f"{absolute} 不是目录")
        entries: list[dict] = []
        for name in sorted(os.listdir(absolute)):
            child = os.path.join(absolute, name)
            entries.append({
                "name": name,
                "type": "directory" if os.path.isdir(child) else "file",
                "size": os.path.getsize(child) if os.path.isfile(child) else 0,
            })
        return entries

    def exists(self, path: str) -> bool:
        return os.path.exists(self.resolve(path))

    def info(self, path: str) -> dict:
        absolute = self.resolve(path)
        if not os.path.exists(absolute):
            raise FileNotFoundError(f"路径不存在: {absolute}")
        return {
            "path": absolute,
            "type": "directory" if os.path.isdir(absolute) else "file",
            "size": os.path.getsize(absolute) if os.path.isfile(absolute) else 0,
        }


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``fs`` 服务（本地文件系统；``root`` 配置可限制执行根）。"""
    config = config or {}
    FileSystem(ctx, root=config.get("root"))


apply.provides = ["fs"]  # 声明：本插件提供 fs 服务

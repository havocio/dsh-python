"""本地创作预设的复制 / 读取 / 删除（dsh ``authoring.ts``）。

创作限定在 ``user`` 根：shipped ``.system`` 集合属于部署，让浏览器改写它等于
把「重置到已知预设」变成同一调用方可先破坏的东西。唯一创作写是整目录复制
既有预设——无人提供组合文本，输入是 host 在自己根上解析的 id + 可选显示名，
创作不授予复制源未携带的任何能力。

适配：node ``cp/rm/chmod`` → ``shutil.copytree``/``shutil.rmtree``/``os.chmod``；
``writeFileAtomic`` → 项目 ``util/atomic_write``。
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

from dsh_py.services.agent_presets.metadata import METADATA_FILE, render_preset_metadata
from dsh_py.services.agent_presets.preset import PRESET_ID


class InvalidPresetIdError(Exception):
    """不能作为根下目录名的预设 id。"""

    def __init__(self, preset_id: str) -> None:
        self.preset_id = preset_id
        super().__init__(
            f"agent-presets: preset id {preset_id!r} must match {PRESET_ID.pattern} — "
            "the id is a directory name, so anything else could escape the preset root"
        )


class PresetExistsError(Exception):
    """复制目标已被占据——复制绝不覆盖。"""

    def __init__(self, preset_id: str) -> None:
        self.preset_id = preset_id
        super().__init__(
            f'agent-presets: preset "{preset_id}" already exists — '
            "a copy never overwrites; delete the existing preset first or choose another id"
        )


class PresetNotWritableError(Exception):
    """在部署不允许处尝试创作。"""

    def __init__(self, preset_id: str, reason: str) -> None:
        self.preset_id = preset_id
        super().__init__(f'agent-presets: preset "{preset_id}" cannot be written: {reason}')


def writable_root(roots: list) -> str:
    """本地创作写入的根：第一个 ``user`` 根；无则抛。"""
    root = next((r for r in roots if r["trust"] == "user"), None)
    if root is None:
        raise PresetNotWritableError("", "this deployment configures no user-writable preset root")
    raw_path = root["path"]
    if raw_path.startswith("~"):
        raw_path = os.path.expanduser(raw_path)
    return os.path.abspath(raw_path)


def read_composition(preset: dict) -> str:
    """读一个预设的组合文本（原样）。"""
    with open(preset["path"], encoding="utf-8") as fh:
        return fh.read()


def _occupied(path: str) -> bool:
    """路径是否已有可用占用（cp 的 errorOnExist 兜底并发竞态）。"""
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _tighten_modes(directory: str) -> None:
    """把复制的树收紧到仅属主：shipped 预设世界可读，复制承载与 settings 文档
    同等的分量，剥离 group/other 访问；属主执行位保留（预设可能带可运行助手）。"""
    os.chmod(directory, 0o700)
    for entry in os.scandir(directory):
        target = os.path.join(directory, entry.name)
        if entry.is_dir():
            _tighten_modes(target)
        else:
            os.chmod(target, 0o700 if os.stat(target).st_mode & 0o100 else 0o600)


def copy_composition(roots: list, source: dict, id: str, name: Optional[str] = None) -> str:
    """复制一个既有预设的整目录为新预设；失败不留半成品。

    :returns: 新预设目录的绝对路径。
    :raises InvalidPresetIdError: id 不可用；PresetExistsError: 磁盘上已占据；
      PresetNotWritableError: 无 user 根。
    """
    if PRESET_ID.match(id) is None:
        raise InvalidPresetIdError(id)
    target_dir = os.path.join(writable_root(roots), id)
    if _occupied(target_dir):
        raise PresetExistsError(id)
    try:
        shutil.copytree(
            os.path.dirname(source["path"]),
            target_dir,
            symlinks=False,
            dirs_exist_ok=False,
        )
        _tighten_modes(target_dir)
        rendered = render_preset_metadata({
            **({"name": name} if name is not None else {}),
            **({"description": source["description"]} if source.get("description") is not None else {}),
        })
        metadata_path = os.path.join(target_dir, METADATA_FILE)
        if rendered is None:
            try:
                os.remove(metadata_path)
            except OSError:
                pass
        else:
            from dsh_py.util.atomic_write import write_file_atomic

            write_file_atomic(metadata_path, rendered, mode=0o600, dir_mode=0o700)
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise
    return target_dir


def delete_composition(roots: list, preset: dict) -> None:
    """删除本地创作预设；shipped 预设拒绝。"""
    if preset["trust"] != "user":
        raise PresetNotWritableError(preset["id"], "it ships with the deployment")
    directory = os.path.join(writable_root(roots), preset["id"])
    if not os.path.isabs(preset["path"]) or not preset["path"].startswith(directory):
        raise PresetNotWritableError(preset["id"], "it does not live under the writable preset root")
    shutil.rmtree(directory, ignore_errors=True)


__all__ = [
    "InvalidPresetIdError",
    "PresetExistsError",
    "PresetNotWritableError",
    "writable_root",
    "read_composition",
    "copy_composition",
    "delete_composition",
]

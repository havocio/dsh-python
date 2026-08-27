"""Agent preset 词汇（dsh ``@deepseek-ai/dsh-agent-presets`` 的共享类型）。

- ``PRESET_ID``：目录名可用的预设 id 模式——id 会成为路径段，所以这是
  包含性边界而非风格规则（``..``/分隔符/类绝对路径会把组合放到部署授权的
  根之外）。
- :class:`AgentPreset` / :class:`PresetRoot` / ``Config``：以 dict 承载
  （dsh 为 interface；运行时同构）。
- ``UnknownPresetError`` / ``PresetMountError``：未知 id（坏请求）与组合不可用
  （部署需修复）是两个不同的调用方信号，分开报。
"""

from __future__ import annotations

import re

# 目录名可用的预设 id（小写字母开头，连字符继续）
PRESET_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class UnknownPresetError(Exception):
    """没有任何配置根提供所请求的预设。"""

    def __init__(self, preset_id: str, available: list) -> None:
        self.preset_id = preset_id
        self.available = list(available)
        super().__init__(
            f"agent-presets: preset \"{preset_id}\" not found "
            f"(available: {', '.join(str(a) for a in available) or 'none'})"
        )


class PresetMountError(Exception):
    """预设存在但其组合无法安装。"""

    def __init__(self, preset_id: str, reason: str, cause: Exception | None = None) -> None:
        self.preset_id = preset_id
        self.reason = reason
        super().__init__(f"agent-presets: preset \"{preset_id}\" failed to mount: {reason}")
        if cause is not None:
            self.__cause__ = cause


__all__ = ["PRESET_ID", "UnknownPresetError", "PresetMountError"]

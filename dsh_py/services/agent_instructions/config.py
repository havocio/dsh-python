"""配置归一化（agent-instructions/config，对标 dsh 的 ``config.ts``）。

工作区指令的发现与渲染配置：harness home、项目根标记、候选文件名、字节预算。
所有路径探测走可选 ``ctx.fs`` 提供者；无提供者的产品按 no-op 处理
（``compose`` 直接返回 undefined，不加载任何指令）。
"""

from __future__ import annotations

import os
from typing import Optional

from dsh_py.util.home_paths import dsh_home_display, resolve_dsh_home

# 默认配置常量（对齐 dsh）：仅扫描 .git 作为项目根标记；AGENTS.md / CLAUDE.md
# 为基础候选；AGENTS.local.md / CLAUDE.local.md 为本地覆盖候选。
DEFAULT_PROJECT_ROOT_MARKERS = [".git"]
DEFAULT_INSTRUCTION_FILE_CANDIDATES = ["AGENTS.md", "CLAUDE.md"]
DEFAULT_LOCAL_INSTRUCTION_FILE_CANDIDATES = ["AGENTS.local.md", "CLAUDE.local.md"]
DEFAULT_MAX_SOURCE_BYTES = 1_048_576
# 保留的路径段：空、'.'、'..' 与任何含路径分隔符的候选都非法
RESERVED_PATH_SEGMENTS = {"", ".", ".."}


class Config:
    """用户面向的工作区指令加载配置。"""

    def __init__(
        self,
        dsh_home: Optional[str] = None,
        project_root_markers: Optional[list] = None,
        max_bytes: Optional[int] = None,
        max_source_bytes: Optional[int] = None,
        instruction_file_candidates: Optional[list] = None,
        local_instruction_file_candidates: Optional[list] = None,
    ) -> None:
        # maxBytes 必填（对齐 dsh 的 z.number().required()）；其余可选
        if max_bytes is None:
            raise TypeError("agent-instructions: maxBytes 为必填项")
        self.dsh_home = dsh_home
        self.project_root_markers = project_root_markers
        self.max_bytes = max_bytes
        self.max_source_bytes = max_source_bytes
        self.instruction_file_candidates = instruction_file_candidates
        self.local_instruction_file_candidates = local_instruction_file_candidates


class ResolvedDiscoveryConfig:
    """发现用的归一化配置（home / 根标记 / 候选文件名）。"""

    def __init__(
        self,
        dsh_home: str,
        project_root_markers: list,
        instruction_file_candidates: list,
        local_instruction_file_candidates: list,
    ) -> None:
        self.dsh_home = dsh_home
        self.project_root_markers = project_root_markers
        self.instruction_file_candidates = instruction_file_candidates
        self.local_instruction_file_candidates = local_instruction_file_candidates


class ResolvedConfig(ResolvedDiscoveryConfig):
    """发现 + 渲染用的归一化配置（额外含字节预算）。"""

    def __init__(
        self,
        dsh_home: str,
        project_root_markers: list,
        instruction_file_candidates: list,
        local_instruction_file_candidates: list,
        max_bytes: int,
        max_source_bytes: int,
    ) -> None:
        super().__init__(dsh_home, project_root_markers,
                         instruction_file_candidates, local_instruction_file_candidates)
        self.max_bytes = max_bytes
        self.max_source_bytes = max_source_bytes


def workspace_baseline_identity(config: ResolvedConfig, cwd: str, project_root: str) -> str:
    """标识一次发现的「发现 / 优先级 / 预算」语义，供 resume 时校验基线兼容性。

    等价于 dsh 的 ``workspaceBaselineIdentity``：把相对根、根标记、字节预算与
    候选文件名序列化成一个稳定字符串。
    """
    relative = os.path.relpath(project_root, cwd) if cwd else project_root
    return repr({
        "projectRoot": relative,
        "projectRootMarkers": config.project_root_markers,
        "maxBytes": config.max_bytes,
        "maxSourceBytes": config.max_source_bytes,
        "instructionFileCandidates": config.instruction_file_candidates,
        "localInstructionFileCandidates": config.local_instruction_file_candidates,
    })


def resolve_config(config: Config) -> ResolvedConfig:
    """解析默认值、harness home 与合法的同目录候选文件名。"""
    disc = resolve_discovery_config(config)
    return ResolvedConfig(
        dsh_home=disc.dsh_home,
        project_root_markers=disc.project_root_markers,
        instruction_file_candidates=disc.instruction_file_candidates,
        local_instruction_file_candidates=disc.local_instruction_file_candidates,
        max_bytes=config.max_bytes,
        max_source_bytes=config.max_source_bytes
        if config.max_source_bytes is not None else DEFAULT_MAX_SOURCE_BYTES,
    )


def resolve_discovery_config(config: Config) -> ResolvedDiscoveryConfig:
    """解析仅用于发现前的子集配置（home / 根标记 / 候选文件名）。"""
    return ResolvedDiscoveryConfig(
        dsh_home=resolve_dsh_home(config.dsh_home),
        project_root_markers=config.project_root_markers
        if config.project_root_markers is not None else list(DEFAULT_PROJECT_ROOT_MARKERS),
        instruction_file_candidates=resolve_instruction_file_candidates(
            config.instruction_file_candidates, DEFAULT_INSTRUCTION_FILE_CANDIDATES),
        local_instruction_file_candidates=resolve_instruction_file_candidates(
            config.local_instruction_file_candidates, DEFAULT_LOCAL_INSTRUCTION_FILE_CANDIDATES),
    )


def resolve_instruction_file_candidates(candidates: Optional[list], fallback: list) -> list:
    """过滤出合法的候选文件名（去除保留段与含路径分隔符的项）。"""
    raw = candidates if candidates is not None else list(fallback)
    return [
        c for c in raw
        if c not in RESERVED_PATH_SEGMENTS and not any(sep in c for sep in ("\\", "/"))
    ]


def user_global_display_path(dsh_home: str) -> str:
    """用户全局 AGENTS.md 的面向用户展示路径。"""
    return f"{dsh_home_display(dsh_home)}/AGENTS.md"

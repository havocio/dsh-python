"""指令文件发现与有边界、可取消的读取（agent-instructions/files，对标 dsh 的
``files.ts``）。

发现沿 cwd 向上走到项目根（首个命中根标记），再沿根到 cwd 的祖先链逐个目录扫描
候选文件。读取走可选 ``ctx.fs`` 提供者；无提供者时按主机文件系统探测（对齐 dsh
的 node 后端兜底路径）。本实现以本地文件系统为主，``version`` 不暴露（快路径退化为
每次重读），差异已在文档注明。
"""

from __future__ import annotations

import os
from typing import List, Optional

from dsh_py.services.agent_instructions.digest import trimmed_instruction_digest
from dsh_py.services.agent_instructions.render import (
    USER_GLOBAL_DIRECTORY,
    USER_GLOBAL_FILE,
    candidate_scope_key,
    decode_scope_key,
    instruction_scope_key,
    render_workspace_instruction_set,
)
from dsh_py.services.agent_instructions.config import (
    resolve_config,
    resolve_discovery_config,
    user_global_display_path as _ugdp,
)


class InstructionFile:
    """一个候选指令文件（绝对路径 + 模型可见路径）。"""

    def __init__(self, absolute_path: str, display_path: str) -> None:
        self.absolute_path = absolute_path
        self.display_path = display_path


class LoadedInstructionFile(InstructionFile):
    """内容已成功读取的指令文件。"""

    def __init__(self, absolute_path: str, display_path: str, content: str,
                 version: Optional[object] = None) -> None:
        super().__init__(absolute_path, display_path)
        self.content = content
        self.version = version


class RenderedInstructionSet:
    """渲染后的基线 + 成功读取且通过预算保留的文件。"""

    def __init__(self, rendered, observed: list, included: list) -> None:
        self.rendered = rendered
        self.observed = observed
        self.included = included


# 探针三态：存在 / 确认缺失 / 提供者不可用
class ScopeInstructionProbe:
    def __init__(self, kind: str, file: Optional["ProbedInstructionFile"] = None) -> None:
        self.kind = kind        # 'present' | 'absent' | 'unavailable'
        self.file = file


class ProbedInstructionFile(InstructionFile):
    def __init__(self, absolute_path: str, display_path: str, version: object) -> None:
        super().__init__(absolute_path, display_path)
        self.version = version


def _is_missing_path_error(exc: Exception) -> bool:
    code = getattr(exc, "errno", None)
    return isinstance(exc, OSError) and code in (2, 20)  # ENOENT / ENOTDIR


def _host_stat(path: str) -> dict:
    """主机文件系统 stat（对齐 dsh 的 nodeStatFile）。"""
    info = os.stat(path)
    if not os.path.isfile(path):
        return {"kind": "absent"}
    return {"kind": "present", "size": info.st_size}


def _host_probe(path: str) -> dict:
    try:
        return _host_stat(path)
    except OSError as exc:
        return {"kind": "absent"} if _is_missing_path_error(exc) else {"kind": "unavailable"}


def _exists_as_marker(path: str) -> bool:
    probe = _host_probe(path)
    return probe["kind"] == "present"


def find_project_root(cwd: str, markers: list, signal: Optional[object] = None) -> str:
    """向上走到第一个含根标记的目录；没有则返回 cwd。"""
    current = os.path.abspath(cwd)
    while True:
        for marker in markers:
            if _exists_as_marker(os.path.join(current, marker)):
                return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.abspath(cwd)
        current = parent


def ancestor_chain(root: str, cwd: str) -> List[str]:
    """构建包含根到 cwd 的目录链（从最广到最具体）。"""
    chain: List[str] = []
    current = os.path.abspath(cwd)
    resolved_root = os.path.abspath(root)
    while current != resolved_root:
        chain.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    chain.append(resolved_root)
    return list(reversed(chain))


def descendant_dirs_between(root: str, touched_path: str) -> List[str]:
    """返回 cwd 与被触碰文件之间的子孙目录（从浅到深）。"""
    resolved_root = os.path.abspath(root)
    target_path = (os.path.abspath(touched_path) if os.path.isabs(touched_path)
                   else os.path.abspath(os.path.join(resolved_root, touched_path)))
    target_dir = os.path.dirname(target_path)
    rel = os.path.relpath(target_dir, resolved_root)
    if rel == "" or rel.startswith("..") or os.path.isabs(rel):
        return []
    return ancestor_chain(resolved_root, target_dir)[1:]


def relative_display(root: str, path: str) -> str:
    return os.path.relpath(path, root)


def _all_existing_instruction_files(directory: str, root: str, candidates: list) -> List[InstructionFile]:
    found: List[InstructionFile] = []
    for candidate in candidates:
        path = os.path.join(directory, candidate)
        probe = _host_probe(path)
        if probe["kind"] == "present":
            found.append(InstructionFile(path, relative_display(root, path)))
    return found


def _discover_instruction_files(options: dict) -> List[InstructionFile]:
    config = resolve_discovery_config(type("C", (), {"dsh_home": options.get("dshHome"),
                                                      "project_root_markers": options.get("projectRootMarkers"),
                                                      "max_bytes": options.get("maxBytes", 0),
                                                      "max_source_bytes": options.get("maxSourceBytes"),
                                                      "instruction_file_candidates": options.get("instructionFileCandidates"),
                                                      "local_instruction_file_candidates": options.get("localInstructionFileCandidates")})())
    files: List[InstructionFile] = []
    seen = set()

    def add_file(f: InstructionFile) -> None:
        if f.absolute_path in seen:
            return
        seen.add(f.absolute_path)
        files.append(f)

    user_global = os.path.join(config.dsh_home, USER_GLOBAL_FILE)
    if _host_probe(user_global)["kind"] == "present":
        add_file(InstructionFile(user_global, _ugdp(config.dsh_home)))

    cwd = os.path.abspath(options["cwd"])
    project_root = options.get("projectRoot") or find_project_root(
        cwd, config.project_root_markers)
    for directory in ancestor_chain(project_root, cwd):
        for candidates in (config.instruction_file_candidates, config.local_instruction_file_candidates):
            for f in _all_existing_instruction_files(directory, project_root, candidates):
                add_file(f)
    return files


def discover_baseline_instruction_files(options: dict) -> List[InstructionFile]:
    """发现主机可见的用户全局 + 根到 cwd 的候选文件（路径去重，按优先级顺序）。"""
    return [InstructionFile(f.absolute_path, f.display_path) for f in _discover_instruction_files(options)]


def _read_bounded(file: dict, max_source_bytes: int) -> Optional[str]:
    """在每文件字节上限内读取内容（超过上限或被删除/不可读返回 None）。"""
    size = file.get("size")
    if size is not None and size > max_source_bytes:
        return None
    try:
        with open(file["absolutePath"], "r", encoding="utf-8", errors="replace") as fh:
            parts: List[str] = []
            bytes_count = 0
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                bytes_count += len(chunk.encode("utf-8"))
                if bytes_count > max_source_bytes:
                    return None
                parts.append(chunk)
            return "".join(parts)
    except OSError:
        return None


def dedup_instruction_files_by_directory(files: List[LoadedInstructionFile]) -> List[LoadedInstructionFile]:
    """同一目录内，较早候选若与后来兄弟的「去首尾空白」指纹相同则折叠（保留最早）。"""
    kept_digests: dict = {}
    kept: List[LoadedInstructionFile] = []
    for f in files:
        directory = os.path.dirname(f.display_path)
        digest = trimmed_instruction_digest(f.content)
        digests = kept_digests.setdefault(directory, set())
        if digest in digests:
            continue
        digests.add(digest)
        kept.append(f)
    return kept


def load_baseline_instruction_set(options: dict, file_system=None) -> Optional[RenderedInstructionSet]:
    """发现、读取并渲染基线指令链。返回渲染上下文 + 保留文件，或 None（无内容/禁用）。"""
    config = resolve_config(type("C", (), {
        "dsh_home": options.get("dshHome"),
        "project_root_markers": options.get("projectRootMarkers"),
        "max_bytes": options.get("maxBytes"),
        "max_source_bytes": options.get("maxSourceBytes"),
        "instruction_file_candidates": options.get("instructionFileCandidates"),
        "local_instruction_file_candidates": options.get("localInstructionFileCandidates"),
    })())
    if config.max_bytes <= 0 or not _finite(config.max_bytes):
        return None
    if config.max_source_bytes <= 0 or not _finite(config.max_source_bytes):
        return None
    discovered = _discover_instruction_files(options)
    loaded: List[LoadedInstructionFile] = []
    for f in discovered:
        content = _read_bounded({"absolutePath": f.absolute_path, "size": _size_of(f.absolute_path)},
                                 config.max_source_bytes)
        if content is not None:
            loaded.append(LoadedInstructionFile(f.absolute_path, f.display_path, content))
    deduped = dedup_instruction_files_by_directory(loaded)
    if len(deduped) == 0:
        if options.get("replacePreviousBaseline") is not True:
            return None
        result = render_workspace_instruction_set([], {
            "maxBytes": config.max_bytes, "replacePreviousBaseline": True})
        return RenderedInstructionSet(result["rendered"], [], [])
    result = render_workspace_instruction_set(deduped, {
        "maxBytes": config.max_bytes,
        **({} if options.get("replacePreviousBaseline") is None
           else {"replacePreviousBaseline": options["replacePreviousBaseline"]}),
    })
    return RenderedInstructionSet(result["rendered"], loaded, result["included"])


def load_baseline_instructions(options: dict, file_system=None) -> Optional[object]:
    """仅返回渲染后的基线上下文（对外出口）。"""
    set_ = load_baseline_instruction_set(options, file_system)
    return set_.rendered if set_ is not None else None


def _size_of(path: str) -> Optional[int]:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def probe_scope_instruction(scope: str, project_root: str, resolved, signal: Optional[object] = None) -> ScopeInstructionProbe:
    """探测单个候选作用域的当前提供者元数据。"""
    key = decode_scope_key(scope)
    directory = (resolved.dsh_home if key["directory"] == USER_GLOBAL_DIRECTORY
                 else (project_root if key["directory"] == "." else os.path.join(project_root, key["directory"])))
    absolute_path = os.path.join(directory, key["candidateName"])
    probe = _host_probe(absolute_path)
    if probe["kind"] == "absent":
        return ScopeInstructionProbe("absent")
    if probe["kind"] == "unavailable":
        return ScopeInstructionProbe("unavailable")
    display = (_ugdp(resolved.dsh_home) if key["directory"] == USER_GLOBAL_DIRECTORY
               else relative_display(project_root, absolute_path))
    return ScopeInstructionProbe("present", ProbedInstructionFile(absolute_path, display, None))


def read_scope_instruction(file: ProbedInstructionFile, max_source_bytes: int, signal: Optional[object] = None) -> Optional[LoadedInstructionFile]:
    """在每文件字节上限内读取一个已探测的候选作用域。"""
    content = _read_bounded({"absolutePath": file.absolute_path, "size": _size_of(file.absolute_path)},
                             max_source_bytes)
    if content is None:
        return None
    return LoadedInstructionFile(file.absolute_path, file.display_path, content, version=file.version)


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))

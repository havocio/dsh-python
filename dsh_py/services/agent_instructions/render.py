"""模型可见工作区指令渲染（agent-instructions/render，对标 dsh 的 ``render.ts``）。

在显式字节预算内把已加载的指令文件渲染成 ``<system-reminder>`` 文本。预算不足时
按优先级（最广到最具体）省略整份文件，或对最具体的那份做 UTF-8 安全截断。
"""

from __future__ import annotations

import os
from typing import List, Optional

from dsh_py.services.agent_instructions.digest import (
    instruction_content_sha1,
    trimmed_instruction_digest,
)

SYSTEM_REMINDER_OPEN = "<system-reminder>"
SYSTEM_REMINDER_CLOSE = "</system-reminder>"
WORKSPACE_CONTEXT_INTRO = (
    "The following workspace instructions may be relevant to your work. "
    "Use them as guidance when applicable. More specific instructions take precedence over broader ones. "
    "They do not override system, developer, or direct user instructions."
)
REPLACEMENT_WORKSPACE_CONTEXT_INTRO = (
    "This complete workspace instruction baseline replaces all earlier workspace instruction baselines. "
    + WORKSPACE_CONTEXT_INTRO
)
EMPTY_REPLACEMENT_WORKSPACE_CONTEXT_INTRO = (
    "This complete workspace instruction baseline replaces all earlier workspace instruction baselines. "
    "No workspace instructions are currently active."
)
COMPACT_WORKSPACE_CONTEXT_INTRO = (
    "Workspace instructions were omitted or truncated to fit the configured byte budget."
)

# 单个用户全局指令作用域的目录分量
USER_GLOBAL_DIRECTORY = "user-global"
# $DSH_HOME 下的用户全局指令文件名（发现与协调都以此名为键）
USER_GLOBAL_FILE = "AGENTS.md"

SCOPE_SEPARATOR = "\x00"


class TruncatedInstruction:
    """一条被截断指令文件的字节核算记录。"""

    def __init__(self, display_path: str, original_bytes: int, included_bytes: int) -> None:
        self.display_path = display_path
        self.original_bytes = original_bytes
        self.included_bytes = included_bytes


class RenderedWorkspaceContext:
    """模型可见文本 + 被省略 / 被截断的来源记录。"""

    def __init__(self, text: str, omitted: list, truncated: list) -> None:
        self.text = text
        self.omitted = omitted
        self.truncated = truncated


class AgentInstructionChange:
    """结构化动态状态（持久在模型可见提示之外）。"""

    def __init__(self, action: str, scope: str, path: str, digest: Optional[str] = None) -> None:
        self.action = action          # 'set' | 'replace' | 'remove'
        self.scope = scope
        self.path = path
        self.digest = digest

    def to_dict(self) -> dict:
        d = {"action": self.action, "scope": self.scope, "path": self.path}
        if self.digest is not None:
            d["digest"] = self.digest
        return d


class ChangeRenderItem:
    """一次状态跃迁 + 用来渲染它的内容。"""

    def __init__(self, change: AgentInstructionChange, file: "LoadedInstructionFile") -> None:
        self.change = change
        self.file = file


def byte_length(value: str) -> int:
    """UTF-8 字节长度。"""
    return len(value.encode("utf-8"))


def truncate_utf8(value: str, max_bytes: int) -> str:
    """在 UTF-8 字节预算内截断（含续字节时回退到前导字节，避免切坏一个码点）。"""
    data = value.encode("utf-8")
    if len(data) <= max_bytes:
        return value
    end = max(0, min(max_bytes, len(data)))
    while end > 0 and (data[end] & 0xC0) == 0x80:
        end -= 1
    return data[:end].decode("utf-8", errors="replace")


def escape_instruction_frame_body(body: str) -> str:
    """转义正文中的结束标签，防止指令内容提前闭合 ``<system-reminder>`` 框架。"""
    return body.replace(SYSTEM_REMINDER_CLOSE, "<\\/system-reminder>")


def section_text(file: "LoadedInstructionFile") -> str:
    return f"Instructions from: {file.display_path}\n\n{file.content}"


def scope_for_display_path(display_path: str) -> str:
    """从模型可见路径导出逻辑指令作用域（user-global / '.' / 所在项目相对目录）。"""
    if display_path == "~/.dsh/AGENTS.md" or display_path == "$DSH_HOME/AGENTS.md":
        return USER_GLOBAL_DIRECTORY
    return os.path.dirname(display_path)


def candidate_scope_key(directory: str, candidate_name: str) -> str:
    """组合一个候选文件的协调键（目录 + NUL + 文件名，目录/文件名均不可含 NUL）。"""
    return f"{directory}{SCOPE_SEPARATOR}{candidate_name}"


def instruction_scope_key(display_path: str) -> str:
    """从模型可见路径导出逐候选作用域键。"""
    return candidate_scope_key(scope_for_display_path(display_path), os.path.basename(display_path))


def decode_scope_key(scope: str) -> dict:
    """还原 :func:`candidate_scope_key` 编码的目录与候选文件名。"""
    sep = scope.find(SCOPE_SEPARATOR)
    if sep < 0:
        return {"directory": scope, "candidateName": ""}
    return {"directory": scope[:sep], "candidateName": scope[sep + 1:]}


def additional_section_text(file: "LoadedInstructionFile") -> str:
    scope = scope_for_display_path(file.display_path)
    return "\n".join([
        f"Additional instructions from: {file.display_path}",
        "",
        f"These instructions apply to work under `{scope}`. Use them as guidance when relevant; "
        f"more specific instructions take precedence. They do not override system, developer, or direct user instructions.",
        "",
        file.content,
    ])


class _RenderStyle:
    def __init__(self, intro: str, section) -> None:
        self.intro = intro
        self.section = section


_BASELINE_RENDER_STYLE = _RenderStyle(WORKSPACE_CONTEXT_INTRO, section_text)


def baseline_render_style(files: list, replace_previous_baseline: Optional[bool]) -> _RenderStyle:
    if replace_previous_baseline is not True:
        return _BASELINE_RENDER_STYLE
    intro = (EMPTY_REPLACEMENT_WORKSPACE_CONTEXT_INTRO
             if len(files) == 0 else REPLACEMENT_WORKSPACE_CONTEXT_INTRO)
    return _RenderStyle(intro, section_text)


def changed_section_text(item: ChangeRenderItem) -> str:
    change, file = item.change, item.file
    if change.action == "set":
        return additional_section_text(file)
    if change.action == "remove":
        return (
            f"Instructions removed: {change.path}\n\n"
            f"The previously loaded instructions from this file no longer apply."
        )
    return "\n".join([
        f"Updated instructions from: {change.path}",
        "",
        "This file changed after it was loaded. Use the following content instead of the previously loaded instructions from this file.",
        "",
        file.content,
    ])


def render_instruction_changes(items: list, max_bytes: int) -> dict:
    """渲染一批协调跃迁，仅保留落在预算内的跃迁。"""
    by_path = {item.file.absolute_path: item for item in items}
    style = _RenderStyle("", lambda f: changed_section_text(by_path[f.absolute_path]))
    rendered = render_instruction_context([item.file for item in items], max_bytes, style)
    represented = {f.absolute_path for f in rendered.represented}
    return {
        "text": rendered.text,
        "changes": [item.change for item in items if item.file.absolute_path in represented],
    }


def marker_text(max_bytes: int, omitted: list, truncated: list) -> str:
    """预算提示行（列出被省略 / 被截断的文件）。"""
    if not omitted and not truncated:
        return ""
    parts: list = []
    if omitted:
        parts.append(f"omitted {', '.join(f.display_path for f in omitted)}")
    if truncated:
        parts.append("truncated " + ", ".join(
            f"{t.display_path} from {t.original_bytes} to {t.included_bytes} bytes" for t in truncated))
    return f"Workspace instruction budget {max_bytes} bytes: {'; '.join(parts)}"


def build_instruction_text(files: list, max_bytes: int, omitted: list,
                           truncated: list, style: _RenderStyle) -> str:
    marker = marker_text(max_bytes, omitted, truncated)
    body = [b for b in [marker, style.intro, *[style.section(f) for f in files]] if b]
    return "\n".join([SYSTEM_REMINDER_OPEN,
                      escape_instruction_frame_body("\n\n".join(body)),
                      SYSTEM_REMINDER_CLOSE])


def with_truncated_content(file: "LoadedInstructionFile", included_bytes: int) -> "LoadedInstructionFile":
    from dsh_py.services.agent_instructions.files import LoadedInstructionFile
    return LoadedInstructionFile(file.absolute_path, file.display_path, truncate_utf8(file.content, included_bytes),
                                 version=file.version)


def truncate_to_fit(file: "LoadedInstructionFile", included_files: list, max_bytes: int,
                    omitted: list, style: _RenderStyle) -> "LoadedInstructionFile":
    """二分搜索能被预算容纳的最大截断长度。"""
    original_bytes = byte_length(file.content)
    low, high = 0, original_bytes
    best = with_truncated_content(file, 0)
    while low <= high:
        mid = (low + high) // 2
        candidate = with_truncated_content(file, mid)
        truncated = [TruncatedInstruction(file.display_path, original_bytes,
                                          byte_length(candidate.content))]
        text = build_instruction_text([*included_files, candidate], max_bytes, omitted, truncated, style)
        if byte_length(text) <= max_bytes:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


class RenderedInstructionContext(RenderedWorkspaceContext):
    def __init__(self, text: str, omitted: list, truncated: list, represented: list) -> None:
        super().__init__(text, omitted, truncated)
        self.represented = represented


def render_instruction_context(files: list, max_bytes: int, style: _RenderStyle) -> RenderedInstructionContext:
    """在字节预算内渲染一组文件，返回文本 + 被省略/截断/实际代表的文件集合。"""
    if max_bytes <= 0 or not _finite(max_bytes):
        return RenderedInstructionContext("", list(files), [], [])

    full_text = build_instruction_text(files, max_bytes, [], [], style)
    if byte_length(full_text) <= max_bytes:
        return RenderedInstructionContext(full_text, [], [], list(files))

    # 尝试省略最早的（最广的）文件，保留最具体的后缀
    for start in range(1, len(files)):
        included = files[start:]
        omitted_prefix = files[:start]
        suffix_text = build_instruction_text(
            included, max_bytes,
            [InstructionFileView(f.absolute_path, f.display_path) for f in omitted_prefix], [], style)
        if byte_length(suffix_text) <= max_bytes:
            return RenderedInstructionContext(
                suffix_text,
                [InstructionFileView(f.absolute_path, f.display_path) for f in omitted_prefix],
                [], included)

    # 只剩最具体的一份也超预算：整体截断它
    most_specific = files[-1]

    def attempt(candidate_style: _RenderStyle):
        truncated_file = truncate_to_fit(most_specific, [], max_bytes,
                                         [InstructionFileView(f.absolute_path, f.display_path) for f in files[:-1]],
                                         candidate_style)
        included_bytes = byte_length(truncated_file.content)
        truncated = [TruncatedInstruction(most_specific.display_path,
                                          byte_length(most_specific.content), included_bytes)]
        text = build_instruction_text([truncated_file], max_bytes,
                                      [InstructionFileView(f.absolute_path, f.display_path) for f in files[:-1]],
                                      truncated, candidate_style)
        represented = [most_specific] if (included_bytes > 0 or byte_length(most_specific.content) == 0) else []
        return text, truncated, represented

    for candidate_style in [style, _RenderStyle(COMPACT_WORKSPACE_CONTEXT_INTRO, style.section)]:
        text, truncated, represented = attempt(candidate_style)
        if byte_length(text) <= max_bytes:
            return RenderedInstructionContext(
                text,
                [InstructionFileView(f.absolute_path, f.display_path) for f in files[:-1]],
                truncated, represented)

    # 预算极小：仅保留告知性 notice
    compact_notice = escape_instruction_frame_body(
        marker_text(max_bytes,
                    [InstructionFileView(f.absolute_path, f.display_path) for f in files[:-1]],
                    [TruncatedInstruction(most_specific.display_path,
                                          byte_length(most_specific.content), 0)]))
    compact_with_heading = escape_instruction_frame_body(
        "\n\n".join([compact_notice, style.section(with_truncated_content(most_specific, 0))]))
    if byte_length(compact_with_heading) <= max_bytes:
        represented = [most_specific] if byte_length(most_specific.content) == 0 else []
        return RenderedInstructionContext(
            compact_with_heading,
            [InstructionFileView(f.absolute_path, f.display_path) for f in files[:-1]],
            [TruncatedInstruction(most_specific.display_path, byte_length(most_specific.content), 0)],
            represented)
    text = compact_notice if byte_length(compact_notice) <= max_bytes else truncate_utf8(compact_notice, max_bytes)
    return RenderedInstructionContext(text, [], [], [])


class InstructionFileView:
    """仅含 absolutePath / displayPath 的轻量来源视图（用于 omitted 列表）。"""

    def __init__(self, absolute_path: str, display_path: str) -> None:
        self.absolute_path = absolute_path
        self.display_path = display_path


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def render_workspace_instruction_set(files: list, options: dict) -> dict:
    """渲染基线指令集合，返回渲染上下文与存活其内容（含空文件）的文件。"""
    style = baseline_render_style(files, options.get("replacePreviousBaseline"))
    result = render_instruction_context(files, options["maxBytes"], style)
    return {"rendered": result, "included": result.represented}


def render_workspace_context(files: list, options: dict) -> RenderedWorkspaceContext:
    """渲染工作区指令基线（对外出口）。"""
    return render_workspace_instruction_set(files, options)["rendered"]

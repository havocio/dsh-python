"""模型侧文件系统发现工具套件（tool-fs-search，对标 dsh 的 ``dsh-tool-fs-search``）：
``glob`` / ``grep`` 两个工具，返回相对工作目录的路径 / 按文件分组的匹配行。

dsh 原版通过 ``@vscode/ripgrep`` 二进制（``ctx.subprocess`` spawn）执行；本 Python
复刻用**原生实现**（``os.walk`` + ``fnmatch``/``re``），保留 dsh 的全部配置语义
（上限、溢出、超时、stderr 上限、超量采样、spill 可选）。返回路径相对解析后的
工作目录显示，仅在「工作目录与文件系统读取根同源」的部署下可被 follow-up 读取
（dsh 的 v1 部署约定，不做运行时校验）。

本插件拥有 schema、参数校验、路径解析、结果解析、保留、格式化与 best-effort
spill；文件系统遍历本身是原生 Python 实现（零外部依赖）。注入 ``tools`` 与
``systemPrompt``；``spillStore`` 经 ``getattr(ctx, "spillStore", None)`` 可选读取
（格式化结果 spill 是可选能力）。
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

from dsh_py.core.context import AppContext

# --------------------------------------------------------------------------- #
# 默认上限（对齐 dsh 的 search-core.ts 常量）
# --------------------------------------------------------------------------- #
GLOB_MAX_RESULTS = 100              # 一次 glob 内联保留的最大路径数
GREP_MAX_MATCHES = 250             # 一次 grep 内联保留的最大匹配数
GREP_MAX_LINE_BYTES = 2000         # 单条匹配行预览字节上限（保留 UTF-8 边界）
SEARCH_META_MAX_BYTES = 65_536      # 持久化 meta 的序列化字节上限
RAW_OUTPUT_MAX_BYTES = 20_000_000   # 等价「完整原始输出」上限（此处为扫描安全阈值）
SEARCH_GRACE_MS = 3_000            # 终止宽限（ms），本实现透传给语义保留
SEARCH_STDERR_MAX_BYTES = 64 * 1024  # 诊断 stderr 尾迹上限（字节）
SEARCH_TIMEOUT_MS = 30_000         # 工具调用协作超时（ms）
MAX_TIMER_DELAY_MS = 600_000       # 定时器上限（用于校验 graceMs）

# glob 发现遍历时永不下钻的 VCS 元数据目录（双层否定 globs 在原生实现里即「剪枝」）
GLOB_VCS_EXCLUDES = [".git", ".svn", ".hg", ".bzr", ".jj", ".sl"]


# --------------------------------------------------------------------------- #
# 错误词汇
# --------------------------------------------------------------------------- #
class SearchError(Exception):
    """搜索失败；携带稳定 code 以便上层按类型分流（对标 dsh 的 ``SearchError``）。"""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# 保留结构（对标 dsh 的 RetainedItems<T>）
# --------------------------------------------------------------------------- #
@dataclass
class RetainedItems:
    """一次保留的结果：保留项、原始总量、是否截断、保留计数。"""

    items: list
    seen: int
    truncated: bool
    kept: int


# --------------------------------------------------------------------------- #
# 路径与预览
# --------------------------------------------------------------------------- #
def to_workdir_relative(path: str, workdir: str) -> str:
    """把绝对路径映射为相对工作目录的显示形式（统一用 ``/`` 分隔）；工作目录外 / 相对路径原样返回。"""
    if not os.path.isabs(path):
        return path
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(workdir))
    if rel == ".":
        return "."
    if rel == ".." or rel.startswith(f"..{os.sep}"):
        return path.replace(os.sep, "/")
    return rel.replace(os.sep, "/")


def preview_line(line: str, max_bytes: int) -> str:
    """把一行预览截断到 ``max_bytes``（保留 UTF-8 边界），超界追加标记。"""
    encoded = line.encode("utf-8")
    if len(encoded) <= max_bytes:
        return line
    cut = encoded[:max_bytes]
    # 去掉末尾可能残缺的多字节字符（续字节以 10xxxxxx 开头）
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    return cut.decode("utf-8", "ignore") + " (line truncated)"


# --------------------------------------------------------------------------- #
# 保留（cap）
# --------------------------------------------------------------------------- #
def retain_grep_matches(matches: list, max_matches: int, max_line_bytes: int) -> RetainedItems:
    """对 grep 匹配做内联保留：预览每行到 ``max_line_bytes``，保留前 ``max_matches``。"""
    seen = len(matches)
    kept = min(seen, max_matches)
    items = [
        {"path": m["path"], "lineNumber": m["lineNumber"], "line": preview_line(m["line"], max_line_bytes)}
        for m in matches[:max_matches]
    ]
    return RetainedItems(items=items, seen=seen, truncated=seen > max_matches, kept=kept)


def retain_glob_paths(paths: list, max_results: int) -> RetainedItems:
    """对 glob 路径做内联保留：保留前 ``max_results``。"""
    seen = len(paths)
    kept = min(seen, max_results)
    return RetainedItems(items=list(paths[:max_results]), seen=seen, truncated=seen > max_results, kept=kept)


# --------------------------------------------------------------------------- #
# best-effort spill（对标 search-core.ts 的 trySaveFormattedResult）
# --------------------------------------------------------------------------- #
async def try_save_formatted_result(
    ctx: AppContext, exec: dict, suggested_name: str, content: str
) -> Optional[dict]:
    """尽力把完整格式化结果持久化到 ``ctx.spillStore``；任何缺失/失败都返回 ``None``。

    搜索成功绝不会因 spill 不可用而变为错误——调用方保留内联结果并报告未保存的尾部。
    """
    agent = exec.get("agent")
    session_id: Optional[str] = None
    if agent is not None:
        session = getattr(agent, "session", None)
        header = getattr(session, "header", None)
        session_id = getattr(header, "id", None) if header is not None else None
    if session_id is None:
        ctx.logger.warning(f"tool-fs-search: 无会话拥有者，{exec.get('name')} 结果未保存")
        return None
    spill = getattr(ctx, "spillStore", None)
    if spill is None:
        ctx.logger.warning(f"tool-fs-search: 未装载 ctx.spillStore 后端，{exec.get('name')} 结果未保存")
        return None
    save = {
        "owner": {"sessionId": session_id},
        "source": {"toolName": exec.get("name"), "callId": exec.get("callId"), "label": "result"},
        "suggestedName": suggested_name,
        "content": content,
    }
    try:
        return await spill.saveText(save)
    except Exception as exc:  # noqa: BLE001
        ctx.logger.warning(f"tool-fs-search: saveText 失败 {exc}；完整结果未保存")
        return None


# --------------------------------------------------------------------------- #
# 取消检查（协作式）
# --------------------------------------------------------------------------- #
def _check_aborted(exec: dict) -> None:
    signal = exec.get("signal")
    if signal is not None and getattr(signal, "aborted", False):
        raise SearchError(f"{exec.get('name')} 在结束前被中止（工具超时或调用方取消）", "SEARCH_ABORTED")


# --------------------------------------------------------------------------- #
# 工作目录解析
# --------------------------------------------------------------------------- #
def _resolve_workdir(exec: dict) -> str:
    agent = exec.get("agent")
    cwd: Optional[str] = None
    if agent is not None:
        session = getattr(agent, "session", None)
        header = getattr(session, "header", None)
        cwd = getattr(header, "cwd", None) if header is not None else None
    return os.path.abspath(cwd) if cwd else os.getcwd()


# --------------------------------------------------------------------------- #
# glob 原生实现
# --------------------------------------------------------------------------- #
def _glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """把 glob 模式转为正则（``**`` 跨分隔符，``*``/``?`` 段内，``[...]`` 字符类）。"""
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
                if i < n and pattern[i] == "/":
                    i += 1
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            while j < n and pattern[j] != "]":
                j += 1
            if j < n:
                out.append(pattern[i:j + 1])
                i = j + 1
            else:
                out.append(re.escape("["))
                i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$", re.DOTALL)


def _glob_match(rel_posix: str, pattern: str) -> bool:
    """glob 匹配：模式含 ``/`` 时匹配整段路径；否则仅匹配任意深度的文件名（对标 ripgrep）。"""
    regex = _glob_to_regex(pattern)
    if "/" in pattern:
        return bool(regex.match(rel_posix))
    base = rel_posix.rsplit("/", 1)[-1]
    return bool(regex.match(base))


def _iter_files(root_abs: str, exclude_dirs: list) -> list:
    """遍历 ``root_abs`` 下所有文件（剪枝 VCS 目录），返回 [(绝对路径, mtime)]。"""
    results: list = []
    for dirpath, dirnames, filenames in os.walk(root_abs):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                results.append((full, os.stat(full).st_mtime))
            except OSError:
                continue
    return results


def _run_glob(pattern: str, path: Optional[str], workdir: str) -> list:
    """执行 glob，返回按修改时间降序的展示路径列表。"""
    if path is not None:
        root_abs = os.path.abspath(os.path.join(workdir, path))
        if os.path.isfile(root_abs):
            return [to_workdir_relative(root_abs, workdir)]
    else:
        root_abs = workdir
    files = _iter_files(root_abs, GLOB_VCS_EXCLUDES)
    matched: list = []
    for full, _mtime in files:
        rel = os.path.relpath(full, root_abs).replace(os.sep, "/")
        if _glob_match(rel, pattern):
            matched.append(full)
    # ripgrep --sort=modified：修改时间降序
    matched.sort(key=lambda f: os.stat(f).st_mtime, reverse=True)
    return [to_workdir_relative(f, workdir) for f in matched]


# --------------------------------------------------------------------------- #
# glob 超量采样（对标 glob.ts 的 sampleAcrossTopLevel）
# --------------------------------------------------------------------------- #
def _relative_to_search_root(path: str, root: str) -> str:
    if root == ".":
        return path[2:] if path.startswith("./") else path
    root_end = len(root)
    while root_end > 0 and root[root_end - 1] == "/":
        root_end -= 1
    trimmed = root[:root_end]
    if trimmed == "":
        return _strip_leading_separators(path)
    if path == trimmed:
        return ""
    if path.startswith(f"{trimmed}/"):
        return path[len(trimmed) + 1:]
    return path


def _strip_leading_separators(path: str) -> str:
    start = 0
    while start < len(path) and path[start] == "/":
        start += 1
    return path[start:]


def _top_level_segment(path: str) -> str:
    trimmed = _strip_leading_separators(path)
    cut = trimmed.find("/")
    return trimmed if cut == -1 else trimmed[:cut]


def sample_across_top_level(paths: list, max_items: int, root: str = ".") -> list:
    """超量结果按「顶层条目」轮转取样，而非取时间头；保证每棵子树都铺到。"""
    groups: dict = {}
    active: list = []
    for p in paths:
        key = _top_level_segment(_relative_to_search_root(p, root))
        group = groups.get(key)
        if group is None:
            groups[key] = [p]
            active.append({"key": key, "items": groups[key], "index": 0, "current": p})
        else:
            group.append(p)
    taken: dict = {}
    count = 0
    while active and count < max_items:
        next_active: list = []
        for entry in active:
            if count >= max_items:
                break
            count += 1
            bucket = taken.get(entry["key"])
            if bucket is None:
                taken[entry["key"]] = [entry["current"]]
            else:
                bucket.append(entry["current"])
            nxt = entry["index"] + 1
            if nxt < len(entry["items"]):
                next_active.append({**entry, "index": nxt, "current": entry["items"][nxt]})
        active = next_active
    return [p for grp in taken.values() for p in grp]


# --------------------------------------------------------------------------- #
# glob 文本格式化（对标 glob.ts 的 formatGlobOutput / formatGlobPage）
# --------------------------------------------------------------------------- #
def _format_glob_page(items: list, seen: int, spill_ref: Optional[dict], basis: str) -> str:
    body = "\n".join(items)
    recovery = (
        f"完整排序结果存于：{spill_ref['locator']}。{spill_ref['retrievalHint']}"
        if spill_ref is not None
        else "完整结果未能保存；请收窄 pattern 或 path 以查看更多。"
    )
    return f"{body}\n\n(显示 {len(items)} / {seen} 个路径{basis} {recovery})"


def _render_glob_paths(paths: list, max_results: int, sample_over_cap: bool,
                       root: str, spill_ref: Optional[dict]) -> str:
    if not paths:
        return "未找到文件"
    if len(paths) <= max_results:
        return "\n".join(paths)
    if not sample_over_cap:
        return _format_glob_page(paths[:max_results], len(paths), spill_ref, ".")
    sampled = sample_across_top_level(paths, max_results, root)
    total_top = len({_top_level_segment(_relative_to_search_root(p, root)) for p in paths})
    shown_top = len({_top_level_segment(_relative_to_search_root(p, root)) for p in sampled})
    basis = (
        "."
        if total_top == len(paths) or shown_top == total_top
        else f"，跨 {shown_top} / {total_top} 个顶层条目采样（而非按修改时间顺序取头部）"
        + ("。请收窄 path 以检查特定子树。" if shown_top < total_top else "")
    )
    return _format_glob_page(sampled, len(paths), spill_ref, basis)


# --------------------------------------------------------------------------- #
# grep 原生实现
# --------------------------------------------------------------------------- #
def _validate_include(include: str) -> None:
    """单个正 glob 过滤器：非空、非否定、非逗号列表（``{a,b}`` 交替合法）。"""
    if include.strip() == "":
        raise ValueError("include 给定时必须是非空 glob")
    if include.startswith("!"):
        raise ValueError("include 必须是正 glob 过滤器；不支持否定模式（\"!…\"）")
    depth = 0
    for ch in include:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            raise ValueError("include 必须是单个 glob，而非逗号分隔列表（用 {a,b} 交替代替）")


def _parse_grep_args(args: dict) -> dict:
    pattern = args.get("pattern", "")
    if pattern == "":
        raise ValueError("pattern 必须是非空字符串")
    path = args.get("path")
    if path is not None and path.strip() == "":
        raise ValueError("path 给定时必须是非空字符串")
    include = args.get("include")
    if include is not None:
        _validate_include(include)
    return {"pattern": pattern, "path": path, "include": include}


def _iter_grep_files(root_abs: str) -> list:
    """遍历待搜索文件（剪枝 VCS 目录）。"""
    return [full for full, _ in _iter_files(root_abs, GLOB_VCS_EXCLUDES)]


def _run_grep(pattern: str, path: Optional[str], include: Optional[str],
              workdir: str) -> list:
    """执行 grep，返回匹配列表 ``[{path, lineNumber, line}]``（输出顺序）。"""
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise SearchError(f"grep 正则被拒绝：{exc}", "SEARCH_INVALID_PATTERN") from exc

    root_abs = os.path.abspath(os.path.join(workdir, path)) if path else workdir
    matches: list = []
    for full in _iter_grep_files(root_abs):
        if include is not None and not fnmatch.fnmatch(os.path.basename(full), include):
            continue
        try:
            with open(full, "r", encoding="utf-8") as f:
                lines = f.read().split("\n")
        except (UnicodeDecodeError, OSError):
            # 二进制或不可读：跳过（对标 dsh 对无效 UTF-8 行的跳过）
            continue
        display = to_workdir_relative(full, workdir)
        for idx, line in enumerate(lines, start=1):
            if regex.search(line):
                matches.append({"path": display, "lineNumber": idx, "line": line})
    return matches


# --------------------------------------------------------------------------- #
# grep 文本格式化（对标 grep.ts 的 formatGrepOutput / formatGrepMatches）
# --------------------------------------------------------------------------- #
def _group_matches_by_file(matches: list) -> dict:
    by_file: dict = {}
    for m in matches:
        by_file.setdefault(m["path"], []).append(m)
    return by_file


def format_grep_matches(matches: list) -> str:
    """按文件分组渲染：每个文件一行 ``path``，后接 ``Line N: <text>``。"""
    sections: list = []
    for path, group in _group_matches_by_file(matches).items():
        body = "\n".join(f"Line {m['lineNumber']}: {m['line']}" for m in group)
        sections.append(f"{path}\n{body}")
    return "\n\n".join(sections)


def _format_grep_output(retained: RetainedItems, spill_ref: Optional[dict]) -> str:
    header = (
        f"找到 {retained.kept} / {retained.seen} 处匹配"
        if retained.truncated
        else f"找到 {retained.seen} 处匹配"
    )
    body = format_grep_matches(retained.items)
    if not retained.truncated:
        return f"{header}\n\n{body}"
    recovery = (
        f"完整 grep 结果存于：{spill_ref['locator']}。{spill_ref['retrievalHint']}"
        if spill_ref is not None
        else "完整结果未能保存；请收窄 pattern、path 或 include 以查看更多。"
    )
    return f"{header}\n\n{body}\n\n({recovery})"


# --------------------------------------------------------------------------- #
# 工具处理器
# --------------------------------------------------------------------------- #
def _system_prompt_for(ctx: AppContext, name: str, order: int, text: str) -> None:
    if not ctx.has_service("systemPrompt"):
        return
    from dsh_py.services.system_prompt import PromptSection
    ctx.systemPrompt.section(PromptSection(name=name, order=order, text=text))


async def _glob_handler(args: dict, exec: dict, config: dict) -> tuple[str, bool]:
    _check_aborted(exec)
    pattern = args.get("pattern", "")
    if pattern.strip() == "":
        return "错误：pattern 必须是非空字符串", True
    path = args.get("path")
    if path is not None and path.strip() == "":
        return "错误：path 给定时必须是非空字符串", True
    workdir = _resolve_workdir(exec)
    try:
        all_paths = _run_glob(pattern, path, workdir)
    except SearchError as exc:
        return f"错误：{exc}", True

    root = "." if path is None else to_workdir_relative(os.path.abspath(os.path.join(workdir, path)), workdir)
    if not all_paths:
        return "未找到文件", False
    if len(all_paths) <= config["globMaxResults"]:
        return "\n".join(all_paths), False

    spill_ref = None
    if config["sampleOverCapGlobResults"]:
        page = sample_across_top_level(all_paths, config["globMaxResults"], root)
    else:
        page = all_paths[: config["globMaxResults"]]
    # 超量：把完整结果 best-effort 存盘
    spill_ref = await try_save_formatted_result(
        exec["__ctx__"], exec, "glob-results.txt", "\n".join(all_paths))
    text = _render_glob_paths(all_paths, config["globMaxResults"],
                              config["sampleOverCapGlobResults"], root, spill_ref)
    return text, False


async def _grep_handler(args: dict, exec: dict, config: dict) -> tuple[str, bool]:
    _check_aborted(exec)
    try:
        parsed = _parse_grep_args(args)
    except ValueError as exc:
        return f"错误：{exc}", True
    workdir = _resolve_workdir(exec)
    try:
        all_matches = _run_grep(parsed["pattern"], parsed["path"], parsed["include"], workdir)
    except SearchError as exc:
        return f"错误：{exc}", True

    if not all_matches:
        return "未找到匹配", False
    retained = retain_grep_matches(all_matches, config["grepMaxMatches"], config["grepMaxLineBytes"])
    if not retained.truncated:
        return _format_grep_output(retained, None), False

    # 超量：把（预览过的）完整结果 best-effort 存盘
    previewed_all = [
        {"path": m["path"], "lineNumber": m["lineNumber"],
         "line": preview_line(m["line"], config["grepMaxLineBytes"])}
        for m in all_matches
    ]
    spill_content = f"找到 {len(all_matches)} 处匹配\n\n{format_grep_matches(previewed_all)}"
    spill_ref = await try_save_formatted_result(
        exec["__ctx__"], exec, "grep-results.txt", spill_content)
    return _format_grep_output(retained, spill_ref), False


# --------------------------------------------------------------------------- #
# 插件入口
# --------------------------------------------------------------------------- #
def apply(ctx: AppContext, config: Any = None) -> None:
    """注册 ``glob`` / ``grep`` 文件系统发现工具。"""
    config = config or {}
    resolved = {
        "sampleOverCapGlobResults": bool(config.get("sampleOverCapGlobResults", False)),
        "globMaxResults": int(config.get("globMaxResults", GLOB_MAX_RESULTS)),
        "grepMaxMatches": int(config.get("grepMaxMatches", GREP_MAX_MATCHES)),
        "grepMaxLineBytes": int(config.get("grepMaxLineBytes", GREP_MAX_LINE_BYTES)),
        "searchMetaMaxBytes": int(config.get("searchMetaMaxBytes", SEARCH_META_MAX_BYTES)),
        "rawOutputMaxBytes": int(config.get("rawOutputMaxBytes", RAW_OUTPUT_MAX_BYTES)),
        "graceMs": int(config.get("graceMs", SEARCH_GRACE_MS)),
        "stderrMaxBytes": int(config.get("stderrMaxBytes", SEARCH_STDERR_MAX_BYTES)),
        "timeoutMs": int(config.get("timeoutMs", SEARCH_TIMEOUT_MS)),
    }
    for name_, value in [
        ("globMaxResults", resolved["globMaxResults"]),
        ("grepMaxMatches", resolved["grepMaxMatches"]),
        ("grepMaxLineBytes", resolved["grepMaxLineBytes"]),
        ("searchMetaMaxBytes", resolved["searchMetaMaxBytes"]),
        ("rawOutputMaxBytes", resolved["rawOutputMaxBytes"]),
        ("graceMs", resolved["graceMs"]),
        ("stderrMaxBytes", resolved["stderrMaxBytes"]),
        ("timeoutMs", resolved["timeoutMs"]),
    ]:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"tool-fs-search: {name_} 必须是正整数")
    if resolved["graceMs"] > MAX_TIMER_DELAY_MS:
        raise ValueError(f"tool-fs-search: graceMs 不得超过 {MAX_TIMER_DELAY_MS}")

    # 把 ctx 塞进 exec 以便处理器触发 spill（dsh_py 的 exec dict 不带 ctx，但 spill 需要）
    def _make_handler(base):
        async def handler(arguments: dict, exec: dict) -> tuple[str, bool]:
            exec = dict(exec)
            exec["__ctx__"] = ctx
            return await base(arguments, exec, resolved)
        return handler

    GLOB_SCHEMA = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "匹配文件路径的 glob 模式（如 \"**/*.py\"、\"src/**/*.test.js\"）。不含 \"/\" 的模式匹配任意深度的文件名，故 \"*\" 与 \"*.py\" 都会搜索整棵树；要锚定深度请包含分隔符。"},
            "path": {"type": "string", "description": "搜索目录。缺省为会话工作目录；相对路径相对其解析。"},
        },
        "required": ["pattern"],
    }
    GREP_SCHEMA = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "用于搜索文件内容的正则表达式（ripgrep 语法）。"},
            "path": {"type": "string", "description": "要搜索的文件或目录。缺省为会话工作目录；相对路径相对其解析。"},
            "include": {"type": "string", "description": "单 glob 过滤器，限定搜索哪些文件（如 \"*.py\"、\"*.{js,jsx}\"）。不是列表；不支持否定。"},
        },
        "required": ["pattern"],
    }

    ctx.tools.register(
        "glob",
        "按路径模式发现文件（glob）。返回匹配的文件路径——绝不返回目录——包括隐藏与被忽略文件"
        f"（VCS 元数据目录除外）。最多返回 {resolved['globMaxResults']} 个路径（修改时间倒序）；超量时"
        + ("跨顶层条目采样" if resolved["sampleOverCapGlobResults"] else "取修改时间头")
        + "，并报告完整排序列表的保存位置。本工具不列举目录项。",
        GLOB_SCHEMA, _make_handler(_glob_handler), timeout_ms=resolved["timeoutMs"],
    )
    ctx.tools.register(
        "grep",
        "用 ripgrep 风格正则搜索文件内容。返回带行号的匹配行，按文件分组。"
        f"内联最多返回前 {resolved['grepMaxMatches']} 处匹配；截断的结果会报告完整匹配列表的保存位置。"
        "需要上下文时请用 read 读取匹配文件。",
        GREP_SCHEMA, _make_handler(_grep_handler), timeout_ms=resolved["timeoutMs"],
    )

    _system_prompt_for(
        ctx, "tool:glob", 103,
        "用 glob 工具——而非 shell find——按路径模式发现文件。不含 \"/\" 的模式匹配任意深度的文件名，"
        "故 \"*\" 匹配整棵树每个文件而非仅顶层。结果只有文件、绝无目录，包含隐藏与被忽略文件：完整结果按修改时间"
        + ("倒序返回，超量结果跨顶层条目采样，因而铺满整棵树而非单一子树。"
           if resolved["sampleOverCapGlobResults"]
           else "倒序返回，超量结果保留修改时间头。"),
    )
    _system_prompt_for(
        ctx, "tool:grep", 104,
        "用 grep 工具——而非 shell grep 或 rg——搜索文件内容。需要上下文时请用 read 读取匹配文件。",
    )


apply.provides = ["toolFsSearch"]
apply.inject = ["tools", "systemPrompt"]

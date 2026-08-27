"""模型侧 ``web_search`` / ``web_fetch`` 工具（覆盖 ``ctx.web``）。

对齐 dsh 的 ``@deepseek-ai/dsh-tool-web``：本包只拥有 schema、校验、提示指引、
上限与展示，从不拥有具体 provider。启用开关控制工具注册；已启用工具在 provider
不可用时保持可见，执行期以结构化错误（code + message）回流。

适配（dsh_py 差异，均已在 README 注明）：
- dsh 的 ``defineTool`` 带 ``output.schema`` / ``render`` / ``presentationMeta`` /
  ``presentCall`` / ``presentResult``（UI 卡片层）；dsh_py 的 :class:`ToolService`
  无展示层——handler 直接返回模型可见文本（``formatSearchOutput`` /
  ``formatFetchOutput``），卡片与 meta 省略，差异属既有简化契约。
- HTML→Markdown：dsh 用 turndown + GFM 插件（表 / 删除线）；本实现懒加载
  ``markdownify``（缺依赖或转换失败降级为原样返回，坏页胜于报错）。dsh 的
  自定义表单元格渲染规则（防 colspan 展开爆炸）由 markdownify 自身行为替代。
- 渲染备忘：dsh 因 render/presentationMeta 双调用用 WeakMap 备忘；dsh_py 每次
  工具执行只渲染一次，无需备忘。
- ``isConcurrencySafe`` / 输出 schema 校验未在 dsh_py 工具契约中表达（只读
  操作天然并发安全）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional
from urllib.parse import urlsplit

from dsh_py.core.context import AppContext
from dsh_py.services.system_prompt import PromptSection
from dsh_py.services.web import (
    WebError,
    WebFetchRequest,
    WebSearchRequest,
    WebSearchResult,
    service_or_none,
)

# 默认单次搜索来源上限（产品控制，非模型参数；对齐 dsh 与 OpenCode 的 Exa 默认）
WEB_SEARCH_MAX_RESULTS = 8

DEFAULT_WEB_TOOL_TIMEOUT_MS = 30_000
# 抓取输出上限：在本地 provider 默认 10 万字符正文上限之上留出余量
DEFAULT_FETCH_MAX_OUTPUT_CHARS = 200_000

# 嵌套深度上限：超过则 HTML 跳过转换原样返回。转换同步执行，未闭合标签的嵌套
# 使 DOM 树与遍历超线性（实测：512 层 ≈ 0.15s，20000 层 ≈ 5s）。真实页面几十层
# 封顶；512 远高于内容、远低于可武器化。健壮性不变量，非可调项。
MAX_CONVERSION_DEPTH = 512

# 永不携带闭合标签的元素（不增长词法栈）
VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})

# 内容按文本解析直到匹配结束标签的元素
RAW_TEXT_ELEMENTS = frozenset({"script", "style", "noscript"})

TRUNCATION_FOOTER = "\n\n(Content truncated. Fetch a more specific URL or section for the full text.)"

SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "The search query."},
    },
    "required": ["query"],
}

FETCH_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "The HTTP(S) URL to fetch."},
    },
    "required": ["url"],
}

_SEARCH_PROMPT_WITH_FETCH = (
    "Use the web_search tool to discover current information on the web. It returns an optional "
    "answer plus a list of source URLs. Follow up with web_fetch when you need the full content of "
    "a specific result, and cite the relevant URLs as markdown links."
)
_SEARCH_PROMPT_WITHOUT_FETCH = (
    "Use the web_search tool to discover current information on the web. It returns an optional "
    "answer plus a list of source URLs. Use the returned source snippets when available, and cite "
    "the relevant URLs as markdown links."
)
_FETCH_PROMPT = (
    "Use the web_fetch tool to retrieve the content of a specific HTTP(S) URL (for example a result "
    "from web_search). It returns the page content decoded to text. Cite the URL as a markdown link "
    "when you use its content."
)


# ---------------------------------------------------------------------------
# web_search：schema 校验外的值约束 + 展示
# ---------------------------------------------------------------------------

def source_label(url: str, title: Optional[str]) -> str:
    """展示标签：标题，否则 hostname；畸形 URL 回退原文（展示绝不抛出）。"""
    if title and len(title) > 0:
        return title
    try:
        return urlsplit(url).hostname or url
    except ValueError:
        return url


def format_search_output(result: WebSearchResult) -> str:
    """把搜索结果格式化为一个模型可见文本块。"""
    parts: list[str] = []
    if result.content:
        parts.append(result.content)
    if result.sources:
        lines = []
        for source in result.sources:
            label = source_label(source.url, source.title)
            meta = []
            if source.snippet:
                meta.append(source.snippet)
            if source.published_at:
                meta.append(f"({source.published_at})")
            suffix = f" — {' '.join(meta)}" if meta else ""
            lines.append(f"- [{label}]({source.url}){suffix}")
        parts.append(f"Sources:\n{chr(10).join(lines)}")
    elif not result.content:
        parts.append("No results found.")
    if result.truncated:
        parts.append(f"(Showing the first {len(result.sources)} sources. Refine the query for more.)")
    parts.append("Cite the relevant URLs above as markdown links in your answer.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# web_fetch：HTML 词法深度守卫 + 渲染
# ---------------------------------------------------------------------------

def _is_tag_boundary(char: str) -> bool:
    """原始文本结束标签名后允许出现的字符。"""
    return char == "" or char == ">" or char == "/" or char.isspace()


def _find_raw_text_end(lower_html: str, name: str, from_: int) -> int:
    """查找匹配的原始文本结束标签，不解释类标记的正文文本。"""
    prefix = f"</{name}"
    candidate = lower_html.find(prefix, from_)
    while candidate != -1:
        idx = candidate + len(prefix)
        if idx >= len(lower_html) or _is_tag_boundary(lower_html[idx]):
            return candidate
        candidate = lower_html.find(prefix, candidate + len(prefix))
    return -1


def exceeds_conversion_depth(html: str) -> bool:
    """保守拒绝词法元素栈越过深度上限的 HTML。

    单趟扫描：忽略注释内的闭合标签、跳过原始文本正文、尊重引号内 ``>``、只对
    当前元素接受闭合标签；畸形输入因此**高估**嵌套而非掩盖。
    """
    lower_html = html.lower()
    open_elements: list[str] = []
    offset = 0
    in_comment = False
    n = len(html)
    while offset < n:
        start = html.find("<", offset)
        if in_comment:
            end = html.find("-->", offset)
            if end != -1 and (start == -1 or end < start):
                in_comment = False
                offset = end + 3
                continue
        if start == -1:
            break
        if not in_comment and html.startswith("<!--", start):
            in_comment = True
            offset = start + 4
            continue

        cursor = start + 1
        closing = cursor < n and html[cursor] == "/"
        if closing:
            cursor += 1
        name_start = cursor
        while cursor < n and (html[cursor].isalnum() or html[cursor] == "-"):
            cursor += 1
        if cursor == name_start or not html[name_start].isalpha():
            offset = start + 1
            continue

        name = lower_html[name_start:cursor]
        quote: Optional[str] = None
        while cursor < n:
            char = html[cursor]
            cursor += 1
            if quote is not None:
                if char == quote:
                    quote = None
            elif char in ('"', "'"):
                quote = char
            elif char == ">":
                break
        if cursor == 0 or html[cursor - 1] != ">":
            break

        if closing:
            if not in_comment and open_elements and open_elements[-1] == name:
                open_elements.pop()
        else:
            last = cursor - 2
            while last >= 0 and html[last].isspace():
                last -= 1
            if name not in VOID_ELEMENTS and (last < 0 or html[last] != "/"):
                open_elements.append(name)
                if len(open_elements) > MAX_CONVERSION_DEPTH:
                    return True
                if not in_comment and name in RAW_TEXT_ELEMENTS:
                    end = _find_raw_text_end(lower_html, name, cursor)
                    if end == -1:
                        break
                    offset = end
                    continue
        offset = cursor
    return False


def _to_markdown(html: str) -> str:
    """HTML→Markdown；``markdownify`` 缺失时原样返回（降级页面胜于报错）。"""
    try:
        from markdownify import markdownify as _md  # 懒加载：可选依赖
    except ImportError:
        return html
    return _md(html, heading_style="ATX", bullets="-")


def render_body(body: Any, max_input_chars: int) -> tuple[str, bool]:
    """渲染抓取正文为模型可见 markdown 文本。

    ``html`` 经 markdownify 转换、``text`` 原样透传；深度越限或转换失败降级为
    原样返回。返回 ``(文本前缀, 源是否被截断)``。
    """
    content = body.content[:max_input_chars]
    source_truncated = len(content) != len(body.content)
    if body.kind == "html":
        if exceeds_conversion_depth(content):
            return content, source_truncated
        try:
            return _to_markdown(content), source_truncated
        except Exception:  # noqa: BLE001 -- 转换失败降级为原样返回
            return content, source_truncated
    return content, source_truncated


def compute_fetch_output(result: Any, max_output_chars: int) -> tuple[str, bool]:
    """渲染抓取结果为有界文本 + 有效截断标志（头部 + 正文 + 截断脚注）。"""
    header = f"Fetched {result.url} (HTTP {result.status_code})\n\n"
    text, source_truncated = render_body(result.body, max_output_chars)
    prefix = f"{header}{text}"
    truncated = result.truncated or source_truncated or len(prefix) > max_output_chars
    full = f"{prefix}{TRUNCATION_FOOTER}" if truncated else prefix
    if len(full) <= max_output_chars:
        return full, truncated
    if max_output_chars < len(TRUNCATION_FOOTER):
        return full[:max_output_chars], truncated
    return f"{prefix[:max_output_chars - len(TRUNCATION_FOOTER)]}{TRUNCATION_FOOTER}", truncated


def format_fetch_output(result: Any, max_output_chars: int) -> str:
    """把抓取结果格式化为一个模型可见文本块（整体有界）。"""
    return compute_fetch_output(result, max_output_chars)[0]


# ---------------------------------------------------------------------------
# 工具 handler
# ---------------------------------------------------------------------------

async def _web_search_handler(args: dict, exec: dict, ctx: AppContext, max_results: int) -> tuple[str, bool]:
    query = args.get("query", "")
    if not query.strip():
        return "web_search: query must be a non-empty string", True
    web = service_or_none(ctx, "web")
    if web is None:
        return "web_search: the web service is not mounted", True
    signal = exec.get("signal") if isinstance(exec, dict) else None
    try:
        result = await web.search(WebSearchRequest(query=query, max_results=max_results), signal)
    except WebError as exc:
        return f"{exc.code}: {exc}", True
    except asyncio.CancelledError:
        return "WEB_ABORTED: web search aborted", True
    except Exception as exc:  # noqa: BLE001
        return f"web_search failed: {exc}", True
    return format_search_output(result), False


async def _web_fetch_handler(args: dict, exec: dict, ctx: AppContext, max_output_chars: int) -> tuple[str, bool]:
    url = args.get("url", "")
    if not url.strip():
        return "web_fetch: url must be a non-empty string", True
    web = service_or_none(ctx, "web")
    if web is None:
        return "web_fetch: the web service is not mounted", True
    signal = exec.get("signal") if isinstance(exec, dict) else None
    try:
        result = await web.fetch(WebFetchRequest(url=url), signal)
    except WebError as exc:
        return f"{exc.code}: {exc}", True
    except asyncio.CancelledError:
        return "WEB_ABORTED: web fetch aborted", True
    except Exception as exc:  # noqa: BLE001
        return f"web_fetch failed: {exc}", True
    return format_fetch_output(result, max_output_chars), False


# ---------------------------------------------------------------------------
# 插件入口
# ---------------------------------------------------------------------------

def _assert_positive_integer(name: str, value: Any) -> None:
    """配置的计数 / 超时 / 字符上限必须是正整数。"""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"tool-web: {name} must be a positive integer")


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """注册启用的 web 工具与系统提示指引。

    ``search`` / ``fetch`` 默认 true；只需其一的产品在配置里关掉另一个。每个工具
    的协作超时预算（``fetchTimeoutMs`` / ``searchTimeoutMs``，默认 30000）作为
    ``ToolDefinition.timeoutMs`` 附到工具，供 timeout-policy 插件强制。
    """
    config = config or {}
    search_enabled = config.get("search", True)
    fetch_enabled = config.get("fetch", True)
    search_max_results = config.get("searchMaxResults", WEB_SEARCH_MAX_RESULTS)
    fetch_timeout_ms = config.get("fetchTimeoutMs", DEFAULT_WEB_TOOL_TIMEOUT_MS)
    search_timeout_ms = config.get("searchTimeoutMs", DEFAULT_WEB_TOOL_TIMEOUT_MS)
    fetch_max_output_chars = config.get("fetchMaxOutputChars", DEFAULT_FETCH_MAX_OUTPUT_CHARS)
    for name, value in (
        ("searchMaxResults", search_max_results),
        ("fetchTimeoutMs", fetch_timeout_ms),
        ("searchTimeoutMs", search_timeout_ms),
        ("fetchMaxOutputChars", fetch_max_output_chars),
    ):
        _assert_positive_integer(name, value)

    tools = service_or_none(ctx, "tools")
    system_prompt = service_or_none(ctx, "systemPrompt")

    if search_enabled:
        if system_prompt is not None:
            system_prompt.section(PromptSection(
                name="tool:web_search",
                order=110,
                text=_SEARCH_PROMPT_WITH_FETCH if fetch_enabled else _SEARCH_PROMPT_WITHOUT_FETCH,
            ))
        if tools is not None:
            tools.register(
                "web_search",
                "Search the web for current information. Returns an optional summary answer and a list of source URLs.",
                SEARCH_SCHEMA,
                _make_search_handler(ctx, search_max_results),
                timeout_ms=search_timeout_ms,
            )

    if fetch_enabled:
        if system_prompt is not None:
            system_prompt.section(PromptSection(
                name="tool:web_fetch",
                order=111,
                text=_FETCH_PROMPT,
            ))
        if tools is not None:
            tools.register(
                "web_fetch",
                "Fetch the content of a specific HTTP(S) URL and return it decoded to text.",
                FETCH_SCHEMA,
                _make_fetch_handler(ctx, fetch_max_output_chars),
                timeout_ms=fetch_timeout_ms,
            )


def _make_search_handler(ctx: AppContext, max_results: int) -> Any:
    async def handler(args: dict, exec: dict) -> tuple[str, bool]:  # noqa: ANN001
        return await _web_search_handler(args, exec, ctx, max_results)

    return handler


def _make_fetch_handler(ctx: AppContext, max_output_chars: int) -> Any:
    async def handler(args: dict, exec: dict) -> tuple[str, bool]:  # noqa: ANN001
        return await _web_fetch_handler(args, exec, ctx, max_output_chars)

    return handler


apply.inject = ["tools", "web"]  # 声明：本插件依赖这些服务（供 loader 拓扑排序）；systemPrompt 为可选（已守卫）

__all__ = [
    "WEB_SEARCH_MAX_RESULTS",
    "DEFAULT_WEB_TOOL_TIMEOUT_MS",
    "DEFAULT_FETCH_MAX_OUTPUT_CHARS",
    "source_label",
    "format_search_output",
    "exceeds_conversion_depth",
    "format_fetch_output",
    "apply",
]

"""模型侧会话检索/追踪工具套件（tool-session-query，对标 dsh 的 ``dsh-tool-session-query``）。

注册 5 个工具，全部经 ``ctx.sessionQuery``（会话查询引擎，内存版或 SQLite 版皆可）实现：

- ``session_search``：跨语料库全文检索会话（按最强命中事件排名）。
- ``session_event_search``：在单个会话内全文检索事件。
- ``session_trace``：追踪一个会话的祖先链与后代树（谱系）。
- ``session_event_trace``：追踪单个事件的来源与位置替代链。
- ``session_event_read``：读取目标事件及其有界原始日志上下文窗口。

工作目录授权：以调用方会话的 ``cwd`` 为边界——``session_search`` 自动注入同工作目录
过滤；其余以会话为目标的工具校验目标会话 ``cwd`` 与调用方一致（调用方无 cwd 时放行，
便于无工作区的测试/部署）。错误统一翻译为 ``SessionQueryError`` 的 ``code`` 供上层分流。

注入 ``tools`` 与 ``systemPrompt``；``sessionQuery`` 经 ``inject`` 声明为前置于本插件加载
的服务（内存版或 SQLite 版二选一，互斥装配）。所有输出为中文排版，字段结构对齐 dsh 的
presentation 契约。
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.services.session_query import SessionQueryError

# 工具默认分页大小
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


# --------------------------------------------------------------------------- #
# 辅助：调用方工作目录 / 会话授权
# --------------------------------------------------------------------------- #
def _caller_cwd(exec: dict) -> Optional[str]:
    """从 exec 的 agent 链取出调用方会话的工作目录（无则 None）。"""
    agent = exec.get("agent")
    if agent is None:
        return None
    session = getattr(agent, "session", None)
    if session is None:
        return None
    header = getattr(session, "header", None)
    if header is None:
        return None
    return getattr(header, "cwd", None)


def _resolve_session_cwd(ctx: AppContext, session_id: str) -> Optional[str]:
    """解析目标会话的工作目录（live 优先，持久化兜底）。"""
    session = ctx.sessions.get(session_id)
    if session is not None:
        return getattr(session.header, "cwd", None)
    persistence = getattr(ctx.sessions, "_persistence", None)
    if persistence is not None:
        try:
            inspection = persistence.load(session_id)
            if inspection is not None:
                meta = inspection.get("meta")
                return getattr(meta, "cwd", None)
        except Exception:
            return None
    return None


def _authorize(ctx: AppContext, exec: dict, session_id: str) -> Optional[str]:
    """工作目录越界校验：调用方无 cwd 时放行；否则目标会话 cwd 必须一致。

    返回错误文案（is_error 文本）或 None（放行）。
    """
    caller = _caller_cwd(exec)
    if caller is None:
        return None
    target = _resolve_session_cwd(ctx, session_id)
    if target == caller:
        return None
    return f"错误：无权访问会话 {session_id}（工作目录越界，调用方 cwd={caller!r}）"


# --------------------------------------------------------------------------- #
# 结果格式化
# --------------------------------------------------------------------------- #
def _fmt_time(t: Any) -> str:
    try:
        if t is None or not isinstance(t, (int, float)):
            return str(t)
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    except (ValueError, OverflowError, OSError):
        return str(t)


def _fmt_session_header(header: Any) -> str:
    parts = [f"id={header.id}"]
    if getattr(header, "cwd", None):
        parts.append(f"cwd={header.cwd}")
    return " ".join(parts)


def _render_event_search(res: dict) -> str:
    hits = res.get("hits", [])
    query = res.get("query", "")
    if not hits:
        return f"会话内检索（query={query!r}）：未找到匹配事件。"
    lines = [f"会话内检索（query={query!r}）：命中 {len(hits)} 条事件——"]
    for h in hits:
        lines.append(
            f"  · [seq {h['seq']}] type={h['type']} time={_fmt_time(h['time'])} "
            f"matchCount={h.get('matchCount', 0)}"
        )
        if h.get("snippet"):
            lines.append(f"      片段：{h['snippet']}")
    cursor = res.get("cursor")
    if cursor is not None:
        lines.append(f"（本页 {len(hits)} 条；后续翻页可传 cursor={cursor!r}）")
    return "\n".join(lines)


def _render_session_search(res: dict) -> str:
    hits = res.get("hits", [])
    query = res.get("query", "")
    if not hits:
        return f"跨会话检索（query={query!r}）：未找到匹配会话。"
    lines = [f"跨会话检索（query={query!r}）：命中 {len(hits)} 个会话——"]
    for h in hits:
        header = h.get("session")
        hid = header.id if header is not None else "?"
        strong = h.get("strongest", {})
        lines.append(f"  · 会话 {hid}：hitCount={h.get('hitCount', 0)}")
        if strong.get("snippet"):
            lines.append(
                f"      最强命中 [seq {strong.get('seq')}] type={strong.get('type')}：{strong.get('snippet')}"
            )
    cursor = res.get("cursor")
    if cursor is not None:
        lines.append(f"（本页 {len(hits)} 个；后续翻页可传 cursor={cursor!r}）")
    return "\n".join(lines)


def _render_trace_session(res: dict) -> str:
    target = res.get("target", {})
    header = target.get("header") if isinstance(target, dict) else None
    lines = [f"会话谱系追踪：{_fmt_session_header(header) if header is not None else '?'}"]
    ancestors = res.get("ancestors", [])
    if ancestors:
        chain = " <- ".join(_fmt_session_header(a["header"]) for a in ancestors)
        lines.append(f"  祖先链（旧→新）：{chain}")
    else:
        lines.append("  祖先链：无（根会话）")

    def render_tree(nodes, indent):
        out = []
        for node in nodes:
            rec = node.get("session", {})
            out.append(f"{indent}- {_fmt_session_header(rec.get('header'))}")
            out.extend(render_tree(node.get("descendants", []), indent + "  "))
        return out

    descendants = res.get("descendants", [])
    if descendants:
        lines.append("  后代树：")
        lines.extend(render_tree(descendants, "    "))
    else:
        lines.append("  后代树：无")
    return "\n".join(lines)


def _render_trace_event(res: dict) -> str:
    target = res.get("target", {})
    lines = [
        f"事件溯源：会话 {res.get('session', {}).get('id') if isinstance(res.get('session'), dict) else '?'} "
        f"seq={target.get('seq')} type={target.get('type')} surface={target.get('surface')}"
    ]
    sources = res.get("sources", [])
    if sources:
        for s in sources:
            lines.append(f"  来源：seq={s.get('seq')} {s.get('kind')} shadowedBy={s.get('shadowedBy')}")
    else:
        lines.append("  来源：无（原始事件）")
    replacement = res.get("replacement")
    lines.append(f"  位置替代：{replacement if replacement is not None else '无'}")
    return "\n".join(lines)


def _render_read_event(res: dict) -> str:
    header = res.get("session")
    sid = header.id if header is not None else "?"
    start = res.get("startSeq")
    end = res.get("endSeq")
    lines = [f"读取会话 {sid} 事件上下文窗口 [seq {start}..{end}]："]
    for e in res.get("events", []):
        text = _event_text(e)
        lines.append(f"  [{e.seq}] {e.type}：{text if text else '（无检索文本）'}")
    return "\n".join(lines)


def _event_text(event: Any) -> str:
    """从原始日志事件提取可读文本（复用 session_query 的提取逻辑）。"""
    from dsh_py.services.session_query import extract_event_text
    try:
        return extract_event_text(event)
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# 处理器（request-dict 调用 ctx.sessionQuery）
# --------------------------------------------------------------------------- #
def _require(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 必须是非空字符串")
    return value


async def _session_event_search_handler(args: dict, exec: dict, ctx: AppContext) -> tuple[str, bool]:
    session_id = _require(args, "sessionId")
    query = _require(args, "query")
    denied = _authorize(ctx, exec, session_id)
    if denied is not None:
        return denied, True
    request = {
        "sessionId": session_id,
        "query": query,
        "filters": args.get("filters", []),
        "limit": args.get("limit", DEFAULT_LIMIT),
        "cursor": args.get("cursor"),
    }
    res = ctx.sessionQuery.search_events(request)
    res["query"] = query
    return _render_event_search(res), False


async def _session_search_handler(args: dict, exec: dict, ctx: AppContext) -> tuple[str, bool]:
    query = _require(args, "query")
    caller = _caller_cwd(exec)
    session_filters = list(args.get("sessionFilters", []))
    # 工作目录授权：自动注入同 cwd 过滤（调用方有 cwd 时）
    if caller is not None:
        session_filters.append({"kind": "cwd", "values": [caller]})
    request = {
        "query": query,
        "sessionFilters": session_filters,
        "eventFilters": args.get("eventFilters", []),
        "limit": args.get("limit", DEFAULT_LIMIT),
        "cursor": args.get("cursor"),
    }
    res = ctx.sessionQuery.search_sessions(request)
    res["query"] = query
    return _render_session_search(res), False


async def _session_trace_handler(args: dict, exec: dict, ctx: AppContext) -> tuple[str, bool]:
    session_id = _require(args, "sessionId")
    denied = _authorize(ctx, exec, session_id)
    if denied is not None:
        return denied, True
    return _render_trace_session(ctx.sessionQuery.trace_session(session_id)), False


async def _session_event_trace_handler(args: dict, exec: dict, ctx: AppContext) -> tuple[str, bool]:
    session_id = _require(args, "sessionId")
    denied = _authorize(ctx, exec, session_id)
    if denied is not None:
        return denied, True
    try:
        seq = int(args.get("seq"))
    except (TypeError, ValueError):
        return "错误：seq 必须是整数", True
    if seq < 1:
        return "错误：seq 必须 >= 1", True
    return _render_trace_event(ctx.sessionQuery.trace_event(session_id, seq)), False


async def _session_event_read_handler(args: dict, exec: dict, ctx: AppContext) -> tuple[str, bool]:
    session_id = _require(args, "sessionId")
    denied = _authorize(ctx, exec, session_id)
    if denied is not None:
        return denied, True
    try:
        seq = int(args.get("seq"))
    except (TypeError, ValueError):
        return "错误：seq 必须是整数", True
    if seq < 1:
        return "错误：seq 必须 >= 1", True
    before = int(args.get("before", 0))
    after = int(args.get("after", 0))
    return _render_read_event(ctx.sessionQuery.read_event(
        {"sessionId": session_id, "seq": seq, "before": before, "after": after})), False


# --------------------------------------------------------------------------- #
# 插件入口
# --------------------------------------------------------------------------- #
def _system_prompt_for(ctx: AppContext, name: str, order: int, text: str) -> None:
    if not ctx.has_service("systemPrompt"):
        return
    from dsh_py.services.system_prompt import PromptSection
    ctx.systemPrompt.section(PromptSection(name=name, order=order, text=text))


def apply(ctx: AppContext, config: Any = None) -> None:
    """注册会话检索/追踪 5 件套工具。"""
    # 确认 sessionQuery 服务可用（内存版或 SQLite 版）
    query = getattr(ctx, "sessionQuery", None)
    if query is None:
        raise RuntimeError("tool-session-query 需要 ctx.sessionQuery 服务（请先装配 session_query 或 session_query_sqlite）")

    def _wrap(base):
        async def handler(arguments: dict, exec: dict) -> tuple[str, bool]:
            exec = dict(exec)
            exec["__ctx__"] = ctx
            try:
                return await base(arguments, exec, ctx)
            except SessionQueryError as exc:
                return f"错误：{exc.message} [{exc.code}]", True
            except ValueError as exc:
                return f"错误：{exc}", True
            except Exception as exc:  # noqa: BLE001
                return f"错误：会话检索失败——{exc}", True
        return handler

    ctx.tools.register(
        "session_event_search",
        "在单个会话内做全文检索（按匹配次数、时间、seq 排名分页）。"
        "用于定位某次对话里包含特定关键词的事件。返回每条命中的 seq、类型、时间与文本片段，"
        "以及后续翻页 cursor。需要会话工作目录授权。",
        {
            "type": "object",
            "properties": {
                "sessionId": {"type": "string", "description": "目标会话 id。"},
                "query": {"type": "string", "description": "检索关键词（中文按字面子串匹配，英文大小写不敏感）。"},
                "filters": {"type": "array", "description": "可选事件级过滤（seq/time/type/surface/text）。"},
                "limit": {"type": "integer", "description": "本页条数上限（默认 20，最大 100）。"},
                "cursor": {"type": "string", "description": "上一页返回的翻页游标；不填取首页。"},
            },
            "required": ["sessionId", "query"],
        },
        _wrap(_session_event_search_handler),
    )
    ctx.tools.register(
        "session_search",
        "跨全部会话做全文检索，按最强命中事件排名返回会话列表（每个会话附最强命中片段）。"
        "用于「之前是否聊过 X」「找关于 Y 的会话」。自动限定在调用方工作目录内。返回翻页 cursor。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词。"},
                "sessionFilters": {"type": "array", "description": "可选会话级过滤（id/cwd/parent/availability/created-at）。"},
                "eventFilters": {"type": "array", "description": "可选事件级过滤（seq/time/type/surface/text）。"},
                "limit": {"type": "integer", "description": "本页会话数上限（默认 20，最大 100）。"},
                "cursor": {"type": "string", "description": "翻页游标；不填取首页。"},
            },
            "required": ["query"],
        },
        _wrap(_session_search_handler),
    )
    ctx.tools.register(
        "session_trace",
        "追踪一个会话的谱系：祖先链（沿 parent 上溯）与后代树（沿 parent 下展）。"
        "用于理解会话从何派生、有哪些子会话。需要会话工作目录授权。",
        {
            "type": "object",
            "properties": {
                "sessionId": {"type": "string", "description": "目标会话 id。"},
            },
            "required": ["sessionId"],
        },
        _wrap(_session_trace_handler),
    )
    ctx.tools.register(
        "session_event_trace",
        "追踪单个事件：直接来源（压缩/修剪中被遮蔽→替代者）与位置替代链。"
        "用于理解某条事件是怎么来的、是否被其它事件替代。需要会话工作目录授权。",
        {
            "type": "object",
            "properties": {
                "sessionId": {"type": "string", "description": "目标会话 id。"},
                "seq": {"type": "integer", "description": "目标事件 seq（>=1）。"},
            },
            "required": ["sessionId", "seq"],
        },
        _wrap(_session_event_trace_handler),
    )
    ctx.tools.register(
        "session_event_read",
        "读取目标事件及其有界原始日志上下文窗口（before/after 条数）。"
        "用于查看某条事件前后的完整对话片段。需要会话工作目录授权。",
        {
            "type": "object",
            "properties": {
                "sessionId": {"type": "string", "description": "目标会话 id。"},
                "seq": {"type": "integer", "description": "目标事件 seq（>=1）。"},
                "before": {"type": "integer", "description": "目标前保留的事件条数（默认 0）。"},
                "after": {"type": "integer", "description": "目标后保留的事件条数（默认 0）。"},
            },
            "required": ["sessionId", "seq"],
        },
        _wrap(_session_event_read_handler),
    )

    _system_prompt_for(
        ctx, "tool:session-query", 113,
        "用 session_search / session_event_search 工具检索历史会话，而非凭记忆臆测或翻找日志；"
        "session_trace / session_event_trace 用于追溯会话谱系与事件来源；"
        "session_event_read 读取具体事件与上下文窗口。检索结果含翻页 cursor，继续查看请原样回传。",
    )


apply.provides = ["toolSessionQuery"]
apply.inject = ["tools", "systemPrompt", "sessionQuery"]

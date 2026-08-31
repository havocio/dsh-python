"""钩子协议核心（hooks/hook-protocol，治理类；与方言无关的共享库）。

把「外部命令钩子」的匹配、执行、解码、结果合并、耐久事件与分离运行静默统一管理。
Claude Code / Codex 桥各自拥有不同的载荷、环境规则、匹配模式与扩展点映射；dsh_py
在此提供**通用桥**可用的中性能力，不绑定任何外部工具。

核心能力：
- :func:`matches_matcher`：匹配器（literal 管道交替 / regex）；
- :func:`parse_hook_output`：把退出码 + stdout JSON + stderr 解码为 :class:`HookOutput`；
- :func:`run_hook`：经 ``ctx.shell`` 跑钩子命令（复用其凭据擦除、进程组取消、超时机制）；
- :func:`merge_hook_outputs`：按克制规则合并多个钩子输出为单一决策；
- 耐久事件助手 ``append_hook_invoked`` / ``append_hook_result`` 与 ``summarize_stderr``；
- :func:`create_detached_runs`：非阻塞分离运行管理（追踪静默）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from dsh_py.core.context import AppContext


# --------------------------------------------------------------------------- #
# 类型与常量
# --------------------------------------------------------------------------- #
# 引用协议默认每钩子超时（毫秒，10 分钟）—— CC 与 Codex 对未设 timeout 的钩子都用它
DEFAULT_HOOK_TIMEOUT_MS = 600_000
# 进入耐久事件的 stderr 摘要上限（字符）
DEFAULT_STDERR_SUMMARY_MAX_CHARS = 500

# 跑钩子的桥：claude-code / codex（原生插件拦截点不算桥，不写 hook/* 记录）
HookDialect = str  # 'claude-code' | 'codex' | 'generic'


@dataclass
class CommandHook:
    """一个已配置的命令钩子（CC 与 Codex 的 ``{type:'command',command,timeout?}`` 形态）。"""
    command: str
    # 每钩子超时（秒，线缆单位）；runner 转成毫秒。缺省用桥的 defaultTimeoutMs
    timeout_sec: Optional[float] = None


@dataclass
class MatcherGroup:
    """一个匹配器组：一个 ``matcher`` 模式（缺省 / ``''`` / ``'*'`` = 匹配全部）加命中的命令钩子。"""
    hooks: list[CommandHook]
    matcher: Optional[str] = None


# 匹配模式：claude-code 在模式纯 ``[A-Za-z0-9_|]+``（管道=精确交替）时用 literal，否则 regex；
# codex 永远 regex。桥为自身方言选择模式。
MatcherMode = str  # 'claude-code' | 'codex'


@dataclass
class HookOutput:
    """方言中性的钩子产出，由 :func:`parse_hook_output` 从退出码 + stdout + stderr 解析。

    每个字段都可选——钩子可能只用到子集；桥决定哪些字段对其钩子点有意义、哪些忽略
    （faithful-but-degraded，例如 codex 忽略 ``allow``/``ask``）。
    """
    exit_code: Optional[int]
    stderr: str = ""
    stdout: str = ""
    # False ⇒ 钩子要求中止（exit 2）；与 stopReason 配对。True/缺省 ⇒ 继续
    continue_flag: Optional[bool] = None
    stop_reason: Optional[str] = None
    # 中性阻断决策：从两个不同的通道归一（legacy 顶层 decision 与 permissionDecision）
    decision: Optional[str] = None  # 'approve'|'allow'|'block'|'deny'|'ask'
    reason: Optional[str] = None
    hook_event_name: Optional[str] = None
    # 注入下一模型请求的额外上下文（CC additionalContext）
    additional_context: Optional[str] = None
    # 展现给用户的告警（CC systemMessage）
    system_message: Optional[str] = None
    # 钩子请求的工具输入改写（PARSED 但不兑现——输入改写被推迟）
    updated_input: Optional[dict] = None


# --------------------------------------------------------------------------- #
# 匹配（matcher.ts）
# --------------------------------------------------------------------------- #
def _build_pattern(matcher: Optional[str], mode: MatcherMode) -> Optional[re.Pattern]:
    """把匹配器模式编译成正则（按方言模式解释）。匹配全部返回 None。"""
    if matcher is None or matcher == "" or matcher == "*":
        return None
    if mode == "claude-code" and re.fullmatch(r"[A-Za-z0-9_|]+", matcher):
        # literal：管道 = 精确交替；每个 token 按字面
        return re.compile("^(?:" + "|".join(re.escape(tok) for tok in matcher.split("|")) + ")$")
    # regex 模式（含 claude-code 的非纯 token 与 codex 的所有情况）
    try:
        return re.compile(matcher)
    except re.error:
        # 非法正则：降级为永不匹配（与 CC 行为一致——坏模式不误伤）
        return re.compile(r"(?!x)x")


def matches_matcher(matcher: Optional[str], name: str, mode: MatcherMode) -> bool:
    """判断 ``name`` 是否命中给定匹配器（按方言模式）。匹配全部 → True。"""
    pattern = _build_pattern(matcher, mode)
    if pattern is None:
        return True
    return pattern.fullmatch(name) is not None


def matcher_diagnostic(matcher: Optional[str], mode: MatcherMode) -> str:
    """返回匹配器的可读诊断（用于配置校验报错）。"""
    if matcher is None or matcher == "" or matcher == "*":
        return "match-all"
    pattern = _build_pattern(matcher, mode)
    return "regex" if (mode == "codex" or not re.fullmatch(r"[A-Za-z0-9_|]+", matcher or "")) else "literal"


# --------------------------------------------------------------------------- #
# 输出解码（codec.ts）
# --------------------------------------------------------------------------- #
def _apply_parsed(output: HookOutput, parsed: dict, expected_event_name: Optional[str]) -> None:
    """把解析出的 stdout JSON 折进 HookOutput（处理事件名不匹配时丢弃事件域字段）。"""
    event_name = parsed.get("hookEventName") or parsed.get("hook_event_name")
    mismatch = expected_event_name is not None and event_name is not None and event_name != expected_event_name
    output.hook_event_name = event_name

    top_decision = parsed.get("decision")
    specific = parsed.get("hookSpecificOutput") or {}
    permission = specific.get("permissionDecision") if isinstance(specific, dict) else None
    perm_reason = specific.get("permissionDecisionReason") if isinstance(specific, dict) else None

    # 中性决策枚举：allow/deny/ask 仅来自 permissionDecision；顶层 decision 只取 approve/block
    if permission in ("allow", "deny", "ask"):
        output.decision = permission
        output.reason = perm_reason
    elif top_decision in ("approve", "block"):
        output.decision = top_decision
        output.reason = parsed.get("reason") if isinstance(parsed.get("reason"), str) else output.reason

    if mismatch:
        # 事件名不匹配：保留 hookEventName，但丢弃事件域字段
        output.additional_context = None
        output.updated_input = None
        return

    if isinstance(parsed.get("continue"), bool):
        output.continue_flag = parsed["continue"]
    if isinstance(parsed.get("stopReason"), str):
        output.stop_reason = parsed["stopReason"]
    if isinstance(parsed.get("additionalContext"), str):
        output.additional_context = parsed["additionalContext"]
    if isinstance(parsed.get("systemMessage"), str):
        output.system_message = parsed["systemMessage"]
    if isinstance(parsed.get("updatedInput"), dict):
        output.updated_input = parsed["updatedInput"]


def parse_hook_output(
    exit_code: Optional[int], stdout: str, stderr: str, expected_event_name: Optional[str] = None,
) -> HookOutput:
    """解码一次钩子执行结果为 :class:`HookOutput`（永不抛错，基础设施拒绝 → 无退出码）。"""
    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    output = HookOutput(exit_code=exit_code, stderr=stderr, stdout=stdout)

    parsed: Any = None
    if stdout:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None  # 干净退出的非 JSON stdout：保留原始文本供桥决定如何渲染

    if exit_code is None:
        return output  # 基础设施拒绝（命令无法运行）——非阻塞错误，无退出码

    if exit_code == 0:
        if isinstance(parsed, dict):
            _apply_parsed(output, parsed, expected_event_name)
        return output

    if exit_code == 2:
        output.continue_flag = False
        if isinstance(parsed, dict):
            _apply_parsed(output, parsed, expected_event_name)
            if output.stop_reason is None and output.decision == "block":
                output.stop_reason = output.reason
        if output.stop_reason is None:
            output.stop_reason = stderr or "hook blocked via exit code 2"
        return output

    # 其他非零：非阻塞错误（无干净退出码可行动）
    return output


# --------------------------------------------------------------------------- #
# 执行（runner.ts）
# --------------------------------------------------------------------------- #
@dataclass
class RunHookOptions:
    """一次钩子调用所需、超出命令行的所有信息。"""
    payload: Any                          # 写入钩子 stdin 的 JSON 载荷（桥构建）
    env: Optional[dict] = None            # 钩子进程的额外环境变量
    cwd: Optional[str] = None             # 工作目录
    signal: Any = None                    # 所属操作的取消信号
    trailing_newline: bool = True         # stdin 载荷是否追加换行（CC 是，Codex 否）
    default_timeout_ms: int = DEFAULT_HOOK_TIMEOUT_MS
    expected_event_name: Optional[str] = None


@dataclass
class RunHookResult:
    """``HookOutput`` 加运行墙钟时长（耐久 hook/result 用）。"""
    output: HookOutput
    duration_ms: int


async def run_hook(
    bash, hook: CommandHook, options: RunHookOptions, now: Callable[[], int],
) -> RunHookResult:
    """以序列化 stdin 运行 ``hook`` 并解码其产出。

    钩子自带超时（秒）覆盖默认超时；可信环境条目在 executor 擦除后合并。基础设施拒绝
    变成「无退出码」的产出，故本函数**永不抛错或崩溃调用方 turn**。
    """
    started = now()
    timeout_ms = int((hook.timeout_sec if hook.timeout_sec is not None else options.default_timeout_ms / 1000.0) * 1000)
    stdin = json.dumps(options.payload, ensure_ascii=False) + ("\n" if options.trailing_newline else "")

    request = {
        "command": hook.command,
        "timeout_ms": timeout_ms,
        "stdin": stdin,
        "signal": options.signal,
        **({"cwd": options.cwd} if options.cwd is not None else {}),
        **({"env": options.env} if options.env is not None else {}),
    }
    try:
        # dsh_py 的 shell executor 接收关键字并执行，返回 {stdout,stderr,exit_code,...}
        result = await bash.run(request)
        exit_code = result.get("exit_code")
        exit_code = None if exit_code is None else int(exit_code)
        return RunHookResult(
            output=parse_hook_output(exit_code, result.get("stdout", ""), result.get("stderr", ""), options.expected_event_name),
            duration_ms=now() - started,
        )
    except Exception as error:  # noqa: BLE001 - executor 仅在基础设施故障时拒绝
        message = str(error) if isinstance(error, Exception) else repr(error)
        return RunHookResult(
            output=parse_hook_output(None, "", message, options.expected_event_name),
            duration_ms=now() - started,
        )


# --------------------------------------------------------------------------- #
# 结果合并（merge.ts）—— 克制合并多个钩子输出为一个决策
# --------------------------------------------------------------------------- #
@dataclass
class MergedOutcome:
    """多个钩子输出的合并结果。"""
    decision: str = "pass"            # 'pass' | 'block' | 'ask'
    block_reason: Optional[str] = None
    additional_contexts: list = field(default_factory=list)
    system_messages: list = field(default_factory=list)
    # 合并期间出现的警告/忽略（输入改写等）
    warnings: list = field(default_factory=list)


def merge_hook_outputs(outputs: list[HookOutput]) -> MergedOutcome:
    """按克制规则合并：任一 deny/block → 阻断（首个原因）；ask 提升；额外上下文累加。"""
    outcome = MergedOutcome()
    saw_ask = False
    for out in outputs:
        # 阻断：exit 2 的 continue:false，或 decision 为 block/deny
        if out.continue_flag is False or out.decision in ("block", "deny"):
            if outcome.decision != "block":
                outcome.decision = "block"
                outcome.block_reason = out.stop_reason or out.reason or "hook blocked the operation"
        if out.decision == "ask":
            saw_ask = True
        if out.additional_context:
            outcome.additional_contexts.append(out.additional_context)
        if out.system_message:
            outcome.system_messages.append(out.system_message)
        if out.updated_input is not None:
            outcome.warnings.append("hook requested input rewrite (updatedInput) — deferred, not honored")
    if outcome.decision != "block" and saw_ask:
        outcome.decision = "ask"
    return outcome


# --------------------------------------------------------------------------- #
# 耐久事件（events.ts）
# --------------------------------------------------------------------------- #
def summarize_stderr(stderr: str, max_chars: int = DEFAULT_STDERR_SUMMARY_MAX_CHARS) -> str:
    """把 stderr 截断为进入耐久事件的摘要。"""
    stderr = stderr or ""
    if len(stderr) <= max_chars:
        return stderr
    return f"{stderr[:max_chars]}… (+{len(stderr) - max_chars} more chars)"


def append_hook_invoked(
    ctx: AppContext, session, turn: int, point: str, dialect: HookDialect,
    handler_id: str, matcher: Optional[str] = None,
) -> None:
    """写一条 log-only 的 ``hook/invoked`` 事件（不可表面事件，不携带 surfaceOp）。"""
    session.append("hook/invoked", {
        "turn": turn, "point": point, "dialect": dialect,
        "matcher": matcher, "handlerId": handler_id,
    })


def append_hook_result(
    ctx: AppContext, session, turn: int, point: str, handler_id: str,
    decision: str, duration_ms: int, exit_code: Optional[int] = None, stderr_summary: Optional[str] = None,
) -> None:
    """写一条与 ``hook/invoked`` 经 handlerId 配对的 log-only ``hook/result`` 事件。"""
    session.append("hook/result", {
        "turn": turn, "point": point, "handlerId": handler_id, "decision": decision,
        "exitCode": exit_code, "stderrSummary": stderr_summary, "durationMs": duration_ms,
    })


# --------------------------------------------------------------------------- #
# 分离运行（detached.ts）—— 非阻塞运行管理，追踪静默
# --------------------------------------------------------------------------- #
class DetachedRuns:
    """一组非阻塞钩子运行的轻量管理：启动后后台跑，提供「全部静默」查询。

    dsh 的 detached 运行用于在 turn 不阻塞的情况下跑钩子（如 Stop 钩子），dsh_py
    在此提供同级能力：提交协程、追踪未完成集合、查询是否全部完成。
    """

    def __init__(self) -> None:
        self._pending: set = set()
        self._seq = 0

    def submit(self, coro: Awaitable, on_done: Optional[Callable[[Any], None]] = None) -> None:
        """提交一个协程在后台运行；完成时自动从待集合中移除。"""
        import asyncio
        self._seq += 1
        task = asyncio.ensure_future(coro)

        def _clear(fut):
            self._pending.discard(task)
            if on_done is not None:
                try:
                    on_done(fut.result() if fut.exception() is None else None)
                except Exception:  # noqa: BLE001
                    pass

        self._pending.add(task)
        task.add_done_callback(_clear)

    def quiesced(self) -> bool:
        """是否所有分离运行都已静默。"""
        return len(self._pending) == 0


# --------------------------------------------------------------------------- #
# 共享执行引擎：按方言运行某拦截点的全部钩子组并合并（桥共用，避免各自重复）
# --------------------------------------------------------------------------- #
def _hook_now() -> int:
    """钩子墙钟起点（毫秒）。"""
    return int(time.time() * 1000)


def _hook_decision_of(output: HookOutput) -> str:
    """把单条钩子产出归约为耐久事件用的中性决策串。"""
    if output.continue_flag is False or output.decision in ("block", "deny"):
        return "block"
    if output.decision == "ask":
        return "ask"
    return "pass"


async def run_hook_point(
    ctx: AppContext,
    groups: list,
    point: str,
    match_query: str,
    payload: Any,
    opts: dict,
) -> "MergedOutcome":
    """按方言运行 ``point`` 命中的全部钩子组，写 ``hook/invoked``/``hook/result`` 并返回合并结果。

    ``groups`` 为该拦截点的 :class:`MatcherGroup` 列表（未命中 matcher 的组跳过）；``opts``
    携带：``agent``/``turn``/``signal``、方言 ``mode``（``claude-code``/``codex``）与
    ``dialect``、``trailing_newline``、``default_timeout_ms``、``stderr_summary_max_chars``、
    ``project_dir``（写入 ``CLAUDE_PROJECT_DIR`` 环境变量）、``env``（额外环境变量）、
    ``plain_stdout_as_context``（Codex：干净 stdout 转上下文）、``next_handler_id``（生成 handlerId）。
    ``turn`` 为 ``None`` 时不写耐久事件（分离运行的生命周期点）。
    """
    agent = opts.get("agent")
    turn = opts.get("turn")
    signal = opts.get("signal")
    mode: MatcherMode = opts.get("mode", "claude-code")
    dialect: HookDialect = opts.get("dialect", "generic")
    trailing_newline: bool = opts.get("trailing_newline", True)
    default_timeout_ms: int = opts.get("default_timeout_ms", DEFAULT_HOOK_TIMEOUT_MS)
    stderr_cap: int = opts.get("stderr_summary_max_chars", DEFAULT_STDERR_SUMMARY_MAX_CHARS)
    project_dir = opts.get("project_dir")
    env_extra = opts.get("env") or {}
    plain_stdout = opts.get("plain_stdout_as_context", False)
    next_id = opts["next_handler_id"]

    outputs: list = []
    # 工作目录：优先 opts.cwd，否则取 agent 会话的 cwd（钩子运行目录）
    workdir = opts.get("cwd")
    if workdir is None and agent is not None:
        header = getattr(getattr(agent, "session", None), "header", None)
        workdir = getattr(header, "cwd", None)
    hook_env = {**( {"CLAUDE_PROJECT_DIR": project_dir} if project_dir else {} ), **env_extra} or None
    session = getattr(agent, "session", None) if agent is not None else None

    for group in groups:
        if not matches_matcher(group.matcher, match_query, mode):
            continue
        for hook in group.hooks:
            handler_id = next_id(point)
            if session is not None and turn is not None:
                append_hook_invoked(ctx, session, turn, point, dialect, handler_id, group.matcher)
            options = RunHookOptions(
                payload=payload,
                env=hook_env,
                cwd=workdir,
                signal=signal,
                trailing_newline=trailing_newline,
                default_timeout_ms=default_timeout_ms,
                expected_event_name=point,
            )
            result = await run_hook(ctx.shell, hook, options, _hook_now)
            out = result.output
            # Codex：干净的非 JSON stdout 折叠进上下文（不泄漏裸 JSON 或非零输出）
            if plain_stdout and out.exit_code == 0 and out.additional_context is None \
               and out.stdout and not out.stdout.startswith("{"):
                out.additional_context = out.stdout
            if out.updated_input is not None:
                ctx.logger.warn(f"{dialect}: {point} 钩子请求了 updatedInput，暂不支持（已忽略）")
            if out.system_message is not None:
                ctx.logger.warn(f"{dialect}: {point} 钩子输出了 systemMessage，暂未呈现（已忽略）")
            outputs.append(out)
            if session is not None and turn is not None:
                append_hook_result(
                    ctx, session, turn, point, handler_id, _hook_decision_of(out),
                    result.duration_ms, exit_code=out.exit_code,
                    stderr_summary=summarize_stderr(out.stderr, stderr_cap) if out.stderr else None,
                )
    return merge_hook_outputs(outputs)

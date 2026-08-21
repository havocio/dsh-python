"""面向模型的任务控制工具（jobs/tool-jobs，第 3 层）。

在 ``ctx.jobs`` 之上注册 ``job_output`` / ``job_list`` / ``job_kill`` 三个
工具。加载插件即附着任务控制器（生产者靠它获准开工），并把未报告完成交付给
拥有 agent。

- ``job_output`` —— 读后台任务：流式任务只返回上次读取以来的增量；终态输出
  任务在 settle 后返回结果。每响应以 ``[status: ...]`` 结尾；非阻塞，除非
  ``wait: true``（最多等配置上限）。
- ``job_list`` —— 列出自己的后台任务（含已完成）。
- ``job_kill`` —— 请求取消运行中任务；立即返回，任务实际停止后以 killed settle。

**与 dsh 的差异（已注明）**：
- dsh 区分 idle 唤醒（``owner.followup``）与 busy 注入（``owner.inject``），
  并有 ``maxConsecutiveWakes`` 预算；dsh_py 的 agent 无 ``status``/``inject``，
  完成通知统一 ``owner.followup``（文档差异）；
- dsh 用 ``finalizeContent``/``output.render`` 做输出上限；dsh_py 在 handler
  内直接以 :mod:`dsh_py.util.retention` 约束模型可见文本。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.services.jobs import JobId
from dsh_py.services.message import MessageSource, TextBlock, create_user_message
from dsh_py.services.system_prompt import PromptSection
from dsh_py.util.retention import TextRetainer

logger = logging.getLogger("dsh_py.tool_jobs")

Config = z.object({
    "waitTimeoutMs": z.integer().default(30_000),
    "maxWaitTimeoutMs": z.integer().default(600_000),
    "completionDelivery": z.string().default("wakeup"),  # 'quiet' | 'wakeup'（dsh_py 统一 followup）
    "maxConsecutiveWakes": z.integer().default(3),
})


def status_line(snapshot: dict) -> str:
    """渲染通用状态行（含可选生产者 detail）。"""
    detail = snapshot.get("detail")
    return f"[status: {snapshot['status']}, {detail}]" if detail is not None \
        else f"[status: {snapshot['status']}]"


def public_job(snapshot: dict) -> dict:
    """从注册表快照去掉所有权与通知簿记字段（面向模型程序安全）。"""
    result = {
        "id": str(snapshot["id"]),
        "kind": snapshot["kind"],
        "label": snapshot["label"],
        "status": snapshot["status"],
        "startedAt": snapshot["startedAt"],
    }
    if snapshot.get("detail") is not None:
        result["detail"] = snapshot["detail"]
    if snapshot.get("finishedAt") is not None:
        result["finishedAt"] = snapshot["finishedAt"]
    return result


def _retain(text: str, kind: str, max_bytes: int) -> str:
    """按预算约束文本（tail/head）。"""
    retainer = TextRetainer({"kind": kind, "maxBytes": max_bytes})
    retainer.push(text)
    return retainer.finish()["text"]


def _fit_with_suffix(content: str, suffix: str, max_bytes: Optional[int], omitted: str) -> str:
    """把内容 + 后缀约束到字节上限（后缀保留，内容尾部截断 + 省略标记）。"""
    complete = content + suffix
    if max_bytes is None or len(complete.encode("utf-8")) <= max_bytes:
        return complete
    fixed = ("" if content.endswith(omitted.strip()) else omitted) + suffix
    fixed_bytes = len(fixed.encode("utf-8"))
    if fixed_bytes >= max_bytes:
        return _retain(fixed, "tail", max_bytes)
    return _retain(content, "tail", max_bytes - fixed_bytes) + fixed


def _completion_notice(snapshot: dict, cap: Optional[int]) -> str:
    """一个已 settle 任务的单行完成通知（有界）。"""
    prefix = f"background job {snapshot['id']}"
    detail = f" ({snapshot['kind']}: {snapshot['label']}) finished {status_line(snapshot)}"
    complete = f"{prefix}{detail}. Read its output with job_output."
    if cap is None or len(complete.encode("utf-8")) <= cap:
        return complete
    omitted = "\n[notice truncated]"
    action = "\nDone; job_output."
    fixed = f"{prefix}{omitted}{action}"
    if len(fixed.encode("utf-8")) <= cap:
        return fixed
    compact = f"{prefix}{action}"
    if len(compact.encode("utf-8")) <= cap:
        return compact
    return _retain(compact, "tail", cap)


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：注册三个任务控制工具 + 附着控制器 + 完成通知。"""
    cfg = config or {}
    wait_default = int(cfg.get("waitTimeoutMs", 30_000))
    wait_cap = int(cfg.get("maxWaitTimeoutMs", 600_000))
    if wait_default > wait_cap:
        raise ValueError(f"tool-jobs: waitTimeoutMs ({wait_default}) 超过 maxWaitTimeoutMs ({wait_cap})")

    # 生产者只有控制器附着时才能开工
    ctx.jobs.attachController("tool-jobs")

    ctx.systemPrompt.section(PromptSection(
        name="tool:jobs",
        order=106,
        text=(
            "Track every background job id you start. You are notified in-session when a job "
            "finishes — do not busy-poll or sleep on one; keep working on independent steps and "
            "do not duplicate a running job's work. Before giving a final answer, collect every "
            "still-relevant job with job_output (set wait: true only when you are genuinely "
            "blocked on it), and job_kill jobs that stopped mattering."
        ),
    ))

    # 完成通知：把未报告完成交付给拥有 agent（dsh_py 统一 followup；见模块差异）
    ctx.jobs.onJobDone(lambda snapshot, owner: _deliver_notice(ctx, snapshot, owner))

    # ------------------------------------------------------------------ #
    # job_output
    # ------------------------------------------------------------------ #
    async def job_output(arguments: dict, exec: dict) -> tuple:
        raw_id = arguments.get("job_id", "")
        if raw_id == "":
            return "invalid job_id: 应为非空字符串", True
        id = JobId(raw_id)
        try:
            if arguments.get("wait") is True:
                timeout = min(float(arguments.get("timeout_ms", wait_default) or wait_default), float(wait_cap))
                await ctx.jobs.wait(id, timeout, exec.get("agent"), exec.get("signal"))
            read = ctx.jobs.read(id, exec.get("agent"))
        except RuntimeError as exc:
            return str(exc), True
        body = read["text"] if read["text"] else "(no new output)"
        snapshot = read["snapshot"]
        if body.endswith("\n"):
            body = body[:-1]
        suffix = f"\n{status_line(snapshot)}"
        cap = snapshot.get("outputLimitBytes")
        text = _fit_with_suffix(body, suffix, cap, "\n[output truncated]")
        return text, False

    # ------------------------------------------------------------------ #
    # job_list
    # ------------------------------------------------------------------ #
    async def job_list(arguments: dict, exec: dict) -> tuple:
        jobs = ctx.jobs.list(exec.get("agent"))
        if not jobs:
            return "(no background jobs)", False
        lines = [f"{j['id']} [{j['kind']}] {j['status']} — {j['label']}" for j in jobs]
        return "\n".join(lines), False

    # ------------------------------------------------------------------ #
    # job_kill
    # ------------------------------------------------------------------ #
    async def job_kill(arguments: dict, exec: dict) -> tuple:
        raw_id = arguments.get("job_id", "")
        if raw_id == "":
            return "invalid job_id: 应为非空字符串", True
        id = JobId(raw_id)
        try:
            outcome = ctx.jobs.kill(id, exec.get("agent"), arguments.get("reason"))
            snapshot = ctx.jobs.get(id, exec.get("agent"))
        except RuntimeError as exc:
            return str(exc), True
        if outcome == "already-finished":
            text = f"job {snapshot['id']} had already finished {status_line(snapshot)}"
        else:
            text = f"requested cancellation of job {snapshot['id']}"
        cap = snapshot.get("outputLimitBytes")
        if cap is not None:
            text = _retain(text, "tail", cap)
        return text, False

    ctx.tools.register(
        "job_output",
        "Read a background job. Stream jobs return only output since the previous read; "
        "final-output jobs return their result after settlement. Every response ends with "
        "`[status: ...]`. Reads are non-blocking unless `wait: true`, which waits up to the configured cap.",
        {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "required": True,
                           "description": "Job id returned by the tool that started the background work."},
                "wait": {"type": "boolean",
                         "description": "Block until the job reaches a terminal status or the timeout expires."},
                "timeout_ms": {"type": "number",
                               "description": "Max wait in milliseconds (only meaningful with wait: true)."},
            },
        },
        job_output,
    )
    ctx.tools.register(
        "job_list",
        "List your background jobs (running and finished) with their ids, kinds, and statuses.",
        {"type": "object", "properties": {}},
        job_list,
    )
    ctx.tools.register(
        "job_kill",
        "Request cancellation of a running background job by job id. Returns immediately; the job "
        "settles as killed once its work actually stops.",
        {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "required": True,
                           "description": "Job id returned by the tool that started the background work."},
                "reason": {"type": "string",
                           "description": "Optional short reason, recorded in the log and forwarded to the job."},
            },
        },
        job_kill,
    )


def _deliver_notice(ctx: AppContext, snapshot: dict, owner: Any) -> None:
    """把未报告完成作为 plugin 来源 notice 消息投递给拥有 agent。"""
    if snapshot.get("reported") or owner is None:
        return
    message = create_user_message(
        [TextBlock(_completion_notice(snapshot, snapshot.get("outputLimitBytes")))],
        source=MessageSource("plugin", plugin="tool-jobs", form="notice"),
    )
    owner.followup(message)


apply.Config = Config
apply.name = "tool-jobs"
apply.inject = ["tools", "jobs", "systemPrompt"]

"""workflow 生命周期不变式伴生（对标 dsh 的 ``@deepseek-ai/dsh-workflow/invariant``）。

检查每次运行的 start/end 与子调用配对。**差异**：dsh 用 cordis 的
``internal/dispatch`` 在事件到达观察者之前做拦截校验；dsh_py 的 EventBus 无
该内部事件，改为在公开 ``workflow/*`` 事件上直接校验（
:meth:`WorkflowEngine.emit_workflow_event` 对 InvariantError 响亮传播，见
``__init__.py``）。语义检查本身 1:1：身份快照一致性、seq/childId 合法性、
agent-end 配对与 outcome、终态结果覆盖全部已观察 start。
"""

from __future__ import annotations

import json
from typing import Any

from dsh_py.core.context import AppContext

PACKAGE_NAME = "dsh-workflow"

#: Cordis 伴生插件名。
name = "workflow-invariant"
#: 保留包名额前必需的服务。
inject = ["invariants"]


def _trace_for(traces: dict, info: Any, fail: Any) -> dict:
    """要求一次运行的每个事件都保留其已校验的身份快照。"""
    trace = traces.get(info.id)
    if trace is None:
        fail(f"workflow event has no matching workflow/start for run {json.dumps(str(info.id))}")
    if trace["meta"] != json.dumps(info.meta.to_dict(), sort_keys=True):
        fail(f"workflow event meta diverges from workflow/start for run {json.dumps(str(info.id))}")
    return trace


def _validate_agent_end(start: Any, end: Any, fail: Any) -> None:
    """断言一个 agent 配对共享的不可变身份字段。"""
    if start.label != end.label or start.phase != end.phase or start.childId != end.childId:
        fail(f"workflow/agent-end identity diverges from workflow/agent-start for seq {end.seq}")
    if end.outcome not in ("completed", "failed", "cancelled"):
        fail(f"workflow/agent-end carries unknown outcome {json.dumps(end.outcome)}")


def _validate_workflow_end(trace: dict, result: Any, fail: Any) -> None:
    """对照累积的运行 trace 校验终态结果。"""
    if trace["agents"]:
        fail(f"workflow/end has {len(trace['agents'])} agent call(s) without workflow/agent-end")
    if not isinstance(result.agentsStarted, int) or result.agentsStarted < trace["starts"]:
        fail("workflow/end agentsStarted must be a safe integer covering every observed agent start")
    if (result.stopReason == "completed") != (result.error is None):
        fail("workflow/end error must be absent exactly for completed runs")


def install(ctx: AppContext, fail: Any) -> None:
    """安装 workflow start/end 与子调用配对检查（注册全局监听）。"""
    traces: dict = {}
    # 身份快照用 JSON 序列化后的 meta（事件携带的是规范化 WorkflowMeta）。
    # 由于 emit_workflow_event 逐例隔离并响亮传播 InvariantError，这里每个
    # fail() 都会中止对应事件的发布并向上抛出。
    ctx.on(
        "workflow/start",
        lambda info: _on_start(traces, info, fail),
        global_=True,
    )
    ctx.on(
        "workflow/agent-start",
        lambda info, agent: _on_agent_start(traces, info, agent, fail),
        global_=True,
    )
    ctx.on(
        "workflow/agent-end",
        lambda info, agent: _on_agent_end(traces, info, agent, fail),
        global_=True,
    )
    ctx.on(
        "workflow/end",
        lambda info, result: _on_end(traces, info, result, fail),
        global_=True,
    )


def _on_start(traces: dict, info: Any, fail: Any) -> None:
    if str(info.id) == "" or len(info.meta.name) == 0 or len(info.meta.description) == 0:
        fail("workflow/start id, meta.name, and meta.description must be non-empty")
    if info.id in traces:
        fail(f"workflow/start repeated run id {json.dumps(str(info.id))}")
    traces[info.id] = {
        "meta": json.dumps(info.meta.to_dict(), sort_keys=True),
        "agents": {},
        "starts": 0,
    }


def _on_agent_start(traces: dict, info: Any, agent: Any, fail: Any) -> None:
    trace = _trace_for(traces, info, fail)
    if not isinstance(agent.seq, int) or agent.seq < 1 or str(agent.childId) == "":
        fail("workflow/agent-start seq must be positive and childId must be non-empty")
    if agent.seq in trace["agents"]:
        fail(f"workflow/agent-start repeated seq {agent.seq}")
    trace["agents"][agent.seq] = agent
    trace["starts"] += 1


def _on_agent_end(traces: dict, info: Any, agent: Any, fail: Any) -> None:
    trace = _trace_for(traces, info, fail)
    start = trace["agents"].get(agent.seq)
    if start is None:
        fail(f"workflow/agent-end has no matching start for seq {agent.seq}")
    _validate_agent_end(start, agent, fail)
    trace["agents"].pop(agent.seq, None)


def _on_end(traces: dict, info: Any, result: Any, fail: Any) -> None:
    trace = _trace_for(traces, info, fail)
    _validate_workflow_end(trace, result, fail)
    traces.pop(info.id, None)


def apply(ctx: AppContext) -> None:
    """注册 workflow 不变式伴生。"""
    ctx.invariants.register(PACKAGE_NAME, install)

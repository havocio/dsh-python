"""子代理工具（对标 dsh 的 ``tool-subagent``，进程内最小形态）。

父 Agent 通过调用工具 ``subagent`` 派生一个**子代理**执行独立任务：

- 参数 ``prompt`` 是**独立完整提示**——子代理看不到父对话（对标 dsh 的
  standalone prompt 语义）；
- 子代理复用当前 ctx 的默认循环与装配（新会话 + ``ctx.agents.create_agent``），
  跑完一轮后把子会话里全部 assistant 文本汇总返回给父代理；
- ``max_depth`` 深度限制（默认 3，``0`` 禁止）防无限递归——超过即拒绝执行；
- 子代理会话不持久化（临时工作空间）。
"""

from __future__ import annotations

from typing import Any

from dsh_py.core.context import AppContext
from dsh_py.services.agent import AgentOptions
from dsh_py.services.message import as_text


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``subagent`` 工具（依赖 tools / agents / sessions 服务）。

    配置（可选）：
    - ``tool_name``：工具名（默认 ``subagent``）；
    - ``provider`` / ``model`` / ``system``：子代理的路由与系统提示（必填
      provider/model，否则子代理无法发起模型调用）；
    - ``max_depth``：最大子代理深度（默认 3，0 禁止派生）；
    - ``description``：工具描述。
    """
    config = config or {}
    tool_name = config.get("tool_name", "subagent")
    provider = config.get("provider", "")
    model = config.get("model", "")
    system = config.get("system", "")
    max_depth = int(config.get("max_depth", 3))
    depth = {"value": 0}  # 当前调用栈深度（asyncio 单线程内安全）

    async def handler(args: dict) -> tuple[str, bool]:
        prompt = str(args.get("prompt", ""))
        if not provider or not model:
            return "子代理未配置 provider/model（注册 subagent 工具时指定）", True
        if not ctx.has_service("agents") or not ctx.has_service("sessions"):
            return "agents / sessions 服务未就绪", True
        if depth["value"] >= max_depth:
            return f"子代理深度超限（上限 {max_depth}）：拒绝执行", True
        depth["value"] += 1
        try:
            sub_session = ctx.sessions.create()
            sub_agent = ctx.agents.create_agent(
                sub_session,
                AgentOptions(provider=provider, model=model, system=system),
            )
            await sub_agent.run(prompt)
            parts = []
            for ev in sub_session.events:
                if ev.type == "assistant/message":
                    text = as_text(ev.data["message"].content)
                    if text:
                        parts.append(text)
            return "\n".join(parts) or "(子代理未产生文本输出)", False
        except Exception as exc:  # noqa: BLE001 - 子代理错误作为文本回流
            return f"子代理执行异常：{exc}", True
        finally:
            depth["value"] -= 1

    ctx.tools.register(
        tool_name,
        config.get("description", "派生一个子代理执行独立任务（提示需自包含，子代理看不到当前对话）"),
        {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
        handler,
    )


apply.inject = ["tools", "agents", "sessions"]  # 依赖声明（loader 拓扑排序）
"""子代理汇报工具（tool-subagent-report，对标 dsh 的 ``dsh-tool-subagent-report``）：
把 ``ctx.subagents`` 的 ``report_from`` 能力暴露为子代理可调用的 ``report`` 工具——

- ``report``（``ctx.subagents.report_from``）：子代理向**启动它的直接父 Agent** 提交选定
  结果内容（结束前调用一次，含自包含结论）；子为授权凭证，调用方无法指定收件人。

实现要点（零外部依赖，薄适配层）：
- 调用方即子 Agent（``exec["agent"]``），由其 ``id`` 反查直接父并投递；越权/缺失由运行时
  抛 ``NO_CHILD`` / ``PARENT_NOT_LIVE``；
- 错误翻译：运行时的 ``WorkflowError`` 折叠为工具结果文本（``is_error=True``）；
- ``delivery``（``wakeup``/``quiet``）由插件配置决定（默认 ``wakeup`` 唤醒父处理，
  ``quiet`` 仅入父收件箱不唤醒）。

偏差说明：dsh 把 ``report`` 工具按子作用域安装（仅子可见）；dsh_py 工具为全局注册，故
该工具对所有 agent 可见——子调用时正常投递，父误调用会因无父链而报 ``NO_CHILD``。已注明。
"""

from __future__ import annotations

from typing import Any

from dsh_py.core.context import AppContext
from dsh_py.services.message import TextBlock


def _translate_error(exc: Exception) -> str:
    code = getattr(exc, "code", "") or ""
    text = str(exc)
    if code:
        return f"错误[{code}]：{text}"
    return f"错误：{text}"


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``report`` 子代理汇报工具（依赖 ``ctx.subagents`` 与 ``ctx.tools``）。"""
    config = config or {}
    delivery = str(config.get("reportDelivery") or "wakeup")

    async def report_handler(args: dict, exec: dict) -> tuple:
        output = args.get("output", "")
        if not str(output).strip():
            return "错误：output 必填", True
        child = exec.get("agent")
        if child is None:
            return "错误：report 需要一个持有会话的调用方子 Agent", True
        content = [TextBlock(str(output))]
        try:
            msg_id = await ctx.subagents.report_from(child, content, {"delivery": delivery})
        except Exception as exc:  # noqa: BLE001 - 折叠为工具错误
            return _translate_error(exc), True
        return f"已向父代理提交报告（消息 id：{msg_id}）。", False

    ctx.tools.register("report", "向启动你的父代理提交选定结果内容（结束前调用一次，含自包含结论）；仅你的直接父接收。", {
        "type": "object",
        "properties": {
            "output": {"type": "string", "description": "提交给父代理的可执行内容（总结结论，引用相关共享路径）。"},
        },
        "required": ["output"],
    }, report_handler)


apply.provides = ["toolSubagentReport"]
apply.inject = ["tools", "subagents"]

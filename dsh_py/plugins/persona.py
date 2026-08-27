"""逐 agent 人格（可组合行，dsh ``@deepseek-ai/dsh-persona``）。

``system-prompt`` 拥有全局人格（自身 config，无条件注册该段）——本行是
**scope-only**：挂进 agent 预设时为该会话遮蔽部署人格（恰如 ``subagent`` 安装的
逐子人格）；全局挂载则与注册表自身的注册冲突并响亮失败。这正是本行存在的
理由：预设无法挂载提示注册表本身，没有自己的行，预设只能改工具、改不了身份。

复用 dsh_py ``system_prompt`` 的 ``PERSONA_SECTION`` / ``PERSONA_ORDER``（与 dsh
同值，避免两处硬编码漂移）。

适配：dsh 的 ``ctx.effect(() => section(...))`` → ``section()`` 返回的注销函数
注册为 fiber 清理；``suppressRuntimeContext`` 在 dsh_py systemPrompt 未实现
（缺省含运行时上下文），文档化省略。
"""

from __future__ import annotations

from typing import Optional

from dsh_py.core.context import AppContext
from dsh_py.services.system_prompt import PERSONA_ORDER, PERSONA_SECTION, PromptSection


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """为挂载上下文的作用域注册人格段。"""
    config = config or {}
    text = config.get("text") or ""
    if not ctx.has_service("systemPrompt"):
        raise RuntimeError("persona: the systemPrompt service is not mounted")
    section = PromptSection(name=PERSONA_SECTION, order=PERSONA_ORDER, text=text)
    if config.get("complete"):
        section.complete = True
    undo = ctx.systemPrompt.section(section)
    ctx.effect(undo, "persona.section()")


apply.inject = ["systemPrompt"]  # 声明：本插件需要 systemPrompt 服务（供 loader 拓扑排序）

__all__ = ["PERSONA_SECTION", "PERSONA_ORDER", "apply"]

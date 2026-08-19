"""唯一装配点：用户层 profile（对齐 dsh 的 ``cordis.patch.yml`` 语义）。

**bundle 层（核心服务）由 ``boot`` 自动叠加**（内置 :data:`dsh_py.loader.CORE_PROFILE`：
llm / sessions / tools / agents / agentLoop），这里只需写：

1. **业务插件行**：``apply_llm``（OpenAI 兼容适配器，7 厂商）与
   ``apply_memory``（跨会话长记忆）——也可以换成你自己的插件；
2. **patch 指令**（可选）：按 id 覆盖 / 禁用 / 插入核心行，例如
   ``{"id": "agentLoop", "config": {"max_steps": 32}}``。

因此默认运行与自定义都指向本文件；自定义组件 = 直接编辑这里的 ``PROFILE``。
"""

from __future__ import annotations

from dsh_py.services.adapters.openai_compatible import apply as apply_llm
from dsh_py.plugins.long_term_memory import apply as apply_memory
# from dsh_py.plugins.system_instructions import apply as apply_instructions

PROFILE = [
    # 业务插件行（bundle 核心服务由 boot 自动叠加，无需重复列出）
    apply_llm,
    apply_memory,
    # 示例 patch：覆盖核心行 agentLoop 的配置（取消注释生效）
    # {"id": "agentLoop", "config": {"max_steps": 32}},
]
# 如需系统指令注入，改为：
# PROFILE = [
#     apply_llm,
#     (apply_instructions, {"instructions": "你是一个简洁、严谨的助手。"}),
#     apply_memory,
# ]

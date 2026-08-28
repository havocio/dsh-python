"""唯一装配点：用户层 profile（对齐 dsh 的 ``cordis.patch.yml`` 语义）。

**bundle 层（核心服务）由 ``boot`` 自动叠加**（内置 :data:`dsh_py.loader.CORE_PROFILE`：
llm / sessions / tools / agents / agentLoop），这里只需写：

1. **业务插件行**：``apply_llm``（OpenAI 兼容适配器，7 厂商）、``apply_deepseek``
   （DeepSeek 官方专用适配器，``deepseek-official`` 路由）与
   ``apply_memory``（跨会话长记忆）——也可以换成你自己的插件；
2. **patch 指令**（可选）：按 id 覆盖 / 禁用 / 插入核心行，例如
   ``{"id": "agentLoop", "config": {"max_steps": 32}}``。

因此默认运行与自定义都指向本文件；自定义组件 = 直接编辑这里的 ``PROFILE``。
"""

from __future__ import annotations

from dsh_py.services.adapters.openai_compatible import apply as apply_llm
from dsh_py.services.adapters.deepseek import apply as apply_deepseek
from dsh_py.plugins.long_term_memory import apply as apply_memory
# from dsh_py.plugins.system_instructions import apply as apply_instructions
# ── web 能力（取消注释启用，见下方 PROFILE 示例）──
# from dsh_py.services.web import apply as apply_web
# from dsh_py.services.web_fetch_http import apply as apply_web_fetch_http
# from dsh_py.plugins.tool_web import apply as apply_tool_web
# from dsh_py.services.web_search_exa import apply as apply_web_search_exa
# from dsh_py.services.web_search_perplexity import apply as apply_web_search_perplexity
# from dsh_py.services.web_search_deepseek import apply as apply_web_search_deepseek
# ── skill 能力（取消注释启用，见下方 PROFILE 示例）──
# from dsh_py.services.skill import apply as apply_skill
# from dsh_py.services.skill_filesystem import apply as apply_skill_fs
# from dsh_py.services.skill_badge import apply as apply_skill_badge
# from dsh_py.plugins.tool_skill import apply as apply_tool_skill
# ── workspace（取消注释启用）──
# from dsh_py.services.storage import apply as apply_storage
# from dsh_py.services.storage_domain import apply as apply_storage_domain
# from dsh_py.services.session_persistence import apply as apply_session_persistence
# from dsh_py.services.workspace_registry import apply as apply_workspace
# ── agent-presets（取消注释启用；组合解析需 PyYAML）──
# from dsh_py.services.agent_presets import apply as apply_agent_presets
# from dsh_py.plugins.persona import apply as apply_persona
# ── interaction 家族（权限/命令/用户提问；取消注释启用）──
# from dsh_py.services.commands import apply as apply_commands
# from dsh_py.services.user_approval import apply as apply_user_approval
# from dsh_py.services.user_questions import apply as apply_user_questions
# from dsh_py.services.permission_presets import apply as apply_permission_presets  # 需 shell+approval+sessions
# from dsh_py.plugins.tool_ask_user import apply as apply_tool_ask_user  # 需 tools+userQuestions
# ── subagent-acp（外进程 ACP 子代理；取消注释启用）──
# from dsh_py.plugins.subagent_acp import apply as apply_subagent_acp  # 需 subagents+subprocess

PROFILE = [
    # 业务插件行（bundle 核心服务由 boot 自动叠加，无需重复列出）
    apply_llm,          # OpenAI 兼容：openai/qwen/zhipu/moonshot/deepseek/ollama/vllm
    apply_deepseek,     # DeepSeek 官方专用：deepseek-official 路由（thinking/reasoning）
    apply_memory,
    # 示例 patch：覆盖核心行 agentLoop 的配置（取消注释生效）
    # {"id": "agentLoop", "config": {"max_steps": 32}},
    # ── web 能力（web_fetch / web_search 工具）── 取消注释启用 ──
    # apply_web,               # ctx.web seam（搜索/抓取 provider 注册表）
    # apply_web_fetch_http,    # 本地 HTTP(S) 抓取 provider（懒加载 httpx）
    # apply_tool_web,          # 模型侧工具：web_search + web_fetch
    # 需要搜索 provider 时（均需对应 API key，缺 key 仅 provider 不可用/执行期报错）：
    # apply_web_search_exa,            # EXA_API_KEY
    # apply_web_search_perplexity,     # PERPLEXITY_API_KEY
    # apply_web_search_deepseek,       # DEEPSEEK_API_KEY（DeepSeek 官方搜索）
    # ── skill 能力（技能目录 + skill 工具）── 取消注释启用 ──
    # apply_skill,           # ctx.skills 注册表（provides: skills）
    # apply_skill_fs,        # 本地目录技能 provider（pathlib 发现 + frontmatter；watch 需 watchdog）
    # apply_skill_badge,     # 内置 dsh-badge 技能
    # apply_tool_skill,      # 模型侧 skill 工具 + 会话目录 + /name 手势注入
    # ── workspace 能力（耐久工作区注册表）── 取消注释启用（需 storage 系列 + sessionPersistence）──
    # apply_storage,                     # ctx.storage（多后端）
    # apply_storage_domain,              # ctx.storageDomain（域数据形式）
    # apply_session_persistence,         # ctx.sessionPersistence（JSONL）
    # apply_workspace,                   # ctx.workspaceRegistry
    # ── agent-presets（预设注册表 + persona 行）── 取消注释启用（组合解析需 PyYAML）──
    # apply_agent_presets,               # ctx.agentPresets（discovery/创作/常驻挂载）
    # 预设目录：~/.dsh/.agent-presets/<id>/agent.cordis.yml（每目录一份插件行组合）
    # ── interaction 家族（权限/命令/用户提问）── 取消注释启用 ──
    # apply_commands,                    # ctx.commands（斜杠命令注册表）
    # apply_user_approval,               # ctx.approval（批准服务；提供 systemPrompt 片段）
    # apply_user_questions,              # ctx.userQuestions（提问 seam）
    # apply_permission_presets,          # ctx.permissionPresets（需 shell+approval+sessions；注册 /permission 命令 + permissions 投影）
    # apply_tool_ask_user,               # 模型侧 ask_user_question 工具（需 tools+userQuestions）
    # ── subagent-acp（外进程 ACP 子代理）── 取消注释启用（需 subagents+subprocess）──
    # 以「python 脚本子代理」为例：command 是程序本身，args 传脚本路径；也可换成任意 ACP 可执行文件
    # (apply_subagent_acp, {"command": "python", "args": ["/path/to/acp-agent.py"], "cwd": "C:/ws"}),
    # ── web 前端演示（--webui）── 取消注释即点亮全部面板 ──
    # 命令面板 + 目标 + 技能 + 工作区 + 后台任务（缺省仅对话可用，面板显示装配引导）
    # apply_commands,                     # 命令面板（console/commands/*）
    # apply_goal,                         # 目标面板（console/goals/get）
    # apply_skill, apply_skill_fs, apply_skill_badge,   # 技能面板（console/skills/*）
    # apply_jobs,                         # 任务面板（console/jobs/list）
    # apply_storage, apply_storage_domain, apply_session_persistence, apply_workspace,  # 工作区面板（console/workspaces/list）
]
# 如需系统指令注入，改为：
# PROFILE = [
#     apply_llm,
#     (apply_instructions, {"instructions": "你是一个简洁、严谨的助手。"}),
#     apply_memory,
# ]

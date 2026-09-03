# 目录结构

> 本文件从 `README.md` 拆出，收录 `dsh_py/` 完整目录树，便于按层定位模块。

```
dsh_py/
├─ core/                         # ── 第 0 层：cordis 内核翻译 ──
│   context.py                    作用域树（extend/isolate/intercept）+ DI + 事件代理
│   events.py                     EventBus（emit/parallel/serial/bail/waterfall）
│   fiber.py                      Fiber 状态机 + ctx.effect 资源清理
│   schema.py                     schemastery 子集（校验 + 默认值）
│   signal.py                     CancelSignal（对标 AbortSignal，支持 .any 融合）
│   service.py / logger.py / reflect.py / registry.py
├─ loader.py                     # ── 第 1 层：Loader/Boot ──
│                                 CORE_PROFILE / compose_entries / boot / load_profile
│                                 _topo_sort（inject/provides 依赖拓扑）
├─ env.py                        .env 分层加载 + 环境变量插值
├─ config.py                     AppConfig + load_app_config（三层深合并 + ${VAR}）
├─ watcher.py                    profile 热重载（st_mtime_ns 轮询）
│
├─ services/                     # ── 第 2/3 层：三 seam + 支撑服务 ──
│   llm.py                        LlmAdapter/StreamChunk/LlmService/HarnessError
│   message.py / session.py / agent.py / tools.py / inbox.py
│   call_config.py / retry_policy.py / attribution.py / brand.py
│   session_persistence.py        Jsonl + Sqlite（zstd/checkpoint）
│   projection.py / projection_cache.py / session_stats.py / session_query.py
│   system_prompt.py              sections/variables/renderPrompt 严格 {{var}}
│   settings.py / credentials.py
│   compaction.py / compaction_basic.py / token_meter.py / tool_result_pruner.py
│   commands.py / fs.py / shell.py / terminal.py
│   agent_instructions/           AGENTS.md/CLAUDE.md 工作区指令加载器
│   subagents.py                  ctx.subagents seam（workflow 子 agent）
│   subprocess.py                 ctx.subprocess seam（完全指定 SpawnSpec + env scrub）
│   subprocess_local.py           树级 spawn（killpg/taskkill）+ OutputCollector + parse_proc_stat
│   web.py                        ctx.web seam（搜索/抓取 provider 注册表 + 执行期选择）
│   web_fetch_http.py             HTTP(S) 抓取 provider（httpx 懒加载、同源重定向、尺寸/超时上限）
│   web_search_deepseek.py        DeepSeek 官方搜索（Anthropic Messages + web_search_20250305）
│   web_search_exa.py / web_search_perplexity.py   Exa / Perplexity 搜索 provider
│   scope.py                     dsh-scope 移植：ScopeKey/父链/NamedEntries/ScopedLayers
│   skill.py                     ctx.skills 分层注册表（provider 合并/rank 裁决/collect 缓存）
│   skill_filesystem.py          本地目录技能 provider（pathlib 发现 + frontmatter 解析）
│   skill_watch.py               可选 watchdog 目录监视（懒加载，缺依赖降级不监视）
│   skill_badge.py               内置 dsh-badge 技能（正文内嵌）
│   workspace.py                 workspace 域词汇/实体（spec/entity/paths，域数据形式）
│   workspace_registry.py        ctx.workspaceRegistry 耐久注册表（create/delete/排序/归档/引导）
│   agent_presets/               ── agent-presets（预设注册表）──
│     __init__.py                AgentPresets 服务（发现/创作/常驻挂载/加入/重链）
│     discovery.py               扫描根 + 组合健康检查（agent.cordis.yml）
│     authoring.py               复制/删除/读取（user 根限定）
│     metadata.py                preset.yml 显示元数据（懒 yaml）
│     mount.py                   组合装载（load_profile 适配）+ 常驻查找
│     session.py / preset.py     会话预设解析 / 词汇与错误
│   commands.py                  ctx.commands 命令注册表（ScopedLayers 分层 + execute 生命周期）
│   user_approval.py             ctx.approval 批准服务（策略折叠 + ask/decided 审计对 + fail-closed）
│   user_questions.py            ctx.userQuestions 提问 seam（唯一 provider + ask 校验）
│   permission_presets.py        ctx.permissionPresets 权限预设（旋钮捆绑 + 投影 + /permission 命令）
│   goal.py / goal_fold.py / goal_round_driver.py
│   plan_mode.py / hooks_protocol.py / schedule.py / schedule_domain.py
│   attachment.py / attachment_image.py / attachment_local.py
│   message_feedback.py / anonymous_user_id.py
│   storage.py / storage_domain.py / storage_json.py / storage_sqlite.py / storage_kv.py
│   spill.py / spill_local.py / jobs.py / jobs_local.py / session_reference.py
│   invariants.py / typert.py / token_meter.py
│   workflow/                     ── workflow 编排引擎（脚本解释执行）──
│     __init__.py                 WorkflowEngine seam + 6 个 workflow/* 事件
│     runtime.py                  WorkflowExecution：exec 注入 agent/parallel/...
│     engine.py                   InlineWorkflowEngine + Run 管理器（折叠 host/session/worker）
│     realm.py / meta.py / schema.py / port.py / invariant.py
│   adapters/
│     openai_compatible.py        7 厂商 OpenAI 兼容
│     deepseek.py                 deepseek-official 专用（thinking/reasoning）
│     pi_ai.py                    llm-pi-ai 通用多路由适配器
│
├─ plugins/                      # ── 模型面工具 / 治理插件 ──
│   system_instructions.py / long_term_memory.py / subagent.py
│   tool_fs.py / tool_bash.py / tool_terminal.py / tool_todo.py
│   tool_goal.py / tool_jobs.py / tool_workflow.py / tool_ralph.py
│   tool_web.py                  web_search / web_fetch 工具（HTML 深度守卫 + markdownify 懒加载）
│   tool_skill.py                skill 工具 + 会话目录 + /name 手势注入（skill-catalog / skill-invocation 来源）
│   persona.py                   逐 agent 人格（scope-only，遮蔽部署人格）
│   tool_ask_user.py             ask_user_question 工具（暂停等待人类回答）
│   command_compact.py / command_feedback.py / command_goal.py
│   guard_repeat_tool.py / guard_timeout.py / hooks.py / spill_policy.py
│   time_context.py / mcp_client/          MCP 桥接（client + bridge + 插件入口）
│
├─ api/                          # ── 第 4 层：跨进程 SDK ──
│   protocol.py                   newline JSON-RPC 2.0 行传输
│   server.py / client.py / websocket.py
│   web_bridge.py                 web 交互桥（批准 answerer / 提问 provider）
├─ gateway.py                    常驻 WebSocket 网关入口（--port/--mock/--host/--webui/--patch）
├─ webui/index.html              内置浏览器前端（对齐 dsh 三栏布局与 --dsw-* token；消费 console.* 方法面）
├─ examples/webui-demo-profile.py  webui 全面板点亮示例 profile（--patch 装配）
├─ sdk.py                        DeepSeekHarness / HarnessSession / RunResult（进程内）
├─ cli.py                        --profile/--patch/--config/--provider/--model/--system/
│                                 --mock/--message/--jsonrpc
├─ configs/
│   profile.py                   唯一装配点（用户层）
│   dsh_config.py                唯一配置编辑点（key/参数/路径）
└─ tests/                        89 个测试模块（纯 assert 脚本，无需 pytest）
```

# TODO / 路线图

> 本文件从 `README.md` 拆出。粗粒度进度见 `docs/PROGRESS.md`。

## 近期

- [~] 第 3 层剩余六包代码已按 dsh 源码落地（`code-runtime`/`sandbox`/`lsp`/`acp`/`tmux-context`/`e2b`），均通过 `py_compile`；`code-runtime`/`sandbox` 已带 seam 契约单测入库，`lsp`/`acp`/`tmux-context` 尚未做运行时验证与单测（`e2b` 后端用户已排除）

- [X]  **`web` 家族落地**（`ctx.web` seam + web-fetch-http + DeepSeek/Exa/Perplexity 搜索 + tool-web 两工具）：纯函数 42 项冒烟 + 装配（拓扑排序/工具注册）已验证；传输/搜索需 `httpx` 与对应 API key
- [X]  **`skill` 家族落地**（先移植 `dsh-scope` 子系统 → `ctx.skills` 分层注册表 + filesystem/badge provider + tool-skill 工具/目录/手势）：纯函数 19 项 + 装配 6 项冒烟已验证；frontmatter 优先懒 yaml、监视需可选 `watchdog`
- [X]  **`workspace` 落地**（`ctx.workspaceRegistry` 耐久工作区注册表：workspace 域 + 历史引导 + 实体成员校验）：端到端 31 项冒烟已验证（fake storageDomain/persistence 环境）
- [X]  **`preset` 落地**（`ctx.agentPresets` 预设注册表 + `persona` 行：发现/健康/创作/常驻挂载/加入与重链/会话解析）：端到端 28 项冒烟已验证；组合解析依赖 **PyYAML**（已装入隔离 venv）
- [X]  **`interaction` 家族落地**（`ctx.commands` 命令注册表 + `ctx.approval` 批准服务 + `ctx.userQuestions` 提问 seam + `ctx.permissionPresets` 权限预设 + `ask_user_question` 工具）：纯函数 + 端到端 **90 项冒烟已验证**（含 lifecycle 审计对、fail-closed、策略折叠、投影/命令装配）
- [~]  六包补 mock 单元测试：`test_code_runtime`/`test_sandbox` 已入库（✅）；`test_lsp`/`test_acp`/`test_tmux_context` 待补（复用既有 fake ctx/shell 范式）
- [ ]  `web`/`skill`/`workspace`/`preset`/`interaction` 家族补 `test_web`/`test_skill`/`test_tool_skill`/`test_workspace`/`test_agent_presets`/`test_interaction` 模块（复用冒烟用例）—— 用户拍板暂缓
- [ ]  `e2b` 适配器 `fs-e2b`/`subprocess-e2b` —— **用户已明确排除**（需 e2b 账号 + SDK），仅保留 `services/e2b.py` 占位 seam
- [X]  **interaction 的 web 侧 UI 适配器（核心部分）**：命令面板 `Ctrl+K`、`/permission` 之外的批准弹窗桥（`approval/request` 通知 → `console/approval/decide`）、user-questions provider 桥（`user-questions/ask` → `console/questions/answer`）、会话列表与历史回放、轨迹面板；**仍未做**：`/permission` 预设切换 UI、富块渲染（Diff/Read/Search/Web/Terminal/JsonTree）、附件、模型选择、主题切换、i18n
- [ ]  附件上传（`ui-attachment`）：需网关 multipart 通道 + `ctx.attachment` 方法面
- [ ]  模型选择（`ui-model-selection`）：需 `console/models/list` 方法面（`ctx.llm.list_providers` 已有）
- [ ]  设置页真实读写：需 `console/settings/get|set` + `console/credentials/*` 方法面
- [ ]  主题切换（浅/深）：`--dsw-*` token 双主题值已备齐（见 `webui/index.html`），缺切换逻辑与持久化
- [X] **`subagent-acp` 落地**（外进程 ACP 子代理后端：`AcpProvider` + `ctx.subagents` backend 分派 + `AcpClientConnection` 客户端 + `start_acp_run` 握手/竞速/折叠 + EOF→terminate dispose 阶梯）：纯函数 + 端到端 **35 项冒烟已验证**（fake ACP 子进程：握手/输出收集/权限自动应答/取消/启动失败/进程早期退出/seam 分派）
- [~]  第 4 层 `host`：**后端子服务已落地**（webserver `ctx.webServer` HTTP 路由载体 + plugin-inventory `ctx.pluginInventory` 经 `ctx.loader` 投影 + 轻量 `loader` 服务 `ctx.loader`，由 `boot` 填充）；不移植 `directory-picker*`(Electron 原生) 与完整 `apiproxy`(被 `gateway.py` 覆盖)。剩余：跨语言 client 完整版（TS/React 生态，非 Python 范围）
- [X]  遥测 / session-telemetry-otel（`services/session_telemetry.py` 捕获协调器 + `services/session_telemetry_otel.py` OTel 后端，FULL/FEEDBACK_ONLY/DISABLED 三模式；`test_session_telemetry.py`/`test_session_telemetry_otel.py` 全绿）
- [X] **Web 前端落地**（`--webui`：内置单页 `webui/index.html`，同端口 HTTP+WS 复用；**三栏布局对齐 dsh** + 命令面板/目标/技能/工作区/jobs/设置面板消费 `console.*` 方法面；点侧栏自动切「面板」tab、缺服务面板显示装配引导；示例装配 `examples/webui-demo-profile.py`——WS 端到端已验证（initialize/console 各面/prompt/chunk/status/shutdown）
- [X] **`session-title` 系列落地**（4 子包：`services/session_title.py` + `session_title_llm.py` + `plugins/session_title_{first-prompt,all-prompts}_llm.py`）：`test_session_title.py` 全绿
- [X] **`tool-fs-search` + `tool-str-replace-editor` 落地**：`test_tool_fs_search.py` / `test_tool_str_replace_editor.py` 全绿
- [X] **`hooks-claude-code` / `hooks-codex` 落地**：`test_hooks_config.py`（10 例）/ `test_hooks_bridges.py`（9 例）全绿
- [X] **`tool-bash-persistent` 落地**：`test_tool_bash_persistent.py`（8 例）全绿（已修 asyncio.Lock 重入死锁）
- [X] **`tool-subagent-control` + `tool-subagent-report` 落地**：`test_tool_subagent_control.py`（6 例）/ `test_tool_subagent_report.py`（3 例）全绿
- [X] **`session-query-sqlite` + `tool-session-query` 落地**：`test_session_query_sqlite.py`（7 例）/ `test_tool_session_query.py`（8 例）全绿
- [X] **`shell-env`**（`ctx.shellEnv` 注册表：`services/shell_env.py` 含 `DSH_*` 内置变量 + session-persistence contributor；已接线 `tool_bash`/`tool_bash_persistent` 的 `collect(execution)` 注入、`terminal.py` 支持 `env`、注册默认 profile；单测 `test_shell_env.py` 22 例全绿）
- [ ]  完整 Web 应用剩余项：跨语言 client 完整版（TS/React 生态，`host` 后端子服务 webserver/plugin-inventory/loader 已落地；`directory-picker*`/完整 `apiproxy` 不移植）；**网关令牌鉴权已落地**（`services/gateway_auth.py` + `api/websocket.py` 接入 `auth`，默认关闭、配置 `gateway.authToken` 或 `DSH_GATEWAY_TOKEN` 启用）

## 已完结里程碑

- [X]  真实 key 联调：DeepSeek / OpenAI / 本地 Ollama 网关（已确认）
- [X]  MCP `streamable-http` 传输端到端验证
- [X]  跨进程 SDK 运行时（newline JSON-RPC）+ Web 网关（WebSocket 广播）
- [X]  typert 协议层（@remote/@remote_scope）
- [X]  session sqlite 持久化（zstd）+ checkpoint 崩溃恢复 + projection/query 完整版
- [X]  compaction 记忆压缩全套（basic + pruner + `/compact`）
- [X]  workflow 编排引擎（seam + 内联引擎 + workflow/ralph 两工具）
- [X]  subprocess 进程 seam（树级 spawn + 终止升级 + process-inspector）
- [X]  goal-round-driver 同会话续行驱动（goal 家族闭环）
- [X]  shell / jobs / bash 接线到 subprocess seam（消除重复 Popen）

# dsh-py

用 **Python 一比一复刻 [dsh](https://github.com/deepseek-ai/dsh)**（DeepSeek Harness）的完整功能——让不懂 TypeScript 的开发者也能用上 dsh 的全套「全插件式」Agent 框架能力。

> **现状**：第 0/2/4 层全部完成；第 3 层支撑服务 **40 包代码已落地**——含 `code-runtime`/`sandbox`/`lsp`/`acp`/`tmux-context`/`e2b` 六包（按「seam + 本地后端/占位」范式翻译，均通过 `py_compile`，**暂未做运行时验证与单测**）、**`web` 家族**（web seam + 抓取/搜索 provider + `web_search`/`web_fetch` 工具）、**`skill` 家族**（`ctx.skills` 分层注册表 + filesystem/badge provider + `skill` 工具与目录/手势注入，先移植了 `dsh-scope` 子系统）、**`workspace`**（`ctx.workspaceRegistry` 耐久工作区注册表）、**`preset`**（`ctx.agentPresets` 预设注册表：发现/创作/常驻挂载 + `persona` 行，依赖 PyYAML）、**`interaction` 家族**（`ctx.commands` 命令注册表 + `ctx.approval` 批准服务 + `ctx.userQuestions` 提问 seam + `ctx.permissionPresets` 权限预设 + `ask_user_question` 工具）；web/skill/workspace/preset/interaction 均端到端冒烟已验证；**59 个测试模块全绿**（约 500+ 断言）；框架内核零第三方依赖。

---

## 目录

- [1. 项目定位](#1-项目定位)
- [2. 架构总览](#2-架构总览)
- [3. 安装](#3-安装)
- [4. 快速开始](#4-快速开始)
- [5. profile 机制（一切皆插件）](#5-profile-机制配置即插件清单一切皆插件)
- [6. SDK 编程式用法](#6-sdk-编程式用法)
- [7. 目录结构](#7-目录结构)
- [8. 复刻进度（逐包对照 dsh）](#8-复刻进度逐包对照-dsh)
- [9. 架构关键不变量](#9-架构关键不变量承重防回归)
- [10. 测试](#10-测试)
- [11. 与 dsh 的已知差异](#11-与-dsh-的已知差异)
- [12. TODO](#12-todo)

---

## 1. 项目定位

dsh_py 是把 dsh 的 **TypeScript 实现逐包翻译成 Python** 的忠实复刻：cordis 内核、Loader/Boot 装配、Session/Agent/LLM 三 seam、支撑服务、应用层全部对齐 dsh 的包结构与行为语义，仅在「脚本执行面」等无法跨语言的部分做了有文档的取舍（见 [§11](#11-与-dsh-的已知差异)）。

**特性总览**：

- **一切皆插件**：模型适配器、工具注册表、会话日志、甚至智能体循环（agent loop）本身都是 profile 插件，任何一部分都可以从配置替换，运行期也可 `ctx.agents.set_factory(...)` 热换
- **cordis 内核完整翻译**：Fiber 生命周期、作用域树（多会话隔离）、schema 校验、依赖拓扑 + 延迟就绪、内置 logger/reflect/registry、Loader/Boot 多 layer 装配、热重载 watcher
- **三 seam 完整版**：
  - *Session* — JSONL/SQLite+zstd 持久化、resume、checkpoint 崩溃恢复、projection/projection-cache/stats、session-query 完整检索
  - *Agent* — Inbox 双队列、cancel 三源融合（caller+fiber+factory）、声明式 agents、resume、有界并行工具执行
  - *LLM* — call-config 三层合并、retry 指数退避、api-key 解析链、attribution/brand 强制归属、适配器拓扑通知、多路由（7 厂商 OpenAI 兼容 + deepseek 官方 + pi-ai 通用）
- **支撑服务 40 包**：system-prompt 组装、tools 完整版（schema 校验 + 信号量并行）、subagent、settings+credentials、MCP 客户端桥接（stdio + streamable-http）、compaction 记忆压缩全套、guard 护栏、hooks 协议桥、schedule 定时器、todo、attachment、feedback、storage 多后端、spill、identity、jobs 后台任务、context 家族（time-context / session-reference / long-term-memory / agent-instructions）、goal 家族（事件溯源 + 三工具 + 续行驱动）、plan-mode、**workflow 编排引擎**（脚本解释执行 + workflow/ralph 两工具）、**subprocess 进程 seam**、typert 声明式远程调用、invariants 自检、code-runtime / sandbox / lsp / acp / tmux-context / e2b（seam + 本地后端/占位）、**web 家族**（`ctx.web` seam + HTTP 抓取 provider + DeepSeek/Exa/Perplexity 搜索 provider + `web_search`/`web_fetch` 工具）、**skill 家族**（`ctx.skills` 分层注册表 + filesystem/badge provider + `skill` 工具 / 会话目录 / `/name` 手势注入；依赖先移植的 **`dsh-scope` 子系统**）、**workspace**（`ctx.workspaceRegistry` 耐久工作区注册表：域数据形式 + 一次性历史引导 + 会话头部成员校验 + 归档集）、**preset**（`ctx.agentPresets` 预设注册表：发现/健康检查/创作（复制/删除）/常驻挂载 + agent 加入/重链 + `persona` 可组合行）、**interaction 家族**（`ctx.commands` 斜杠命令注册表（ScopedLayers 分层 + `command/run`↔`command/done` 生命周期）、`ctx.approval` 批准服务（策略折叠 + 审计对 + fail-closed）、`ctx.userQuestions` 提问 seam（唯一 provider + 存活/委托/意图校验）、`ctx.permissionPresets` 权限预设（沙箱/批准旋钮捆绑 + `permissions` 投影 + `/permission` 命令）、`ask_user_question` 工具）
- **应用层**：进程内 SDK（`DeepSeekHarness`）、跨进程 newline JSON-RPC SDK、WebSocket 常驻网关、交互式 CLI + headless 单任务模式

**依赖策略**：框架内核零第三方依赖；HTTP 类适配器（OpenAI 兼容 / DeepSeek / MCP streamable-http）懒加载 `httpx`；sqlite 后端可选 `zstandard` 压缩；Web 网关依赖 `websockets`（均仅应用层）。新依赖统一装入隔离 venv（`C:/Users/jdn/.workbuddy/binaries/python/envs/default`），引入时在模块 docstring 注明用途。

---

## 2. 架构总览

```
┌─ 第 4 层 应用层 ─────────────────────────────────────────────┐
│  cli.py（交互/headless/--jsonrpc）  sdk.py（进程内）          │
│  api/（跨进程 JSON-RPC over stdio）  gateway.py + websocket   │
├─ 第 3 层 支撑服务（40 包，全部插件装配）────────────────────────┤
│  system-prompt · tools · settings · credentials · subagent · │
│  mcp · compaction · fs/shell/terminal · guard · hooks ·      │
│  schedule · todo · attachment · feedback · util · storage ·  │
│  spill · identity · jobs · context家族 · goal家族 · plan ·    │
│  workflow（编排引擎） · subprocess（进程seam） · typert ·     │
│  invariants                                                   │
├─ 第 2 层 三 seam ────────────────────────────────────────────┤
│  Session（持久化/投影/查询） Agent（Inbox/取消/resume）       │
│  LLM（配置合并/重试/多路由适配器）                            │
├─ 第 1 层 Loader / Boot ──────────────────────────────────────┤
│  loader.py（profile 合成/拓扑排序） watcher.py（热重载）      │
│  config.py（统一配置三层深合并） env.py（.env 插值）          │
├─ 第 0 层 内核（cordis 翻译） ────────────────────────────────┤
│  context（作用域树/DI） events（waterfall 总线） fiber        │
│  schema（schemastery 子集） signal（取消） logger/reflect/    │
│  registry（内置服务）                                        │
└──────────────────────────────────────────────────────────────┘
```

**装配管线**：`boot` 叠加 bundle 层（`CORE_PROFILE`）→ 用户层（`configs/profile.py`）→ `--patch` overlays，插件按 `inject`/`provides` 依赖拓扑排序加载，可卸载（返回 `PluginHandle`，fiber dispose 自动回收资源）。

---

## 3. 安装

```bash
# 方式一：作为可编辑包安装（推荐，自动装 httpx + 提供 dsh-py 命令）
cd dsh-py 所在目录
pip install -e .

# 方式二：仅装运行依赖，直接用模块方式运行
pip install httpx
```

> 要求 Python ≥ 3.10。

---

## 4. 快速开始

### 4.1 离线演示（无需任何 key）

```bash
python -m dsh_py.cli --mock
# 或安装后：dsh-py --mock
```

输入消息即可看到全链路流式输出（mock 模型固定回复）。

### 4.2 接入真实模型（推荐：统一配置文件）

**key、模型参数、数据库、工作目录等集中写在配置文件，不依赖环境变量**。

默认配置在 `dsh_py/configs/dsh_config.py`（唯一配置编辑点，随仓库走）；个人机器级覆盖写在 `~/.dsh/dsh_config.py`（同结构，深合并覆盖，不进仓库）。

```python
# dsh_py/configs/dsh_config.py —— 编辑这里
CONFIG = {
    "llm": {
        "provider": "deepseek",          # 默认供应商（CLI 不传 --provider 时使用）
        "model": "deepseek-chat",        # 默认模型
        "api_key": "sk-xxxxx",           # 全局默认 key（明文，或 "${VAR}" 引用环境变量）
        "api_keys": {"openai": "sk-..."},  # 按供应商细分（多厂商场景）
        "temperature": 0.7,
        "max_tokens": 4096,
    },
    "database": {"url": "sqlite:///sessions.db"},  # SqliteSessionPersistence 后端已落地（含 zstd）
    "workdir": "~/.dsh/work",            # fs/shell/terminal 工具的默认工作目录
}
```

CLI 显式参数（`--provider` / `--model` / `--max-tokens`）优先于配置文件；`--config FILE` 可指定另一份配置文件（指定后不再合并默认两层）。

```bash
python -m dsh_py.cli                      # 直接用配置文件的 provider/model
python -m dsh_py.cli --provider deepseek --model deepseek-chat   # 命令行覆盖
python -m dsh_py.cli --config my_config.py                       # 自定义配置文件
```

**API key 解析优先级**：配置文件 `llm.api_key` / `llm.api_keys.<provider>` > credentials 服务 > 环境变量。所以下面这种传统方式仍然兼容：

```bash
export DEEPSEEK_API_KEY="sk-xxxxx"
python -m dsh_py.cli --provider deepseek --model deepseek-chat
```

内置 7 个厂商及对应环境变量兜底（OpenAI 兼容适配器）：

| provider   | 环境变量（兜底）    | 备注                                                                   |
| ---------- | ------------------- | ---------------------------------------------------------------------- |
| `openai`   | `OPENAI_API_KEY`    |                                                                        |
| `qwen`     | `DASHSCOPE_API_KEY` | 通义千问                                                               |
| `zhipu`    | `ZHIPU_API_KEY`     | 智谱 GLM                                                               |
| `moonshot` | `MOONSHOT_API_KEY`  | Kimi                                                                   |
| `deepseek` | `DEEPSEEK_API_KEY`  | DeepSeek 兼容                                                          |
| `ollama`   | （可空）            | 本地网关，默认`http://localhost:11434/v1`，可用 `OLLAMA_BASE_URL` 覆盖 |
| `vllm`     | （可空）            | 本地网关，默认`http://localhost:8000/v1`，可用 `VLLM_BASE_URL` 覆盖    |

未设置对应 key 的厂商不会真正发请求（缺 key 时抛 `MISSING_CREDENTIAL`）。

另有一个 **DeepSeek 官方专用适配器**（`llm-deepseek` 翻译，thinking/reasoning 协议），在 `configs/profile.py` 加一行后使用：

```python
"dsh_py.services.adapters.deepseek:apply",
```

```bash
# key 写进配置文件的 llm.api_key（deepseek-official 路由读这个字段）
python -m dsh_py.cli --provider deepseek-official --model deepseek-v4-pro
```

### 4.3 headless 单任务（对齐 `dsh --profile headless "task"`）

```bash
# 一条任务 → 创建（并持久化）新会话 → 打印最终回复 → 退出
python -m dsh_py.cli --mock --message "写个冒泡排序"
```

---

## 5. profile 机制（配置即插件清单，**一切皆插件**）

**装配点唯一**：默认运行与自定义都指向同一个 profile 文件（`dsh_py/configs/profile.py`）。自定义组件 = **直接编辑这个文件**，无需新建第二个 profile；CLI 每次启动都加载它。`--profile` 仅在你确实想换成别的装配文件时才需要。

profile 是一个 `.py` 文件，导出 `PROFILE` 列表，元素可以是：

- **字符串** `"dsh_py.plugins.long_term_memory"` —— 模块名，调用其 `apply(ctx, config)`
- **字符串** `"dsh_py.services.agent:apply_loop"` —— `模块:属性`，精准指向某个插件入口
- **可调用** `apply(ctx, config)` 函数（插件）
- **字典** `{"plugin": "模块:属性", "config": {...}}` 或 `{"apply": fn, "config": {...}}` —— 带配置的插件行；`{"id": "x", ...}` 形式带 id，供上层 layer 以 patch 指令定位覆盖

**核心 seam 也是插件**（不硬编码装配）：`llm` / `sessions` / `tools` / `agents` 注册表 / `agentLoop`（默认智能体循环）全部作为 profile 条目按序加载，**每一部分都可以从配置替换**。核心清单由 `CORE_PROFILE`（bundle 层）提供，`boot` 管线自动叠加：bundle 层 → 用户层（`configs/profile.py`）→ `--patch` overlays。

编辑 `configs/profile.py` 示例（换掉默认循环 + 系统指令注入 + MCP 服务器）：

```python
PROFILE = [
    # 业务插件（bundle 核心由 boot 自动叠加）
    (apply_instructions, {"instructions": "你是一个简洁、严谨的中文助手。"}),
    {"plugin": "dsh_py.services.adapters.deepseek:apply"},          # DeepSeek 官方适配器
    {"plugin": "dsh_py.plugins.mcp_client:apply", "config": {       # MCP 服务器工具
        "transport": "stdio", "serverName": "files",
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    }},
]
```

> 运行时换循环也无需重启装配：`ctx.agents.set_factory(你的实现)` 即完成替换，所有调用方都经 `ctx.agents.create_agent`，不感知具体实现。同理，`sessions` / `tools` / `llm` 也可提供同名服务的插件整体替换。

改完直接运行（自动加载的就是这个文件）：

```bash
python -m dsh_py.cli --provider deepseek --model deepseek-chat
```

---

## 6. SDK 编程式用法

### 6.1 进程内 SDK（对标 dsh 的 `@deepseek-ai/dsh-sdk`）

```python
import asyncio
from dsh_py.sdk import DeepSeekHarness

async def demo():
    harness = DeepSeekHarness()                       # 默认装配 configs/profile.py
    result = await harness.run(
        "用一句话解释什么是 Agent。",
        {"provider": "deepseek", "model": "deepseek-chat"},
    )
    print(result.final_response)                      # 最终 assistant 文本
    await harness.close()                             # 卸载全部插件

asyncio.run(demo())
```

底层等价写法（直接操作上下文）：

```python
import asyncio
from dsh_py.core.context import AppContext
from dsh_py.loader import load_profile, CORE_PROFILE
from dsh_py.services.agent import AgentOptions

async def demo():
    ctx = AppContext()
    load_profile(ctx, [*CORE_PROFILE, "dsh_py.plugins.long_term_memory"])

    session = ctx.sessions.create()
    agent = ctx.agents.create_agent(                  # 经注册表创建（循环可替换）
        session, AgentOptions(provider="deepseek", model="deepseek-chat"))

    @ctx.on("session/event")
    def on_event(_s, event):                          # 流式分块
        if event.type == "assistant/chunk":
            c = event.data["chunk"]
            if c.type == "text-delta" and c.text:
                print(c.text, end="", flush=True)

    await agent.run("你好")

asyncio.run(demo())
```

### 6.2 跨进程 SDK（对标 dsh 的 `sdk-jsonrpc-server` + `dsh-sdk-client`）

同名同 API 的**子进程版**：`dsh_py.api.DeepSeekHarness` 拉起一个 `python -m dsh_py.cli --jsonrpc` 运行时子进程，走 newline JSON-RPC 通信——进程外/跨语言客户端可用同一协议驱动 harness（wire 定义在 `dsh_py/api/protocol.py`）。

```python
import asyncio
from dsh_py.api import DeepSeekHarness

async def demo():
    harness = DeepSeekHarness(provider="deepseek-official", model="deepseek-v4-flash")
    try:
        session = harness.session()                 # 缺省自动生成 session-<uuid>
        result = await session.run("用一句话解释什么是 Agent。")
        print(result.final_response)                # 最终 assistant 文本
    finally:
        await harness.close()                       # shutdown → terminate → kill 阶梯

asyncio.run(demo())
```

**协议面**（与 dsh 的 `@deepseek-ai/dsh-sdk-protocol` 一致）：

| 方向 | 方法             | 说明                                                                                                        |
| ---- | ---------------- | ----------------------------------------------------------------------------------------------------------- |
| 请求 | `initialize`     | 握手：cwd / provider / model / maxTokens（provider 无适配器且为 deepseek-official 时兜底挂载 llm-deepseek） |
| 请求 | `session/prompt` | 按 sessionId 取/建会话，投递 contentBlocks，返回`messageId`                                                 |
| 请求 | `shutdown`       | 幂等关闭：退订事件、取消会话 agent、卸载兜底插件                                                            |
| 通知 | `session.event`  | 会话日志事件（含`agent/inbox/spliced` 回执）                                                                |
| 通知 | `session.status` | agent 生命周期`running` / `idle`（客户端靠它判定一轮 run 结束）                                             |

服务器也可直接跑：`python -m dsh_py.cli --jsonrpc --mock`（stdout 仅协议帧）。

### 6.3 Web 网关（常驻后端服务，对标 dsh 的 `api/gateway`）

把 harness 变成**网络服务**：`python -m dsh_py.gateway --port 8080` 起一个常驻 WebSocket 服务器，远程客户端（网页 / App / 任何语言）连接 `ws://localhost:8080`，说**同一套 JSON-RPC**（方法面与 6.2 完全一致，传输从 stdio 换成 WebSocket 帧）：

```bash
python -m dsh_py.gateway --port 8080 --mock    # 离线演示
python -m dsh_py.gateway --port 8080 --host 0.0.0.0   # 开放局域网
```

```python
# 任意客户端：initialize → session/prompt → 收 session.event/status → shutdown
import asyncio, json, websockets

async def demo():
    async with websockets.connect("ws://127.0.0.1:8080") as ws:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": "1", "method": "initialize",
                                  "params": {"cwd": ".", "provider": "deepseek-official"}}))
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": "2", "method": "session/prompt",
                                  "params": {"sessionId": "s1",
                                             "contentBlocks": [{"type": "text", "text": "你好"}]}}))
        while True:  # 服务器推送：session.event / session.status（idle 结束）
            frame = json.loads(await ws.recv())
            if frame.get("method") == "session.status" and frame["params"]["status"] == "idle":
                break

asyncio.run(demo())
```

**多连接共享**：每个连接独立订阅，事件广播到全部连接（共享 harness 实例）。依赖 `websockets`（仅应用层；框架本体仍零依赖）。

---

## 7. 目录结构

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
├─ gateway.py                    常驻 WebSocket 网关入口（--port/--mock/--host）
├─ sdk.py                        DeepSeekHarness / HarnessSession / RunResult（进程内）
├─ cli.py                        --profile/--patch/--config/--provider/--model/--system/
│                                 --mock/--message/--jsonrpc
├─ configs/
│   profile.py                   唯一装配点（用户层）
│   dsh_config.py                唯一配置编辑点（key/参数/路径）
└─ tests/                        59 个测试模块（纯 assert 脚本，无需 pytest）
```

---

## 8. 复刻进度（逐包对照 dsh）

### 8.1 分层状态

| 层 | 内容 | 状态 |
| --- | --- | --- |
| **第 0 层 · 内核** | Fiber 状态机 + effect / 作用域树（isolate 多会话隔离）/ schema 校验 / inject 拓扑 + 延迟就绪 / 内置 logger·reflect·registry / Loader-Boot（多 layer + env + 热重载） | ✅ 完成 |
| **第 1 层 · Loader/Boot** | 多 layer profile 合并、`--patch` overlay、schema 校验 + 拓扑排序、可卸载插件、热重载 watcher、环境变量插值 | ✅ 完成 |
| **第 2 层 · 三 seam** | Session（JSONL + SQLite/zstd + resume + checkpoint + projection/query 完整版）/ Agent（Inbox / cancel 三源 / 声明式 / resume）/ LLM（call-config / retry / api-key / attribution / topology / 多路由适配器） | ✅ 完成 |
| **第 3 层 · 支撑服务** | 40 个功能包（见 8.2 清单）；六包按「seam + 本地后端/占位」翻译、代码已落地、**未经运行时验证与单测**（见 8.3）；`web`/`skill`/`workspace`/`preset`/`interaction` 已冒烟验证 | ✅ 40 包（代码落地）|
| **第 4 层 · 应用层** | 进程内 SDK / 跨进程 JSON-RPC SDK / WebSocket 网关 / headless + 交互 CLI / `--jsonrpc` 子进程模式 | ✅ 完成（`acp-agent`/`host`/跨语言 client 完整版未做） |

### 8.2 第 3 层已落地 40 包清单

| # | 包 | 实现位置 | 要点 |
| --- | --- | --- | --- |
| 1 | `system-prompt` | `services/system_prompt.py` | sections/variables/renderPrompt，严格 `{{var}}` 插值 |
| 2 | `tools` | `services/tools.py` | JSON Schema 执行前校验、有界信号量并行 + 顺序回填、错误文本回流 |
| 3 | `settings` | `services/settings.py` | 作用域 get/set/watch + schema 校验，运行时改 maxParallelToolCalls 即时生效 |
| 4 | `credentials` | `services/credentials.py` | credential_ref + resolve/set/delete/describe + `credentials/updated` |
| 5 | `subagent` | `plugins/subagent.py` | 子会话 + 子 agent 执行、max_depth 限制 |
| 6 | `mcp` | `plugins/mcp_client/` | stdio + streamable-http 双传输、重连监督、工具同步 |
| 7 | `compaction` | `services/compaction*.py` + `tool_result_pruner` | 压力/溢出双触发、锁事务、摘要收敛、自动挂钩、`/compact` |
| 8 | `fs` | `services/fs.py` + `plugins/tool_fs.py` | 行窗口读/原子写/字面替换 read/write/edit 工具 |
| 9 | `shell` | `services/shell.py` + `plugins/tool_bash.py` | 命令执行（**已接线 ctx.subprocess seam**）、bash 工具 + run_in_background |
| 10 | `terminal` | `services/terminal.py` + `plugins/tool_terminal.py` | 持久 Popen 会话（非 PTY，差异注明） |
| 11 | `guard` | `plugins/guard_repeat_tool.py` + `guard_timeout.py` | 重复调用提醒 + 工具超时强制 |
| 12 | `hooks` | `services/hooks_protocol.py` + `plugins/hooks.py` | 与方言无关的匹配/解码/合并 + 三拦截点通用桥 |
| 13 | `schedule` | `services/schedule*.py` | 域纯函数 + 实时计时器投影 + 三 agent 工具 |
| 14 | `todo` | `plugins/tool_todo.py` | 整表替换工具 + todos 投影单元 last-write-wins |
| 15 | `attachment` | `services/attachment*.py` | seam + 零依赖四格式头解析 + 内容寻址本地存储 |
| 16 | `feedback` | `services/message_feedback.py` + `plugins/command_feedback.py` | CAS 版本语义 + typert 远程作用域 |
| 17 | `util` | `util/` 家族 7 原语 | brand/timeout/atomic-write/home-paths/output-retention/native-command/launch-environment |
| 18 | `runtime-diagnostics` | `services/invariants.py` | invariants 自检框架（注册 + 检查 + fail） |
| 19 | `storage` | `services/storage*.py` | hub + JSON/SQLite 双后端 + domain 层 |
| 20 | `spill` | `services/spill*.py` + `plugins/spill_policy.py` | seam + 本地后端 + post-execute 策略 |
| 21 | `identity` | `services/anonymous_user_id.py` | anonymous-user-id（attribution 前置） |
| 22 | `jobs` | `services/jobs*.py` + `plugins/tool_jobs.py` | 后台任务 + 三工具；真实 bash 任务经 ctx.subprocess |
| 23 | `context` 家族 | `plugins/time_context.py` / `services/session_reference.py` / `plugins/long_term_memory.py` / `services/agent_instructions/` | time-context + session-reference + long-term-memory + agent-instructions |
| 24 | `goal` | `services/goal*.py` + `plugins/tool_goal.py` + `command_goal.py` | 事件溯源域 + 服务 + 三工具 + `/goal` 命令 |
| 25 | `plan` | `services/plan_mode.py` | plan-mode 协作状态 |
| 26 | `workflow` | `services/workflow/` + `plugins/tool_workflow.py` + `tool_ralph.py` | 编排引擎（**Python 脚本 re-target** 解释执行）+ workflow/ralph 两工具 |
| 27 | `subprocess` | `services/subprocess.py` + `subprocess_local.py` | 进程执行 seam + 树级本地实现（killpg/taskkill） |
| 28 | `goal-round-driver` | `services/goal_round_driver.py` | 同会话自动续行驱动（goal 家族闭环） |
| 29 | `typert` | `services/typert.py` | @remote/@remote_scope 声明式远程调用 |
| 30 | `code-runtime` | `services/code_runtime.py` + `code_runtime_local.py` | seam + `multiprocessing` 进程隔离后端（替 worker_threads）+ RPC 管道承载 host bindings；错误作结果字段非拒绝 |
| 31 | `sandbox` | `services/sandbox.py` + `sandbox_local.py` + `sandbox_policy.py` | seam + bwrap/seatbelt/Windows-ACL 占位（fail-closed）+ 会话模式折叠 `effectiveSandboxMode` |
| 32 | `lsp` | `services/lsp.py` + `lsp_stdio.py` + `plugins/tool_lsp.py` | seam + Content-Length JSON-RPC stdio 后端 + `lsp` 工具（1 基→0 基 UTF-16 光标） |
| 33 | `acp` | `services/acp.py` | ACP stdin/stdout newline JSON-RPC 协议服务端（initialize/authenticate/sessions.new/prompt/cancel + 会话/agent 事件桥） |
| 34 | `tmux-context` | `plugins/tmux_context.py` | 经 `ctx.shell.execute()` 跑 `tmux display-message` 捕获面板位置（$TMUX_PANE + controlling-tty 双重校验、变化抑制；Windows 无 tmux 但代码完整） |
| 35 | `e2b` | `services/e2b.py` | seam + 占位 provider（惰性 `get_sandbox()` 首次就绪 + 超时/销毁回收；SDK 延迟导入，缺 SDK/账号仅在使用时报错） |
| 36 | `web` 家族 | `services/web.py` + `web_fetch_http.py` + `web_search_deepseek.py` + `web_search_exa.py` + `web_search_perplexity.py` + `plugins/tool_web.py` | `ctx.web` seam（fetch/search 共 seam、执行期选择、`maxResults` 封顶）+ HTTP 抓取 provider（同源重定向、字节/字符/超时上限）+ 三搜索 provider（缺 key 仅不可用）+ `web_search`/`web_fetch` 工具（HTML 深度守卫 512、markdownify 懒加载、截断脚注） |
| 37 | `skill` 家族 | `services/scope.py`（**dsh-scope 移植**）+ `services/skill.py` + `skill_filesystem.py` + `skill_watch.py` + `skill_badge.py` + `plugins/tool_skill.py` | `ctx.skills` 分层注册表（provider 合并/rank 裁决/collect 缓存/`skills/change` 通知）+ filesystem provider（pathlib 发现 + frontmatter 解析 + 可选 watchdog）+ 内置 dsh-badge + `skill` 工具（会话目录 `skill-catalog` 来源 + `/name` 手势 `skill-invocation` 注入） |
| 38 | `workspace` | `services/workspace.py` + `workspace_registry.py` | `ctx.workspaceRegistry` 耐久注册表：`workspace` 域（workspaces 表 + global 状态）+ 一次性历史引导（按规范 cwd 分组会话、最晚活动优先）+ create（canonical 路径复用/非目录拒绝）/delete（保留目录与日志）/insert_before 排序/归档集 + 实体（attach 头部 cwd 校验、哨兵中止 no-op 写、status） |
| 39 | `preset` | `services/agent_presets/`（7 模块）+ `plugins/persona.py` | `ctx.agentPresets`：发现（每调用重读根 + 组合健康检查：缺组合/坏 YAML → broken 行）+ 创作（user 根限定整目录复制/删除/读取）+ 常驻挂载（单飞 + mtime/size 戳换代 + 加入 = scope 父链绑定/重链）+ `resolve_session_preset` + `persona` 可组合行（scope-only 遮蔽部署人格） |
| 40 | `interaction` 家族 | `services/commands.py` + `user_approval.py` + `user_questions.py` + `permission_presets.py` + `plugins/tool_ask_user.py` | `ctx.commands` 斜杠命令注册表（ScopedLayers 分层遮蔽 + `parseCommand` + execute 的 `command/run`↔`command/done` 生命周期 + `commands/change` 通知）+ `ctx.approval` 批准服务（`approval/policy` 折叠 + `approval/asked`↔`approval/decided` 审计对 + never 确定性拒绝/fail-closed/信号撤回 + systemPrompt policy 片段）+ `ctx.userQuestions` 提问 seam（唯一 provider + CALLER_NOT_LIVE/DELEGATED_CALLER/BAD_INTENT 校验）+ `ctx.permissionPresets` 权限预设（沙箱/批准旋钮捆绑 + `permissions` 投影单元 + `/permission` 命令 + 会话初始钉定）+ `ask_user_question` 工具 |

### 8.3 新落地六包（代码已写，**未做运行时验证 / 单测**）

> **决策背景（2026-08-27）**：此前误判 `code-runtime`/`sandbox`/`lsp`/`acp`/`tmux-context`「依赖外部基础设施不可写」。经核对 dsh 源码确认——它们与 `process`/`shell`/`fs` 同构，可按既有「seam + 本地后端 + 占位」范式落地，**只有 `e2b` 真正需要账号与 SDK**（已写 seam + 占位 provider）。用户拍板：**「遵照 dsh 源码，先把代码写出来，能不能跑先不管」**。

据此六包已按 dsh 对应 TS 模块逐函数翻译为 Python，**全部通过 `py_compile`**，但**尚未做 mock 单测与运行时验证**（即不保证端到端可跑，仅保证语法/类型与结构对齐）。验收前需补的测试见 §12。

### 8.4 收尾 / 未做（真正依赖外部集成）

| 包 / 项 | 原因 |
| --- | --- |
| `e2b` 适配器 `fs-e2b` / `subprocess-e2b` | 需 e2b 账号 + 官方 SDK，仅占位 seam 已落地 |
| `acp-agent`（ACP 协议入口）/ 跨语言 client 完整版 | 依赖真实客户端 SDK 与 typert 跨进程成熟度 |
| 第 4 层 `host`（聚合宿主） | 应用层收尾集成 |
| 六包 mock 单测 | 用户拍板暂缓（仅 `py_compile` 校验，未补 `test_*`） |

---

## 9. 架构关键不变量（承重，防回归）

> 改任何包前先读本节。这些是 dsh_py 与 dsh 对齐过程中踩坑沉淀的行为契约，破坏任一都会引发深层回归。

- **waterfall 监听器统一 `async`**：pre-step 类 `await next()` 取默认决策；stream 类 `async for chunk in next()` 包流；`inner` 返回协程/asyncgen/值原样透传。
- **创建 Agent 一律 `ctx.agents.create_agent`**（注册表 + 工厂可替换）；无循环时抛 `RuntimeError` 提示 `apply_loop`。
- **作用域树**：服务沿「当前→父」解析，`isolate`（label 变化）即阻断；事件总线全局共享 + 祖先链收集 + `global_` 恒可见；子 ctx `dispose()` 回收本作用域资源。
- **Session 持久化**：`JsonlSessionPersistence`（首行 header / torn 尾行丢弃 / 版本 fail loud）；`resume` 无后端或不存在明确报错。
- **LLM**：`call_config` 三层合并、`retry` 策略、`normalize_api_key`（trim + 可打印 ASCII 白名单）、本地拒非法/缺 key。
- **API key 解析优先级**：配置文件 > credentials seam > 环境变量。
- **服务名带连字符**（如 `appConfig`）须 `ctx.provide("appConfig", ...)` 注入，不可用属性访问。
- **projection 全值事件规则**：携带状态的日志事件必须携带完整后状态（绝不只增量）；单元 `apply` 不关心的事件必须返回同一引用（`is` 门控变更通知 → 零下游）。
- **投影 schema 用 `core/schema` 的 `validate`**（非 `parse`）；`ProjectionDefinition.schema=None` 表示透传；`snapshot.as_of_seq = session.seq - 1`。
- **冷读阶梯**：`restore_floor` 取所有注册单元的最低起点（任一单元无行会把 floor 拉到 0 → 全量重折）；`restore` 在 `base_seq>0` 且行不可用时抛错。
- **取消三源融合**：`CancelSignal.any([caller, fiber, factory])`（对齐 `AbortSignal.any`）；`AgentLoop` 持 `_teardown` 工厂信号，`resume`/`create_agent` 经 `Agent` 构造的 `signal` 参数融合。
- **拓扑通知 contained**：`ctx.events.dispatch(name, *args)` 逐个隔离监听器异常（注册表通知非否决），如 `llm/adapters-updated`。
- **工具 handler 契约**：handler 必须返回 `(text, is_error)` 二元组（错误文本回流；缺 required 参数经 schema 校验回流）。
- **Session surface**：表面 seq 从 1 起，取事件须 `events[seq-1]`；surface replace 用 `append(..., surface_op={"op":"replace",start,end})`，替换后 seq 不再单调。
- **compaction 事务**：`compaction/start` 是持久锁；失败路径恰好一次 `compaction/end`（带 error）；摘要帧估算必须 < 被遮蔽内容（收敛校验）；assistant 工具调用节点后/工具结果前的切口本就应不平衡（配对语义）。
- **session-query**：`ctx.sessions.list()` 返回 **id 列表**（str）非 Session 对象（遍历须 `get(id)`）；结构性事件 text 为空不进检索文档；`SessionHeader.parent_session` 供谱系追踪。
- **tools schema 的 object 节点 `required` 必须是列表**，逐属性 `required: True` 会让转换器崩溃（bool 不可迭代）。
- **workflow**：`WorkflowStartRequest` 传 dict 须在 `start()` 归一化；`emit_workflow_event` 用 `_listeners` 手动逐例遍历 + InvariantError 重抛。
- **subprocess**：`spawn` 同步返回句柄（pid -1）由后台任务接线；`wait_for` 超时会取消底层 future，须 `asyncio.shield(handle.done)` 再 terminate；jobs 注册表按**属性**访问 hooks（`SimpleNamespace` 而非 dict）。
- **goal-round-driver**：活 agent 注册表在 `ctx.agentLoop.get/roots`（非 `ctx.agents`）；`Session` 无 `.id` → `session.header.id`；`followup` 会立即触发 `_drain`（瞬时 mock 下 armed 目标自动级联到上限是正确行为）。
- **跨进程 SDK**：stdin 读必须用 daemon 线程，否则 shutdown 后客户端不关 stdin 会挂死进程。
- **websockets ≥17**：`serve` 回调单参数 `async def handler(ws)`（移除 path 参数）。

---

## 10. 测试

纯 `assert` 脚本（无需 pytest），用项目隔离 venv 的 python 直接跑：

```bash
cd dsh_py 所在目录
for t in dsh_py/tests/test_*.py; do python "$t"; done
# 或单跑某个：python dsh_py/tests/test_workflow.py
```

**59 个测试模块**（约 500+ 断言）按层分组：

| 分组 | 模块 |
| --- | --- |
| **第 0 层 内核**（8） | `test_core` `test_fiber` `test_scope` `test_schema` `test_boot` `test_inject` `test_builtin` `test_util` |
| **第 2 层 三 seam**（12） | `test_llm` `test_llm_full` `test_adapter` `test_adapter_http` `test_adapter_deepseek` `test_llm_pi_ai` `test_session_persistence` `test_session_sqlite` `test_session_query_full` `test_agent` `test_agent_extended` `test_layer2_complete` |
| **第 3 层 支撑服务**（34） | `test_system_prompt` `test_tools_full` `test_settings_credentials` `test_subagent` `test_mcp_client` `test_mcp_streamable_http` `test_compaction` `test_tool_result_pruner` `test_command_compact` `test_tool_fs_shell_terminal` `test_guard_hooks_schedule` `test_hooks_protocol` `test_schedule_domain` `test_tool_todo` `test_attachment` `test_feedback` `test_invariants` `test_storage` `test_spill` `test_identity` `test_jobs` `test_time_context` `test_session_reference` `test_long_term_memory` `test_goal` `test_agent_instructions` `test_plan_mode` `test_workflow` `test_tool_workflow` `test_tool_ralph` `test_subprocess` `test_subprocess_integration` `test_goal_round_driver` `test_typert` |
| **第 4 层 应用层**（4） | `test_sdk` `test_api_sdk` `test_gateway` `test_config` |
| **示例插件**（1） | `test_plugins` |

---

## 11. 与 dsh 的已知差异

> 均为有文档的取舍，不影响 1:1 对齐的包结构与行为语义；每个差异在对应模块 docstring / 复刻计划文档中有完整说明。

| 领域 | dsh（TypeScript） | dsh_py（Python） |
| --- | --- | --- |
| **workflow 脚本语言** | node:vm 执行模型写的 **JavaScript** | `exec` 在注入 async hook 的命名空间执行 **Python 脚本**（引擎/API/事件/组合子 1:1，仅脚本面语义重定向） |
| **workflow 引擎隔离** | worker_threads 隔离线程，可 terminate() 强杀 + syncTimeoutMs | 进程内 asyncio **内联引擎**（折叠 host/session/worker；无强杀与同步超时，停泊脚本由 grace 强制终止；`syncTimeoutMs` 仅配置占位） |
| **结构化输出** | LLM 层 response_format 原生支持 | 文本→**JSON 提取兜底**（如 workflow 子 agent、ralph report） |
| **terminal / subprocess 终端** | node-pty 真实 PTY（前台组/信号） | **非 PTY 近似**（持久 Popen 会话，前台组检查 None，前台发信号退化为直接子进程） |
| **spawn 同步性** | Node `spawn` 同步返回 | asyncio 创建异步 → 句柄先同步返回（pid -1）后台任务接线 |
| **长记忆** | 语义向量检索 | 关键词召回原型（JSONL），非语义检索 |
| **token 估算** | 模型真实 tokenizer | 启发式（CJK 逐字 + 4 字符/token），compaction 收敛校验以该估算为准 |
| **session-query 检索** | 全文索引/嵌入基础设施 | 会话内存倒排索引（字母数字词 + CJK 单字）；`readTitle*` 未实现 |
| **attachment 图像** | sharp 全解码 | 结构级四格式头解析（零依赖） |
| **feedback 存储** | storage-domain | 零依赖 JSON KV 表 |
| **workflow 内部事件** | `internal/dispatch` 拦截 | 改为公开事件 + InvariantError 响亮传播 |
| **goal-round-driver** | `ctx.agents.get/list`、`withoutInitiator`、`agent/error` | 用 `ctx.agentLoop.get/roots` + `agent/status` 事件跟踪；无 initiator 概念省略对应钩子 |
| **Windows 环境** | — | 子进程类测试退出期可能有良性管道 GC 噪声（`test_mcp_client` 等，退出码 0） |
| **code-runtime 隔离** | Node `worker_threads` 隔离线程 + RPC 管道 | `multiprocessing` 进程隔离（替 worker_threads）；失败分类（exception/timeout/abort/worker-exit/invalid-output/output-limit）作结果字段非拒绝 |
| **sandbox 后端** | bwrap/landlock（Linux）/ seatbelt（macOS） | 同构后端占位 + Windows-ACL 占位，fail-closed（需真实 sandbox 二进制才生效） |
| **lsp 光标坐标** | 1 基 UTF-16 光标 | 模型侧工具 1 基→0 基转换，内部零基 UTF-16 |
| **acp 协议** | typert 远程作用域 | stdio newline JSON-RPC 服务端（方法面与 dsh 一致）；事件桥接复用本地 `ctx` 事件总线 |
| **tmux-context** | dsh 用 `ctx.shell.resolve/run` | 改用本地 `ctx.shell.execute()`（返回 `{exit_code, stdout}`）+ `ctx.on("agent/pre-step", prepend=True)` + `MessageSource(kind="plugin", plugin="tmux-context")` |
| **e2b 就绪时机** | 同步打开沙箱客户端 | 惰性 `get_sandbox()` 首次创建就绪 future（避免同步构造触碰事件循环）；SDK 延迟导入 |
| **web 传输** | 浏览器 `fetch` + `TextDecoder` | 懒加载 `httpx.AsyncClient`（超时/重定向/尺寸上限语义对齐）；`bytes.decode` 承担解码（未知 charset → `WEB_UNSUPPORTED_CONTENT_TYPE`） |
| **web 取消** | `AbortSignal` 事件竞速 | 检查点 `throw_if_aborted` + `asyncio.CancelledError` → `WEB_ABORTED`（fetch 用 `asyncio.timeout` 区分 `WEB_FETCH_TIMEOUT`） |
| **tool-web 展示层** | `defineTool` output schema / render / presentationMeta / presentCall / presentResult（UI 卡片） | dsh_py 工具契约无展示层：handler 直接返回模型可见文本（`formatSearchOutput` / `formatFetchOutput`），卡片/meta 省略 |
| **HTML→Markdown** | turndown + GFM 插件（含防 colspan 表规则） | 懒加载 `markdownify`（缺依赖/转换失败降级原样返回）；词法深度守卫 512 1:1 移植 |
| **DeepSeek 搜索请求记录** | `agents.currentInitiator()?.session` | dsh_py 无 initiator 概念：写入首个活跃根 agent 的会话（无则跳过，观测性） |
| **settings 区 schema** | schemastery `Config` 默认值 | `install_settings_section` 沿用，schema 置 None（默认值在 apply 侧补全） |
| **scope 子系统** | Cordis `ctx.plugin` + `ctx.extend({symbol})` 铸造作用域、`scopeTarget` 事件载波 | `create_scope` = `ctx.extend()` + 实例属性打标；`scopeTarget`/`carrierKeyOf`（事件层专用）未移植；`ScopedLayers.effect` 改为「立即 action + fiber 清理 + 立即 notify」 |
| **skill 取消** | `AbortSignal` 事件竞速（`waitWithAbort`） | 检查点 `throw_if_aborted`（无 promise 竞速，文档化） |
| **skill frontmatter** | `yaml` 包完整解析 | 优先懒加载 `yaml`，缺失/失败回退极简映射解析器（扁平标量 + 内联 metadata，零依赖妥协） |
| **skill 监视** | chokidar（祖先模式/双探针/稳定阈值） | 懒加载 `watchdog`（目录已存在才监视，任意事件触发失效）；缺依赖降级不监视；默认 `watch=False`；dsh_py 无 `fs/observed` 事件，宿主变更观测 no-op |
| **tool-skill 目录/手势** | `defineTool` 输出 schema + `MessageSourceMap` 合并 | dsh_py 工具契约无输出 schema（handler 返回 `render_skill_content` 文本）；`MessageSource` 扩展 `name`/`entries`/`update` 字段承载 `skill-invocation`/`skill-catalog` 来源；`tools.get(name, agent)` 身份比较 → `tools.has(name)` |
| **workspace 生命周期** | `Service.init` 异步初始化钩子 | 显式 `async start()`（插件 apply 以 `create_task` 调度）；未就绪前 `require_*` 抛错、异步入口 `await _when_ready()` |
| **workspace 路径规范** | `fs.realpath`（缺失抛 ENOENT） | `os.path.realpath` 从不抛——`create` 补存在性检查以对齐「原错误拒绝」 |
| **workspace 会话索引** | `sessions.list()` 返回 Session 对象 | dsh_py 返回 **id 列表**——逐个 `get(id)` 取 header |
| **workspace 表更新 no-op** | fn 原样返回 current 触发哨兵中止写槽 | dsh_py `update` 无条件写——以**哨兵异常**中止（不落盘不事件，调用方捕获吞掉） |
| **preset 组合装载** | cordis `Include`/`EntryTree` + 未激活行/泄漏服务审计 | dsh_py `load_profile` 直接装载（缺失 inject 即整体拒绝——审计由 loader 承担）；无 isolate 领域 → 泄漏审计天然满足；组合行 `{name, config}` → `{"plugin", config}`，group 行扁平化 |
| **preset 事件/告警** | `agent/created` 告警监听 + reflect.store 服务读取 | dsh_py 无 `agent/created`（省略告警）；`service_for` 退化在 agent ctx 链上尽力解析 |
| **preset 注册表可见性** | scope 父链 + per-fiber 服务存储，常驻挂载注册对加入的 agent 可见 | dsh_py 工具/提示/技能注册表全局——预设行注册即全 agent 可见；预设服务落在常驻作用域 ctx（沿父链可见） |
| **preset 依赖** | js-yaml | 懒加载 **PyYAML**（已装入隔离 venv）；缺失时组合判 broken 并注明 |
| **commands 远程面** | `TypertRemoteService` + `@Remote`（list/execute） | 继承 `Service` + `@remote` 打标（dsh_py typert 为独立注册表 + 元数据装饰器）；Wire 形态不变 |
| **commands 通知** | `events.dispatch` 逐个隔离 + promise 捕获 | `ctx.emit("commands/change")`（dsh_py 事件分发本就逐个隔离，非否决） |
| **approval 分发** | `scopeTarget(this, req.agent)` 作用域过滤瀑布流 | dsh_py 无 scope 事件载波 → 普通 `ctx.waterfall("approval/request", ...)`，监听器按 `req.agent` 属性自行过滤 |
| **approval 消息注入** | `agent.inject(createUserMessage(...))` | `agent.session.append("user/message", create_user_message(...))`（无 inject 入口） |
| **permission-presets 沙箱默认** | `ctx.shell.sandboxMode`（executor 能力）+ 静态 `inject=['shell','approval','sessions']` | dsh_py shell 无该属性 → `Config.sandboxDefault`（默认 workspace-write）；装配处仍 fail-loud 检查三服务 |
| **permission-presets 初始钉定** | `session/created` 事件监听 + 遍历既有 | dsh_py 无 `session/created` → 构造时遍历 + 公开 `pinInitialPermission` 供装配方调用 |

**依赖**：`pyproject.toml` 仅声明 `httpx` 为必需依赖；`zstandard`（sqlite 可选压缩）、`websockets`（Web 网关）、`PyYAML`（agent-presets 组合解析，懒加载）为应用层依赖；测试依赖（`pytest`）放在可选 `dev` 分组（测试本体不依赖 pytest）。

---

## 12. TODO

### 近期

- [~] 第 3 层剩余六包代码已按 dsh 源码落地（`code-runtime`/`sandbox`/`lsp`/`acp`/`tmux-context`/`e2b`），均通过 `py_compile`，**未做运行时验证与单测**（用户拍板「先把代码写出来，能不能跑先不管」）
- [X] **`web` 家族落地**（`ctx.web` seam + web-fetch-http + DeepSeek/Exa/Perplexity 搜索 + tool-web 两工具）：纯函数 42 项冒烟 + 装配（拓扑排序/工具注册）已验证；传输/搜索需 `httpx` 与对应 API key
- [X] **`skill` 家族落地**（先移植 `dsh-scope` 子系统 → `ctx.skills` 分层注册表 + filesystem/badge provider + tool-skill 工具/目录/手势）：纯函数 19 项 + 装配 6 项冒烟已验证；frontmatter 优先懒 yaml、监视需可选 `watchdog`
- [X] **`workspace` 落地**（`ctx.workspaceRegistry` 耐久工作区注册表：workspace 域 + 历史引导 + 实体成员校验）：端到端 31 项冒烟已验证（fake storageDomain/persistence 环境）
- [X] **`preset` 落地**（`ctx.agentPresets` 预设注册表 + `persona` 行：发现/健康/创作/常驻挂载/加入与重链/会话解析）：端到端 28 项冒烟已验证；组合解析依赖 **PyYAML**（已装入隔离 venv）
- [X] **`interaction` 家族落地**（`ctx.commands` 命令注册表 + `ctx.approval` 批准服务 + `ctx.userQuestions` 提问 seam + `ctx.permissionPresets` 权限预设 + `ask_user_question` 工具）：纯函数 + 端到端 **90 项冒烟已验证**（含 lifecycle 审计对、fail-closed、策略折叠、投影/命令装配）
- [ ] 六包补 mock 单元测试（`test_code_runtime`/`test_sandbox`/`test_lsp`/`test_acp`/`test_tmux_context`/`test_e2b`，复用既有 fake ctx/shell 范式）—— 用户拍板暂缓
- [ ] `web`/`skill`/`workspace`/`preset`/`interaction` 家族补 `test_web`/`test_skill`/`test_tool_skill`/`test_workspace`/`test_agent_presets`/`test_interaction` 模块（复用冒烟用例）—— 用户拍板暂缓
- [ ] `e2b` 适配器 `fs-e2b`/`subprocess-e2b`（需 e2b 账号 + SDK）
- [ ] interaction 的 web 侧 UI 适配器（`/permission` 弹窗 / 命令面板 / user-questions provider / approval answerer）—— seam 已备齐（`ctx.commands`/`ctx.approval`/`ctx.userQuestions`/`ctx.permissionPresets`），属第 4 层 UI 集成工作
- [ ] 第 4 层收尾：`acp-agent`（ACP 协议入口）、`host`（聚合宿主/桌面 UI）、跨语言 client 完整版
- [ ] 遥测 / session-telemetry-otel（attribution 强制头已落地，剩采集与导出）
- [ ] 完整 Web 应用（gateway 已有常驻雏形，缺完整前端/鉴权）

### 已完结里程碑

- [X] 真实 key 联调：DeepSeek / OpenAI / 本地 Ollama 网关（用户已确认）
- [X] MCP `streamable-http` 传输端到端验证
- [X] 跨进程 SDK 运行时（newline JSON-RPC）+ Web 网关（WebSocket 广播）
- [X] typert 协议层（@remote/@remote_scope）
- [X] session sqlite 持久化（zstd）+ checkpoint 崩溃恢复 + projection/query 完整版
- [X] compaction 记忆压缩全套（basic + pruner + `/compact`）
- [X] workflow 编排引擎（seam + 内联引擎 + workflow/ralph 两工具）
- [X] subprocess 进程 seam（树级 spawn + 终止升级 + process-inspector）
- [X] goal-round-driver 同会话续行驱动（goal 家族闭环）
- [X] shell / jobs / bash 接线到 subprocess seam（消除重复 Popen）

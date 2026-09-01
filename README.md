# dsh-py

用 **Python 一比一复刻 [dsh](https://github.com/deepseek-ai/dsh)**（DeepSeek Harness）的完整功能——让不懂 TypeScript 的开发者也能用上 dsh 的全套「全插件式」Agent 框架能力。

> **现状**：第 0/1/2/4 层全部完成；第 3 层支撑服务 **45 个功能包已落地**（含近期补完的纯 Python 工具/后端包，均含冒烟全绿）；**79 个测试模块全绿**（约 540+ 断言）；框架内核零第三方依赖。逐包进度见 [`docs/PROGRESS.md`](docs/PROGRESS.md)。

---

## 目录

- [1. 项目定位](#1-项目定位)
- [2. 架构总览](#2-架构总览)
- [3. 安装](#3-安装)
- [4. 快速开始](#4-快速开始)
- [5. profile 机制（一切皆插件）](#5-profile-机制配置即插件清单一切皆插件)
- [6. SDK 编程式用法](#6-sdk-编程式用法)
- [7. 复刻进度概要](#7-复刻进度概要)
- [8. 测试](#8-测试)
- [相关文档](#相关文档)

---

## 1. 项目定位

dsh_py 是把 dsh 的 **TypeScript 实现逐包翻译成 Python** 的忠实复刻：cordis 内核、Loader/Boot 装配、Session/Agent/LLM 三 seam、支撑服务、应用层全部对齐 dsh 的包结构与行为语义，仅在「脚本执行面」等无法跨语言的部分做了有文档的取舍（见 [`docs/DIFFERENCES.md`](docs/DIFFERENCES.md)）。

**特性总览**：

- **一切皆插件**：模型适配器、工具注册表、会话日志、甚至智能体循环（agent loop）本身都是 profile 插件，任何一部分都可以从配置替换，运行期也可 `ctx.agents.set_factory(...)` 热换
- **cordis 内核完整翻译**：Fiber 生命周期、作用域树（多会话隔离）、schema 校验、依赖拓扑 + 延迟就绪、内置 logger/reflect/registry、Loader/Boot 多 layer 装配、热重载 watcher
- **三 seam 完整版**：Session（JSONL/SQLite+zstd 持久化、resume、checkpoint、projection/query）/ Agent（Inbox 双队列、cancel 三源融合、声明式、resume）/ LLM（call-config 三层合并、retry、api-key 解析链、多路由适配器）
- **支撑服务 45 包**：system-prompt / tools / settings / subagent / mcp / compaction / fs·shell·terminal / guard / hooks / schedule / todo / attachment / feedback / storage / spill / identity / jobs / context 家族 / goal 家族 / plan / workflow 编排引擎 / subprocess / typert / invariants，以及 web / skill / workspace / preset / interaction / subagent-acp 家族（详见 [`docs/PROGRESS.md`](docs/PROGRESS.md)）
- **应用层**：进程内 SDK、跨进程 JSON-RPC SDK、WebSocket 常驻网关、交互式 CLI + headless 单任务模式

**依赖策略**：框架内核零第三方依赖；HTTP 类适配器懒加载 `httpx`；sqlite 后端可选 `zstandard`；Web 网关依赖 `websockets`（均仅应用层）。新依赖统一装入隔离 venv。

---

## 2. 架构总览

```
┌─ 第 4 层 应用层 ─────────────────────────────────────────────┐
│  cli.py（交互/headless/--jsonrpc）  sdk.py（进程内）          │
│  api/（跨进程 JSON-RPC over stdio）  gateway.py + websocket   │
├─ 第 3 层 支撑服务（45 包，全部插件装配）────────────────────────┤
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

> 改任何包前先读承重不变量：[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)（waterfall 监听器统一 async、作用域树、projection 全值事件、取消三源融合、tools schema 契约等）。

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
        "provider": "deepseek",
        "model": "deepseek-chat",
        "api_key": "sk-xxxxx",
        "api_keys": {"openai": "sk-..."},
        "temperature": 0.7,
        "max_tokens": 4096,
    },
    "database": {"url": "sqlite:///sessions.db"},
    "workdir": "~/.dsh/work",
}
```

CLI 显式参数（`--provider` / `--model` / `--max-tokens`）优先于配置文件；`--config FILE` 可指定另一份配置文件。

**API key 解析优先级**：配置文件 `llm.api_key` / `llm.api_keys.<provider>` > credentials 服务 > 环境变量。

```bash
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

另有一个 **DeepSeek 官方专用适配器**（`llm-deepseek` 翻译，thinking/reasoning 协议），在 `configs/profile.py` 加一行后使用：

```python
"dsh_py.services.adapters.deepseek:apply",
```

```bash
python -m dsh_py.cli --provider deepseek-official --model deepseek-v4-pro
```

### 4.3 headless 单任务（对齐 `dsh --profile headless "task"`）

```bash
# 一条任务 → 创建（并持久化）新会话 → 打印最终回复 → 退出
python -m dsh_py.cli --mock --message "写个冒泡排序"
```

---

## 5. profile 机制（配置即插件清单，一切皆插件）

**装配点唯一**：默认运行与自定义都指向同一个 profile 文件（`dsh_py/configs/profile.py`）。自定义组件 = **直接编辑这个文件**，无需新建第二个 profile；CLI 每次启动都加载它。`--profile` 仅在你确实想换成别的装配文件时才需要。

profile 是一个 `.py` 文件，导出 `PROFILE` 列表，元素可以是：

- **字符串** `"dsh_py.plugins.long_term_memory"` —— 模块名，调用其 `apply(ctx, config)`
- **字符串** `"dsh_py.services.agent:apply_loop"` —— `模块:属性`，精准指向某个插件入口
- **可调用** `apply(ctx, config)` 函数（插件）
- **字典** `{"plugin": "模块:属性", "config": {...}}` 或 `{"apply": fn, "config": {...}}` —— 带配置的插件行；`{"id": "x", ...}` 形式带 id，供上层 layer 以 patch 指令定位覆盖

**核心 seam 也是插件**（不硬编码装配）：`llm` / `sessions` / `tools` / `agents` 注册表 / `agentLoop`（默认智能体循环）全部作为 profile 条目按序加载，**每一部分都可以从配置替换**。核心清单由 `CORE_PROFILE`（bundle 层）提供，`boot` 管线自动叠加：bundle 层 → 用户层（`configs/profile.py`）→ `--patch` overlays。

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

### 6.2 跨进程 SDK（对标 dsh 的 `sdk-jsonrpc-server` + `dsh-sdk-client`）

同名同 API 的**子进程版**：`dsh_py.api.DeepSeekHarness` 拉起一个 `python -m dsh_py.cli --jsonrpc` 运行时子进程，走 newline JSON-RPC 通信——进程外/跨语言客户端可用同一协议驱动 harness（wire 定义在 `dsh_py/api/protocol.py`）。协议面与 dsh 的 `dsh-sdk-protocol` 一致：`initialize` / `session/prompt` / `shutdown` + `session.event` / `session.status` 通知。

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

服务器也可直接跑：`python -m dsh_py.cli --jsonrpc --mock`（stdout 仅协议帧）。

### 6.3 Web 网关（常驻后端服务，对标 dsh 的 `api/gateway`）

把 harness 变成**网络服务**：`python -m dsh_py.gateway --port 8080` 起一个常驻 WebSocket 服务器，远程客户端（网页 / App / 任何语言）连接 `ws://localhost:8080`，说**同一套 JSON-RPC**（方法面与 6.2 完全一致，传输从 stdio 换成 WebSocket 帧）：

```bash
python -m dsh_py.gateway --port 8080 --mock    # 离线演示
python -m dsh_py.gateway --port 8080 --mock --webui   # 同端口伺服浏览器前端
```

`--webui` 通过 `websockets.serve` 的 `process_request` 钩子在**同一端口**伺服内置单页前端（`dsh_py/webui/index.html`，零依赖、离线可用，视觉对齐 dsh 三栏布局与 `--dsw-*` token，消费新增的 `console.*` 方法面）。前端交互细节、面板/方法面对照、交互桥设计见 `dsh_py/webui/index.html` 与 `api/web_bridge.py` 内注释。

---

## 7. 复刻进度概要

- **第 0/1/2/4 层**：全部完成（cordis 内核、Loader/Boot、三 seam、应用层）。
- **第 3 层支撑服务**：45 个功能包代码落地（含 web / skill / workspace / preset / interaction / subagent-acp 家族，均端到端冒烟；近期另补 session-title / shell-env / session-telemetry / session-query-sqlite / fs-sandbox / fs-observation-policy 等纯 Python 工具/后端包，均含入库冒烟）。
- **六包**（`code-runtime`/`sandbox`/`lsp`/`acp`/`tmux-context`；`e2b` 仅占位 seam）：`code-runtime`/`sandbox` 已带 seam 契约单测（正式入库，79 模块全绿）；`lsp`/`acp`/`tmux-context` 通过 `py_compile`、尚未做运行时验证与单测；`e2b` 后端（`fs-e2b`/`subprocess-e2b`）用户已明确排除（需账号 + SDK）。

完整逐包清单、A 类工具包明细、六包决策背景、未做项 → [`docs/PROGRESS.md`](docs/PROGRESS.md)。

---

## 8. 测试

纯 `assert` 脚本（无需 pytest），用项目隔离 venv 的 python 直接跑：

```bash
cd dsh_py 所在目录
for t in dsh_py/tests/test_*.py; do python "$t"; done
# 或单跑某个：python dsh_py/tests/test_workflow.py
```

**79 个测试模块**（约 540+ 断言）按层分组清单见 [`docs/PROGRESS.md`](docs/PROGRESS.md)。

---

## 相关文档

| 文档 | 内容 |
| --- | --- |
| [`docs/PROGRESS.md`](docs/PROGRESS.md) | 逐包复刻进度（45 包清单 + 六包 + 未做项）+ 测试模块清单 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 架构总览图 + 承重不变量（防回归） |
| [`docs/STRUCTURE.md`](docs/STRUCTURE.md) | `dsh_py/` 完整目录结构树 |
| [`docs/DIFFERENCES.md`](docs/DIFFERENCES.md) | 与 dsh（TypeScript）的已知差异对照表 |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | TODO / 路线图（近期 + 已完结里程碑） |

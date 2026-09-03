# dsh-py

> 纯 Python 一比一复刻 [DeepSeek Harness（dsh）](https://github.com/deepseek-ai/dsh) 的全插件式 Agent 运行时。
> 让不懂 TypeScript 的开发者也能用上 dsh 的整套「一切皆插件」框架能力。

**dsh-py** is a faithful Python port of DeepSeek's [dsh](https://github.com/deepseek-ai/dsh) (TypeScript/Cordis) Agent runtime — same package structure, same event-sourced semantics, same plugin model. It is built so Python developers can run, extend, and embed dsh-style agents without touching TypeScript.

![python](https://img.shields.io/badge/python-%E2%89%A53.10-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![tests](https://img.shields.io/badge/tests-89%20modules%20%2F%2089%20passing-brightgreen)
![upstream](https://img.shields.io/badge/upstream-deepseek--ai%2Fdsh-blue)

---

## 目录

- [1. 简介](#1-简介)
- [2. 特性](#2-特性)
- [3. 架构总览](#3-架构总览)
- [4. 安装](#4-安装)
- [5. 快速开始](#5-快速开始)
- [6. 插件机制：一切皆插件](#6-插件机制一切皆插件)
- [7. SDK 编程式用法](#7-sdk-编程式用法)
- [8. 复刻范围与状态](#8-复刻范围与状态)
- [9. 测试](#9-测试)
- [10. 文档导航](#10-文档导航)
- [11. 贡献指南](#11-贡献指南)
- [12. 许可证与致谢](#12-许可证与致谢)

---

## 1. 简介

dsh 是 DeepSeek 开源的**事件溯源式 Agent 运行时底座**：基于 Cordis 插件内核，一个可运行的 Agent = `Profile`（配置档）叠加 `Bundle`（能力束）叠加 `Plugin`（插件）。任何「产品能力」——模型适配器、工具、会话持久化、沙箱、审批、UI——都是可插拔的插件，产品本身由一组分层组合的 `cordis.yml` 在启动时拼装而成。

dsh-py 把这套架构**逐包翻译成 Python**：cordis 内核、Loader/Boot 装配、Session/Agent/LLM 三 seam、支撑服务、应用层全部对齐 dsh 的包结构与行为语义。仅在无法跨语言的部分做了有文档的取舍（见 [docs/DIFFERENCES.md](docs/DIFFERENCES.md)）。

**为什么需要它**：dsh 本体是 TypeScript，对 Python 技术栈团队不友好；dsh-py 让你用 Python 直接复用同一套「全插件」心智模型，并便于在 Python 生态（FastAPI、LangChain、数据分析等）中嵌入。

---

## 2. 特性

- **一切皆插件**：模型适配器、工具注册表、会话日志，甚至智能体循环（agent loop）本身都是 profile 插件。任何一部分都能从配置替换，运行期也可 `ctx.agents.set_factory(...)` 热换。
- **cordis 内核完整翻译**：Fiber 生命周期、作用域树（多会话隔离）、schema 校验、依赖拓扑 + 延迟就绪、内置 logger/reflect/registry、Loader/Boot 多 layer 装配、热重载 watcher。
- **三 seam 完整版**：
  - `Session`：JSONL/SQLite+zstd 持久化、resume、checkpoint、projection/query
  - `Agent`：Inbox 双队列、cancel 三源融合、声明式、resume
  - `LLM`：call-config 三层合并、retry、api-key 解析链、多路由适配器（OpenAI 兼容 ×7 厂商 / DeepSeek 官方 / PI-AI）
- **支撑服务 45 包**：system-prompt / tools / settings / subagent / mcp / compaction / fs·shell·terminal / guard / hooks / schedule / todo / attachment / feedback / storage / spill / identity / jobs / context 家族 / goal 家族 / plan / workflow 编排引擎 / subprocess / typert / invariants，以及 web / skill / workspace / preset / interaction / subagent-acp 家族（完整清单见 [docs/STATUS.md](docs/STATUS.md)）。
- **多种入口**：交互式 CLI、headless 单任务、进程内 SDK、跨进程 JSON-RPC SDK、常驻 WebSocket 网关、内置 Web UI。
- **零核心依赖**：框架内核不依赖任何第三方库；HTTP 适配器懒加载 `httpx`，Web 网关依赖 `websockets`，均仅应用层。

---

## 3. 架构总览

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

> 改任何包前先读承重不变量：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)（waterfall 监听器统一 async、作用域树、projection 全值事件、取消三源融合、tools schema 契约等）。

---

## 4. 安装

```bash
# 方式一：作为可编辑包安装（推荐，自动装 httpx + 提供 dsh-py 命令）
pip install -e .

# 方式二：仅装运行依赖，直接用模块方式运行
pip install httpx

# 可选：开发依赖（含 pytest，尽管测试本体不依赖 pytest）
pip install -e ".[dev]"
```

> 要求 Python ≥ 3.10。

---

## 5. 快速开始

### 5.1 离线演示（无需任何 key）

```bash
python -m dsh_py.cli --mock
# 或安装后：dsh-py --mock
```

输入消息即可看到全链路流式输出（mock 模型固定回复）。

### 5.2 接入真实模型（推荐：统一配置文件）

> **key、模型参数、数据库、工作目录等集中写在配置文件，不依赖环境变量。**

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

### 5.3 headless 单任务（对齐 `dsh --profile headless "task"`）

```bash
# 一条任务 → 创建（并持久化）新会话 → 打印最终回复 → 退出
python -m dsh_py.cli --mock --message "写个冒泡排序"
```

### 5.4 Web UI（内置单页前端）

```bash
python -m dsh_py.gateway --port 8080 --mock --webui
# 浏览器打开 http://localhost:8080
```

`--webui` 通过 `websockets.serve` 的 `process_request` 钩子在**同一端口**伺服内置单页前端（`dsh_py/webui/index.html`，零依赖、离线可用，视觉对齐 dsh 三栏布局）。

---

## 6. 插件机制：一切皆插件

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

> 运行时换循环也无需重启装配：`ctx.agents.set_factory(你的实现)` 即完成替换，所有调用方都经 `ctx.agents.create_agent`，不感知具体实现。

---

## 7. SDK 编程式用法

### 7.1 进程内 SDK（对标 dsh 的 `@deepseek-ai/dsh-sdk`）

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

### 7.2 跨进程 SDK（对标 dsh 的 `sdk-jsonrpc-server` + `dsh-sdk-client`）

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

### 7.3 Web 网关（常驻后端服务，对标 dsh 的 `api/gateway`）

把 harness 变成**网络服务**：`python -m dsh_py.gateway --port 8080` 起一个常驻 WebSocket 服务器，远程客户端（网页 / App / 任何语言）连接 `ws://localhost:8080`，说**同一套 JSON-RPC**（方法面与 7.2 完全一致，传输从 stdio 换成 WebSocket 帧）。详见 `dsh_py/webui/index.html` 与 `api/web_bridge.py` 内注释。

---

## 8. 复刻范围与状态

| 层 | 状态 | 说明 |
|---|---|---|
| 第 0 层 内核（cordis） | ✅ 完整 | Fiber / 作用域树 / schema / inject 拓扑 / 内置 logger·reflect·registry |
| 第 1 层 Loader/Boot | ✅ 完整 | 多 layer profile、inject 拓扑排序、热重载、env 插值、加载期校验 |
| 第 2 层 三 seam | ✅ 完整 | LLM / Session / Agent 三面主能力齐（checkpoint、projection 缓存、取消三源融合） |
| 第 3 层 支撑服务 | ✅ 范围内完整 | 约 39/42 功能家族完整；剩余为已拍板排除项（见下） |
| 第 4 层 应用层 | ✅ 完整 | CLI / SDK / API / 常驻网关 / webui 齐；`host` 后端子服务（webserver/plugin-inventory/loader）已落地 |

**结论**：在定义范围内（Python 后端，不含第三方应用接入 / 前端 client / 云端 e2b / Windows ACL 沙箱），复刻已完成；89 个测试模块全绿，范围内功能家族全覆盖。

**已明确排除（非功能缺口）**：
- `e2b` 后端（`fs-e2b`/`subprocess-e2b`）—— 需 e2b 账号 + 官方 SDK
- `sandbox-windows-acl` —— Windows 原生 ACL FFI，非 Python 复刻优先项
- `subagent` 外进程后端（claude-code / codex / dsh-sdk）—— 跨语言生态接入口径
- `session-log-export` / `host` 桌面壳 / webui 增强 UI（附件·模型选择·设置读写·主题切换·富块渲染·i18n）—— 前端/React 生态，不在 Python 复刻范围
- `web` 搜索外部调用需 API key，按第三方接入口径可忽略

完整逐包清单、A/B/C 类决策背景、未做项 → [docs/STATUS.md](docs/STATUS.md)；与 dsh 的已知差异 → [docs/DIFFERENCES.md](docs/DIFFERENCES.md)。

---

## 9. 测试

纯 `assert` 脚本（无需 pytest），用项目隔离 venv 的 python 直接跑：

```bash
# 全量回归（逐个子进程跑 dsh_py/tests/test_*.py，按退出码判成败）
python run_all_tests.py
# 或单跑某个：python dsh_py/tests/test_workflow.py
```

**89 个测试模块**（约 620+ 断言）按层分组清单见 [docs/STATUS.md](docs/STATUS.md)。

---

## 10. 文档导航

| 文档 | 内容 | 受众 |
| --- | --- | --- |
| [docs/STATUS.md](docs/STATUS.md) | 逐包复刻进度（45 包清单 + 六包 + 排除项）+ 测试模块清单 | 所有人 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 架构总览图 + 承重不变量（防回归） | 贡献者 |
| [docs/STRUCTURE.md](docs/STRUCTURE.md) | `dsh_py/` 完整目录结构树 | 所有人 |
| [docs/DIFFERENCES.md](docs/DIFFERENCES.md) | 与 dsh（TypeScript）的已知差异对照表 | 所有人 |
| [docs/ROADMAP.md](docs/ROADMAP.md) | 已完成里程碑 + 未来可选方向 | 贡献者 |
| [docs/reference/](docs/reference/) | 上游 dsh 源码分析、复刻计划、差距分析等内部研究资料 | 深度读者 |

---

## 11. 贡献指南

### 开发环境

```bash
git clone <your-fork>
cd dsh-py
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python run_all_tests.py                             # 期望 89/89 通过
```

### 编码约定

- **源码一律中文注释 / docstring**。
- 每完成一个包给出**独立验证**（纯 `assert` + `__main__` 自跑，无需 pytest）。
- 不动内核承重不变量（改任何包前先读 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)）。
- 配置走文件（`configs/dsh_config.py` + 机器级 `~/.dsh/dsh_config.py` 深合并），不依赖环境变量。
- 「一切皆插件」：核心 seam 也是 profile 插件；`inject`/`provides` 供 loader 拓扑排序。

### 新增一个包的流程

1. **读源码**：在 `source/`（上游 dsh TypeScript）找到对应包，理解其事件 / seam / 契约。
2. **补缝**：若缺底层 seam（如 subprocess / sandbox），先实现 seam 再补功能包。
3. **实现**：逐文件翻译到 `dsh_py/services/` 或 `dsh_py/plugins/`，中文注释。
4. **测试**：在 `dsh_py/tests/` 写 `test_*.py`（`assert` 自跑 + 关键契约单测）。
5. **登记**：更新 [docs/STATUS.md](docs/STATUS.md) 的对应行。

---

## 12. 许可证与致谢

- **许可证**：[MIT](LICENSE)
- **上游**：本项目的架构、包结构、事件契约均对齐 DeepSeek 开源的 [dsh](https://github.com/deepseek-ai/dsh)（MIT）。
- **致谢**：感谢 DeepSeek AI 开源 dsh，使这套「全插件式 Agent 运行时」得以被更广泛地复用。

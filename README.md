# dsh-py

用 **Python 复刻 [dsh](https://github.com/deepseek-ai/dsh)**（DeepSeek Harness）的完整功能——让不懂 TS 的开发者也能用上 dsh 的全套
「全插件式」Agent 框架能力：

- **一切皆插件**：模型适配器、工具注册表、会话日志、甚至智能体循环（agent loop）本身都是
  profile 插件，任何一部分都可以从配置替换
- **cordis 内核完整翻译**：Fiber 生命周期、作用域树（多会话隔离）、schema 校验、依赖拓扑、
  Loader/Boot 多 layer 装配、热重载
- **三 seam 完整版**：Session（JSONL 持久化 + resume）、Agent（Inbox / cancel / 声明式 agents）、
  LLM（call-config 三层合并 / 重试策略 / api-key 校验）
- **支撑服务**：system-prompt 组装体系、tools 完整版（参数校验 / 并行）、subagent、settings +
  credentials、MCP 客户端桥接
- **应用层**：进程内 SDK（`DeepSeekHarness`）、交互式 CLI + headless 单任务模式

框架本体 **零第三方依赖**；仅 HTTP 类适配器（OpenAI 兼容 / DeepSeek / MCP streamable-http）
懒加载 `httpx`。

---

## 1. 安装

```bash
# 方式一：作为可编辑包安装（推荐，自动装 httpx + 提供 dsh-py 命令）
cd dsh-py 所在目录
pip install -e .

# 方式二：仅装运行依赖，直接用模块方式运行
pip install httpx
```

> 要求 Python ≥ 3.10。

---

## 2. 快速开始

### 2.1 离线演示（无需任何 key）

```bash
python -m dsh_py.cli --mock
# 或安装后：dsh-py --mock
```

输入消息即可看到全链路流式输出（mock 模型固定回复）。

### 2.2 接入真实模型

适配器为**休眠态**，靠环境变量驱动。以 DeepSeek 为例：

```bash
export DEEPSEEK_API_KEY="sk-xxxxx"

# 默认与自定义都指向同一个 profile（configs/profile.py），无需任何参数即可接真实模型
python -m dsh_py.cli --provider deepseek --model deepseek-chat
```

内置 7 个厂商及对应环境变量（OpenAI 兼容适配器）：


| provider   | 环境变量            | 备注                                                                   |
| ---------- | ------------------- | ---------------------------------------------------------------------- |
| `openai`   | `OPENAI_API_KEY`    |                                                                        |
| `qwen`     | `DASHSCOPE_API_KEY` | 通义千问                                                               |
| `zhipu`    | `ZHIPU_API_KEY`     | 智谱 GLM                                                               |
| `moonshot` | `MOONSHOT_API_KEY`  | Kimi                                                                   |
| `deepseek` | `DEEPSEEK_API_KEY`  | DeepSeek 兼容                                                          |
| `ollama`   | （可空）            | 本地网关，默认`http://localhost:11434/v1`，可用 `OLLAMA_BASE_URL` 覆盖 |
| `vllm`     | （可空）            | 本地网关，默认`http://localhost:8000/v1`，可用 `VLLM_BASE_URL` 覆盖    |

未设置对应 key 的厂商不会真正发请求（缺 key 时抛 `MISSING_CREDENTIAL`）。

另有一个 **DeepSeek 官方专用适配器**（`llm-deepseek` 翻译，thinking/reasoning 协议），
在 `configs/profile.py` 加一行后使用：

```python
"dsh_py.services.adapters.deepseek:apply",
```

```bash
export DEEPSEEK_API_KEY="sk-xxxxx"
python -m dsh_py.cli --provider deepseek-official --model deepseek-v4-pro
```

### 2.3 headless 单任务（对齐 `dsh --profile headless "task"`）

```bash
# 一条任务 → 创建（并持久化）新会话 → 打印最终回复 → 退出
python -m dsh_py.cli --mock --message "写个冒泡排序"
```

---

## 3. profile 机制（配置即插件清单，**一切皆插件**）

**装配点唯一**：默认运行与自定义都指向同一个 profile 文件
（`dsh_py/configs/profile.py`）。自定义组件 = **直接编辑这个文件**，无需新建
第二个 profile；CLI 每次启动都加载它。`--profile` 仅在你确实想换成别的装配文件
时才需要。

profile 是一个 `.py` 文件，导出 `PROFILE` 列表，元素可以是：

- **字符串** `"dsh_py.plugins.long_term_memory"` —— 模块名，调用其 `apply(ctx, config)`
- **字符串** `"dsh_py.services.agent:apply_loop"` —— `模块:属性`，精准指向某个插件入口
- **可调用** `apply(ctx, config)` 函数（插件）
- **字典** `{"plugin": "模块:属性", "config": {...}}` 或 `{"apply": fn, "config": {...}}` ——
  带配置的插件行；`{"id": "x", ...}` 形式带 id，供上层 layer 以 patch 指令定位覆盖

**核心 seam 也是插件**（不硬编码装配）：`llm` / `sessions` / `tools` / `agents`
注册表 / `agentLoop`（默认智能体循环）全部作为 profile 条目按序加载，**每一部分
都可以从配置替换**。核心清单由 `CORE_PROFILE`（bundle 层）提供，`boot` 管线自动
叠加：bundle 层 → 用户层（`configs/profile.py`）→ `--patch` overlays。

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

> 运行时换循环也无需重启装配：`ctx.agents.set_factory(你的实现)` 即完成替换，
> 所有调用方都经 `ctx.agents.create_agent`，不感知具体实现。同理，`sessions` /
> `tools` / `llm` 也可提供同名服务的插件整体替换。

改完直接运行（自动加载的就是这个文件）：

```bash
python -m dsh_py.cli --provider deepseek --model deepseek-chat
```

---

## 4. SDK 编程式用法

推荐使用进程内 SDK（对标 dsh 的 `@deepseek-ai/dsh-sdk`）：

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

---

## 5. 目录结构

```
dsh_py/
  core/      context, events, service, fiber, schema, signal
             # 服务容器 + 事件总线(waterfall) + Fiber 生命周期 + 作用域树 + schema 校验 + 取消信号
  services/  llm, message, session, tools, agent, inbox,
             call_config, retry_policy, system_prompt, settings, credentials,
             session_persistence
             # 三 seam 完整版 + 支撑服务
  services/adapters/  openai_compatible（7 厂商）, deepseek（官方专用）
  plugins/   system_instructions, long_term_memory, subagent,
             mcp_client/  # MCP 桥接（client + bridge + 插件入口）
  loader.py  CORE_PROFILE, compose_entries, boot, load_profile   # Loader/Boot 管线
  env.py     .env 分层加载 + 环境变量插值
  watcher.py profile 热重载（st_mtime_ns 轮询）
  sdk.py     DeepSeekHarness / HarnessSession / RunResult（进程内 SDK）
  cli.py     --profile/--patch/--provider/--model/--system/--mock/--message
  configs/   profile.py                       # 唯一装配点（用户层）
  tests/     20 个测试文件
```

---

## 6. 复刻进度（对照 dsh 逐包翻译）


| 层                      | 内容                                                                                                               | 状态    |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------ | ------- |
| **第 0 层 · 内核**     | Fiber 生命周期 / 作用域树（isolate/intercept）/ schema 校验 / inject 拓扑 / Loader-Boot（多 layer + env + 热重载） | ✅ 完成 |
| **第 2 层 · 三 seam**  | Session（JSONL 持久化 + resume）/ Agent（Inbox / cancel / 声明式）/ LLM（call-config / retry / api-key）           | ✅ 完成 |
| **第 3 层 · 支撑服务** | system-prompt / tools 完整版 / subagent / settings + credentials / mcp-client                                      | ✅ 完成 |
| **第 4 层 · 应用层**   | 进程内 SDK / headless CLI                                                                                          | ✅ 完成 |
| **补遗**                | llm-deepseek 专用适配器（thinking/reasoning 协议）                                                                 | ✅ 完成 |

---

## 7. 测试

```bash
# 用项目隔离 venv 的 python（或任意装了 httpx 的 3.10+ 环境）
for t in test_core test_llm test_adapter test_agent test_plugins test_adapter_http \
         test_fiber test_scope test_schema test_boot test_session_persistence \
         test_agent_extended test_llm_full test_system_prompt test_tools_full \
         test_subagent test_settings_credentials test_sdk test_adapter_deepseek \
         test_mcp_client; do python -m dsh_py.tests.$t; done
```

20 个测试全部通过即表示内核、三 seam、支撑服务、应用层均可用。

---

## 8. TODO

### 近期（联调与验证）

- [ ]  **真实 key 联调**：DeepSeek / OpenAI / 本地 Ollama 网关实打一轮（代码路径已用
  mock + 本地 SSE 验证，未在生产端点实打过）
- [ ]  MCP `streamable-http` 传输端到端验证（目前 stdio 已端到端覆盖）
- [ ]  `test_adapter_http` / `test_mcp_client` 在 Windows 退出期的管道 GC 噪声清理
  （良性，不影响结果）

### 中期（功能补全）

- [ ]  **web / API 网关**：`packages/api/gateway` 翻译——JSON-RPC over WebSocket 服务器，
  让 `DeepSeekHarness` 支持跨进程/跨语言客户端（对标 dsh 的 `HarnessClient`）
- [ ]  llm-pi-ai 适配器（多厂商协议路由层翻译）
- [ ]  session sqlite 持久化后端（目前仅 JSONL）
- [ ]  session projection / checkpoint-policy（崩溃恢复）
- [ ]  compaction（记忆压缩）插件
- [ ]  更多内置工具（bash / fs / terminal 等 dsh 生态包）

### 远期（生产化）

- [ ]  guard（护栏）/ hooks / schedule 等治理插件
- [ ]  e2b / sandbox / code-runtime（沙箱执行）
- [ ]  遥测 / anonymous-user-id / 归因头（attribution）
- [ ]  完整 Web 应用（apps/web 翻译）

---

## 9. 已知限制

- 长记忆为关键词召回原型，非语义向量检索；存储为本地 JSONL。
- 真实模型代码路径已用本地 SSE 服务器验证，但**未用真实 API key 在生产环境联调**（见 TODO）。
- `pyproject.toml` 仅声明 `httpx` 为必需依赖，测试依赖（`pytest`）放在可选 `dev` 分组。
- Windows 上子进程类测试（MCP stdio）在解释器退出期可能打印良性管道 GC 噪声。

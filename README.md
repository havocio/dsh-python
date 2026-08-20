# dsh-py

用 **Python 一比一复刻 [dsh](https://github.com/deepseek-ai/dsh)**（DeepSeek Harness）的完整功能——让不懂 TS 的开发者也能用上 dsh 的全套

「全插件式」Agent 框架能力：

- **一切皆插件**：模型适配器、工具注册表、会话日志、甚至智能体循环（agent loop）本身都是

  profile 插件，任何一部分都可以从配置替换
- **cordis 内核完整翻译**：Fiber 生命周期、作用域树（多会话隔离）、schema 校验、依赖拓扑 +

  延迟就绪、内置 logger/reflect/registry、Loader/Boot 多 layer 装配、热重载
- **三 seam 完整版**：Session（JSONL/SQLite+zstd 持久化、resume、checkpoint 崩溃恢复、

  projection/projection-cache/stats、session-query 完整检索）、Agent（Inbox、cancel 三源融合、

  声明式 agents、resume）、LLM（call-config 三层合并、retry、api-key、attribution/brand、topology）
- **支撑服务**：system-prompt 组装体系、tools 完整版（参数校验 / 有界并行）、subagent、

  settings + credentials、MCP 客户端桥接、compaction 记忆压缩（token-meter + basic + 修剪 +

  `/compact` 命令）、fs·shell·terminal 内置工具、typert 声明式远程调用
- **应用层**：进程内 SDK（`DeepSeekHarness`）、跨进程 JSON-RPC SDK、WebSocket 网关、

  交互式 CLI + headless 单任务模式

框架本体 **零第三方依赖**；HTTP 类适配器（OpenAI 兼容 / DeepSeek / MCP streamable-http）

懒加载 `httpx`，sqlite 后端可选 `zstandard`，Web 网关依赖 `websockets`（均仅应用层）。

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

### 2.2 接入真实模型（推荐：统一配置文件）

**key、模型参数、数据库、工作目录等集中写在配置文件，不依赖环境变量**。

默认配置在 `dsh_py/configs/dsh_config.py`（唯一配置编辑点，随仓库走）；

个人机器级覆盖写在 `~/.dsh/dsh_config.py`（同结构，深合并覆盖，不进仓库）。

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

CLI 显式参数（`--provider` / `--model` / `--max-tokens`）优先于配置文件；

`--config FILE` 可指定另一份配置文件（指定后不再合并默认两层）。

```bash
python -m dsh_py.cli                      # 直接用配置文件的 provider/model
python -m dsh_py.cli --provider deepseek --model deepseek-chat   # 命令行覆盖
python -m dsh_py.cli --config my_config.py                       # 自定义配置文件
```

**API key 解析优先级**：配置文件 `llm.api_key` / `llm.api_keys.<provider>` >

credentials 服务 > 环境变量。所以下面这种传统方式仍然兼容：

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

另有一个 **DeepSeek 官方专用适配器**（`llm-deepseek` 翻译，thinking/reasoning 协议），

在 `configs/profile.py` 加一行后使用：

```python
"dsh_py.services.adapters.deepseek:apply",
```

```bash
# key 写进配置文件的 llm.api_key（deepseek-official 路由读这个字段）
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

注册表 / `agentLoop`（默认智能体循环）全部作为 profile 条目按序加载，\*\*每一部分

都可以从配置替换\*\*。核心清单由 `CORE_PROFILE`（bundle 层）提供，`boot` 管线自动

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
>
> 所有调用方都经 `ctx.agents.create_agent`，不感知具体实现。同理，`sessions` /
>
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

### 4.1 跨进程 SDK（对标 dsh 的 `sdk-jsonrpc-server` + `dsh-sdk-client`）

同名同 API 的**子进程版**：`dsh_py.api.DeepSeekHarness` 拉起一个

`python -m dsh_py.cli --jsonrpc` 运行时子进程，走 newline JSON-RPC 通信——

进程外/跨语言客户端可用同一协议驱动 harness（wire 定义在 `dsh_py/api/protocol.py`）。

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

### 4.2 Web 网关（常驻后端服务，对标 dsh 的 `api/gateway`）

把 harness 变成**网络服务**：`python -m dsh_py.gateway --port 8080` 起一个常驻

WebSocket 服务器，远程客户端（网页 / App / 任何语言）连接

`ws://localhost:8080`，说**同一套 JSON-RPC**（方法面与 4.1 完全一致，传输从

stdio 换成 WebSocket 帧）：

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

**多连接共享**：每个连接独立订阅，事件广播到全部连接（共享 harness 实例）。

依赖 `websockets`（仅应用层；框架本体仍零依赖）。

---

## 5. 目录结构

```
dsh_py/
  core/      __init__, context, events, service, fiber, schema, signal,
             logger, reflect, registry
             # 服务容器 + 事件总线(waterfall) + Fiber 生命周期 + 作用域树 + schema 校验
             # + 取消信号 + inject 拓扑/延迟就绪 + 内置三服务
  services/  llm, message, session, agent, tools, system_prompt, settings, credentials,
             inbox, call_config, retry_policy, session_persistence,
             attribution, brand, projection, projection_cache, session_stats, session_query,
             token_meter, compaction, compaction_basic, tool_result_pruner, commands,
             fs, shell, terminal, storage_kv
             # 三 seam 完整版 + 支撑服务 + 记忆压缩 + 内置工具能力
  services/adapters/  openai_compatible（7 厂商）, deepseek（官方专用）
  plugins/   system_instructions, long_term_memory, subagent,
             tool_fs, tool_bash, tool_terminal, command_compact,
             mcp_client/  # MCP 桥接（client + bridge + 插件入口）
  loader.py  CORE_PROFILE, compose_entries, boot, load_profile   # Loader/Boot 管线
  env.py     .env 分层加载 + 环境变量插值
  config.py  AppConfig + load_app_config            # 统一配置文件（key/模型/数据库/工作目录）
  api/       protocol, server, client, websocket    # 跨进程 SDK + Web 网关
  gateway.py 常驻 WebSocket 网关入口（--port/--mock/--host）
  watcher.py profile 热重载（st_mtime_ns 轮询）
  sdk.py     DeepSeekHarness / HarnessSession / RunResult（进程内 SDK）
  cli.py     --profile/--patch/--config/--provider/--model/--system/--mock/--message/--jsonrpc
  configs/   profile.py                       # 唯一装配点（用户层）
             dsh_config.py                   # 唯一配置编辑点（key/参数/路径）
  tests/     35 个测试模块
```

---

## 6. 复刻进度（对照 dsh 逐包翻译）


| 层                      | 内容                                                                                                                                                                                                                                                                    | 状态    |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| **第 0 层 · 内核**     | Fiber 生命周期 / 作用域树（isolate/intercept）/ schema 校验 / inject 拓扑 + 延迟就绪 / 内置 logger·reflect·registry / Loader-Boot（多 layer + env + 热重载）                                                                                                          | ✅ 完成 |
| **第 2 层 · 三 seam**  | Session（JSONL + SQLite/zstd 持久化 + resume + checkpoint 崩溃恢复 + projection/projection-cache/stats/session-query 完整版）/ Agent（Inbox / cancel 三源融合 / 声明式校验 / resume）/ LLM（call-config / retry / api-key / attribution / topology / llm-pi-ai 多路由） | ✅ 完成 |
| **第 3 层 · 支撑服务** | system-prompt / tools 完整版 / subagent / settings + credentials / mcp-client / compaction（记忆压缩全套：basic + 修剪 +`/compact` 命令）/ fs·shell·terminal 内置工具、typert 声明式远程调用                                                                          | ✅ 完成 |
| **第 4 层 · 应用层**   | 进程内 SDK / 跨进程 JSON-RPC SDK / WebSocket 网关 / headless CLI                                                                                                                                                                                                        | ✅ 完成 |
| **补遗**                | llm-deepseek 专用适配器（thinking/reasoning 协议）/ llm-pi-ai 通用多路由适配器                                                                                                                                                                                          | ✅ 完成 |

---

## 7. 测试

```bash
# 用项目隔离 venv 的 python（或任意装了 httpx 的 3.10+ 环境）
for t in test_core test_llm test_adapter test_agent test_plugins test_adapter_http \
         test_fiber test_scope test_schema test_boot test_session_persistence \
         test_session_sqlite test_agent_extended test_llm_full test_system_prompt test_tools_full \
         test_subagent test_settings_credentials test_sdk test_adapter_deepseek \
         test_mcp_client test_config test_api_sdk test_gateway test_mcp_streamable_http \
         test_inject test_builtin test_layer2_complete test_compaction test_tool_fs_shell_terminal test_tool_result_pruner test_command_compact test_session_query_full test_typert test_llm_pi_ai; do python -m dsh_py.tests.$t; done
```

35 个测试全部通过即表示内核、三 seam、支撑服务、应用层均可用。

---

## 8. TODO

### 近期（联调与验证）

- [X]  **真实 key 联调**：DeepSeek / OpenAI / 本地 Ollama 网关实打一轮（用户已确认）
- [X]  **MCP `streamable-http` 传输端到端验证**（stdio 已覆盖；新增 `test_mcp_streamable_http` 3 项：握手→list→call + GET 通知流 + 插件集成，修复 `202` 无 `Content-Length` 致客户端挂起 + 空 body 误解析两处 bug）
- [X]  `test_adapter_http` / `test_mcp_client` 在 Windows 退出期的管道 GC 噪声清理

  （良性，不影响结果）

### 中期（功能补全）

- [X]  **跨进程 SDK 运行时**（web/API 网关第一步）：`dsh_py/api/`——newline JSON-RPC

  协议 + `HarnessSdkJsonRpcServer` + `HarnessClient`/`DeepSeekHarness` 子进程版

  （`--jsonrpc` 模式；对标 dsh 的 `sdk-jsonrpc-server` + `dsh-sdk-client`）
- [X]  **Web 网关**（对标 `packages/api/gateway`）：`dsh_py/gateway.py` +

  `api/websocket.py`——JSON-RPC over WebSocket 常驻服务（`--port/--mock/--host`），

  多连接共享 harness 实例、事件广播；依赖 `websockets`（应用层）
- [X]  **typert 协议层**：`@Remote`/`@RemoteScope` 装饰器 + 代码生成（对标

  `typert/protocol` + `generator`），业务方法声明式暴露
- [X]  ~~llm-pi-ai 适配器~~（`services/adapters/pi_ai.py`：内置目录 + 配置解析 + 快照适配器 + 模型发现；协议表/目录/thinkingFormat 为 Python 版子集，差异已注明）
- [X]  session sqlite 持久化后端（`SqliteSessionPersistence`：单库 + 单事务原子耐久 + 可选 zstd 压缩）
- [X]  session checkpoint-policy 崩溃恢复（`CheckpointPolicy` 周期写前缀快照 + `load` 优先续接）
- [X]  session projection / projection-cache / stats（投影注册表 + 持久化缓存 + 会话统计）
- [X]  session-query 完整版（live-preferred 语料库 / 双层过滤 / 事件窗口 / 全文检索分页游标 / 谱系与事件追踪；旧 API 兼容）
- [X]  ~~compaction（记忆压缩）全套~~（token-meter + seam + basic 后端 + 自动挂钩 + tool-result-pruner 修剪 + `/compact` 命令）
- [X]  ~~更多内置工具（bash / fs / terminal 等 dsh 生态包）~~（已落地：read/write/edit + bash + terminal）

### 远期（生产化）

- [ ]  guard（护栏）/ hooks / schedule 等治理插件
- [ ]  e2b / sandbox / code-runtime（沙箱执行）
- [ ]  遥测 / anonymous-user-id（attribution 强制头已落地，见第 2 层）
- [ ]  完整 Web 应用（apps/web 翻译）

---

## 9. 已知限制

- 长记忆为关键词召回原型，非语义向量检索；存储为本地 JSONL。
- **真实 key 联调已完成**（DeepSeek / OpenAI / 本地 Ollama）；Mock 端到端始终可用。
- compaction 的 token 估算为启发式（CJK 逐字 + 4 字符/token），非模型真实 tokenizer；摘要收敛校验以该估算为准。
- session-query 的全文检索为**会话内存倒排索引**（字母数字词 + CJK 单字），与 dsh 的全文索引/嵌入基础设施不同；`readTitle*` 因依赖 dsh-session-title 未实现。
- terminal 内置工具为**持久 Popen 会话**（非 PTY）：无信号处理、无交互式提示符检测，与 dsh 的 `terminal-bash`（伪终端）有差异。
- `pyproject.toml` 仅声明 `httpx` 为必需依赖；`zstandard`（sqlite 可选压缩）、`websockets`（Web 网关）为应用层依赖，测试依赖（`pytest`）放在可选 `dev` 分组。
- Windows 上子进程类测试（MCP stdio）在解释器退出期可能打印良性管道 GC 噪声。

# 架构总览与关键不变量

> 本文件从 `README.md` 拆出，集中收录「架构总览图」与「承重不变量」。改任何包前先读不变量一节。

## 架构总览

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

## 架构关键不变量（承重，防回归）

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

# 路线图（Roadmap）

> 本文件说明 dsh-py 的**已完成里程碑**与**未来可选方向**。
> 逐包实现状态见 [`STATUS.md`](STATUS.md)；与 dsh 的差异见 [`DIFFERENCES.md`](DIFFERENCES.md)。

## 当前状态（2026-09）

在**定义范围内**（Python 后端，不含第三方应用接入 / 前端 client / 云端 e2b / Windows ACL 沙箱），复刻**已完成**：

- 第 0/1/2/4 层全部完成（cordis 内核、Loader/Boot、三 seam、应用层）。
- 第 3 层支撑服务 **45 个功能包代码落地**（含 web / skill / workspace / preset / interaction / subagent-acp 家族，均端到端冒烟）。
- **89 个测试模块全绿**（约 620+ 断言）。

## 已完结里程碑

- [x] cordis 内核（Fiber / 作用域树 / schema / inject 拓扑 / 内置 logger·reflect·registry）
- [x] Loader/Boot 多 layer 装配 + 热重载 + env 插值
- [x] 三 seam（Session 持久化/投影/查询 · Agent Inbox/取消/resume · LLM 合并/重试/多路由适配器）
- [x] 支撑服务 45 包（system-prompt / tools / settings / credentials / subagent / mcp / compaction / fs·shell·terminal / guard / hooks / schedule / todo / attachment / feedback / storage / spill / identity / jobs / context·goal 家族 / plan / workflow 编排引擎 / subprocess / typert / invariants / web / skill / workspace / preset / interaction / subagent-acp）
- [x] 应用层（CLI / 进程内 SDK / 跨进程 JSON-RPC SDK / WebSocket 网关 / 内置 Web UI）
- [x] `host` 后端子服务（webserver / plugin-inventory / loader）
- [x] 网关令牌鉴权
- [x] session-title / tool-fs-search / tool-str-replace-editor / hooks 桥（cc·codex）/ shell-env / session-query-sqlite / tool-session-query / fs-sandbox / fs-observation-policy / PowerShell 家族 / bash-sandbox
- [x] 遥测（session-telemetry + OTel 后端）

## 未来可选方向（非阻塞，按社区需求排序）

以下均为**原范围之外**或**增强项**，是否实现取决于贡献者兴趣：

- [ ] `e2b` 后端（`fs-e2b` / `subprocess-e2b`）—— 需 e2b 账号 + 官方 SDK
- [ ] `sandbox-windows-acl` —— Windows 原生 ACL FFI 沙箱执行体
- [ ] Web UI 增强：附件上传、模型选择、设置页真实读写、主题切换（浅/深）、富块渲染（Diff/Read/Search/Web/Terminal/JsonTree）、i18n
- [ ] 跨语言 client 完整版（TS/React 生态，对标 dsh 的 client）
- [ ] 长记忆从「关键词召回原型」升级为语义向量检索
- [ ] token 估算从启发式升级为真实 tokenizer
- [ ] workflow 引擎补 worker_threads 等价强杀与 syncTimeoutMs 语义
- [ ] CI：把 89 个契约单测接入 GitHub Actions
- [ ] 补充边界用例与更多端到端集成测试

# Windows Symphony Runner

Runner 是 Python 实现的 Windows 原生 Symphony 调度层，直接驱动 Windows 版 `codex app-server`，不依赖 WSL、Elixir 或 Mix。

## 单 Issue 执行语义

每次 Tick：

1. 注册并上报 Worker 心跳；
2. 通过 Tracker Adapter 按 `active_states` 获取标准化 Issue；
3. 按 `required_labels`、`dispatchable`、阻塞关系和状态并发限制筛选；
4. 在全局并发上限内原子 Claim；
5. 创建或复用 `workspace.root/<issue-identifier>`；
6. 首次创建时 Clone 并 checkout Issue 固定的 40 位 commit；
7. 注入通用 Agent 的 Skill allowlist；
8. 启动 `codex app-server`，创建或恢复 Thread；
9. 在一个 Attempt 与 Lease 中连续运行多个 Turn；
10. 直到 Agent 完成、请求人工输入、报告阻塞或 Runner 需要重试；
11. 恢复工作区内临时注入的 Agent 资产，但保留业务变更和 Thread 上下文。

Runner 的 Tracker 边界使用 `fetch_issues_by_states` 与 `fetch_issues_by_ids`。Control Plane Adapter 负责把本地记录标准化为 `id`、`identifier`、`state`、`labels`、`blocked_by`、`native_ref` 和 `dispatchable`；最终是否调度由 Runner 决定。

普通 Turn 完成不会切换 Issue 状态，也不会创建新 Attempt。达到单进程 `max_turns` 时才短暂释放到重试队列，下一 Attempt 仍复用同一 Workspace 和 Thread，从剩余工作继续。

## 通用 Agent

`WORKFLOW.md` 只有一个 `agent` 配置：并发数、最大 Turn、Skill、沙箱、网络、模型与推理强度。不存在 Profile 匹配、Role 路由或 Stage DAG。Issue 无论是需求、Bug、分析或测试，都由该 Agent 根据描述和验收标准自行组织完整工作。

Agent 可调用六个绑定当前 Claim 的工具：

- `issue_get`
- `issue_add_event`
- `issue_add_artifact`
- `issue_request_human`
- `issue_complete`
- `issue_block`

工具参数不能指定其他 Issue，Claim Token 不会进入 Codex 子进程环境。

## Workspace 与安全

Workspace 是 Issue 级持久目录。`after_create` 只在目录首次创建时执行，Clone 失败会删除未运行过 Agent 的半成品目录。所有后续 Attempt 都复用该目录。

正式配置默认 `danger-full-access`，但 Prompt 仍禁止 Push、PR/MR、Merge、发布、生产凭据和破坏性清理；这些操作由控制面人工交付门禁执行。运行结束后恢复临时 `.agents`/`.symphony` 资产，避免把 Skill 注入内容提交到业务仓库。

Runner 会记录 Attempt、Thread、Turn Count 和归一化执行事件，不保存模型隐藏推理文本。

# Fshows Symphony 实施方案

更新时间：2026-08-05

## 1. 目标模型

本项目直接采用 OpenAI Symphony 的核心调度抽象，不再兼容此前的五角色交付流水线。

```text
Issue（唯一调度单位）
├── Persistent Workspace（唯一工作区和变更集）
├── Attempt 1
│   ├── Thread（可恢复）
│   ├── Turn 1..N
│   └── Structured Events
├── Attempt 2（异常/进程边界后继续，复用 Workspace 与 Thread）
├── Human Decisions
├── Artifacts
└── Delivery Gate（Review → Commit → PR → Merge）
```

删除的概念：`Feature`、`WorkItem`、阶段依赖 DAG、固定 Agent Role/Profile、阶段 Handoff、阶段看板和 Elixir 兼容适配层。

## 2. 核心流程

```text
UI 手工创建 Issue
→ Runner 领取 Issue
→ 通用 Coding Agent 在同一会话中分析、实现、测试
→ 必要时请求人工决策并恢复原 Thread
→ Agent 提交完整结果
→ 人工最终验收
→ 控制面生成本地 Commit
→ 人工授权 Push / 创建 PR
→ 确认 PR 合并
→ Issue Done
```

Agent Profile 只剩一个 `coding_agent` 配置。Skill 是这个 Agent 的能力集合，不再用 Skill 或 Role 拆分生命周期。

## 3. 状态机

| 状态 | 含义 |
|---|---|
| `ready` | 可被 Runner 领取 |
| `running` | 一个 Agent Attempt 正在处理完整 Issue |
| `retry_queued` | Runner 异常或进程 Turn 上限后的短暂续跑等待 |
| `needs_human` | 等待人工决策，Workspace 与 Thread 保留 |
| `blocked` | 存在真实外部阻塞 |
| `reviewing` | 完整实现与测试已提交，等待一次最终验收 |
| `awaiting_publish` | 已验收并生成本地 Commit，等待发布授权 |
| `pr_open` | PR 已创建，等待合并 |
| `done` | PR 已确认合并 |
| `cancelled` | 人工取消 |

Turn 是 Attempt 内部事件，不驱动宏观状态来回切换。

## 4. 已完成

- [x] SQLite v2 单一基线迁移与 Issue 数据模型；
- [x] Issue CRUD、Candidates、原子 Claim、Lease、Heartbeat、Retry；
- [x] Attempt、Thread、Turn Count 与结构化执行事件；
- [x] 人工决策、Artifact 与完整审计事件；
- [x] 单 Agent `WORKFLOW.md` 与 Windows Runner；
- [x] 同一 Attempt 多 Turn、跨 Attempt Thread 恢复；
- [x] Issue 级持久 Workspace 与临时 Skill 资产恢复；
- [x] 一次最终 Review、本地 Commit、Push/PR/Merge 人工门禁；
- [x] Issue/Agent/Attempt 中心 UI；
- [x] 删除 Feature/WorkItem/Profile/Handoff/Elixir 兼容层；
- [x] Active Run Reconciliation：刷新运行中 Issue、终止失效/终态 Run、检测 Codex 事件停滞；
- [x] Workspace 启动清理与 `before_remove` 生命周期 Hook；
- [x] Turn 间重新确认 Claim，`turn_timeout_ms` 按消息静默时间重置；
- [x] `WORKFLOW.md` 热更新、运行中配置快照和 Last-Known-Good 回退；
- [x] 新协议、脚本和自动化回归测试。

## 5. 后续任务

1. Workspace Retention：支持按保留期延迟清理终态 Issue，并把清理结果登记为审计事件。
2. 运行指标：Token、Rate Limit、Turn Count、耗时和错误聚合。
3. Tracker 标准兼容层：在不恢复本地 WorkItem 模型的前提下，适配标准 Issue Tracker/CLI/API。

这些任务不能重新引入固定生命周期或阶段角色；它们都应围绕 `Issue → Agent Session → Workspace` 的 Symphony 调度模型扩展。

## 6. 验收标准

- 新建一个 Issue 后无需人工切换看板状态即可自动启动；
- Agent 在同一 Thread 连续多 Turn，Issue 全程保持 `running`；
- 人工问答后恢复同一 Thread 和 Workspace；
- 分析、代码、测试均出现在同一个业务变更集中；
- Agent 完成后只进入一次 `reviewing`；
- 未授权时不会 Push 或创建 PR；
- PR 未合并时不能标记 `done`；
- 数据库和 API 中不存在 Feature、WorkItem、Agent Profile 或 Handoff 表/端点。

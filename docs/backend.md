# Control Plane 后端

后端采用 FastAPI、SQLAlchemy、Alembic 和 SQLite。v2 是不兼容重建：旧 Feature、WorkItem、依赖、角色 Profile 和 Handoff 表均已移除。

## 数据模型

- `issues`：需求、不可变仓库起点、状态、Claim、重试和交付信息；
- `issue_events`：生命周期审计；
- `issue_artifacts`：可选的仓库相对产物登记；
- `agent_attempts`：每次 Claim 的配置快照、Thread、最新 Turn 和 Turn Count；
- `agent_attempt_events`：命令、工具、消息和文件变更等结构化执行事件；
- `human_decisions`：Agent 请求的人工输入；
- `workers`：Runner 心跳和当前活跃 Issue。

## 主要 API

```text
POST /api/issues
GET  /api/issues
GET  /api/issues/candidates
GET  /api/issues/{id}
PATCH /api/issues/{id}

POST /api/issues/{id}/claim
POST /api/issues/{id}/heartbeat
POST /api/issues/{id}/release
POST /api/issues/{id}/status
POST /api/issues/{id}/attempt-context
GET  /api/issues/{id}/attempts
POST /api/issues/{id}/attempts/{attempt_id}/events
GET  /api/issues/{id}/attempts/{attempt_id}/events
POST /api/issues/{id}/events
GET  /api/issues/{id}/events
POST /api/issues/{id}/artifacts
POST /api/issues/{id}/decisions
GET  /api/issues/{id}/decisions

POST /api/issues/{id}/delivery
GET  /api/agent-runtimes
POST /api/maintenance/tick
```

Claim 使用版本号做原子 compare-and-set。Claim Token 只在成功领取时返回一次；普通查询只暴露 Worker 和到期时间。Lease 过期后进入 `retry_queued`，维护任务到期后重新置为 `ready`。

## 生命周期

```text
ready → running
running → reviewing | needs_human | blocked | retry_queued | cancelled
needs_human → ready
blocked → ready
retry_queued → ready
reviewing → ready | awaiting_publish
awaiting_publish → pr_open
pr_open → done
```

`reviewing` 是整个 Issue 的一次最终人工验收，不是阶段审批。通过后在 Issue Workspace 生成本地 Commit；Push/PR 和合并确认都要求显式授权。Agent 不能调用这些交付 API。

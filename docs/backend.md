# Control Plane 后端

后端采用 FastAPI、SQLAlchemy、Alembic 和 SQLite。v2 是不兼容重建：旧 Feature、WorkItem、依赖、角色 Profile 和 Handoff 表均已移除。

## 数据模型

- `issues`：标准化 Issue 标识、标签、阻塞关系、调度资格、不可变仓库起点、Claim、重试和交付信息；
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

`GET /api/issues?state=ready&state=running` 和 `GET /api/issues?id=...` 构成 Control Plane Tracker Adapter 的标准读取内核。API 同时返回 `identifier`、`state`、`labels`、`blocked_by`、`native_ref`、`dispatchable` 等 Symphony 标准化字段。

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

`reviewing` 是整个 Issue 的一次最终人工验收，不是阶段审批。通过后在业务 Git 仓库生成本地 Commit；Push、GitHub PR/GitLab MR 和合并确认都要求显式授权。交付层只接受 Workspace 根目录或其直接 `repo/` 子目录中的真实 Git 根，禁止 Git 向父目录穿透。Agent 不能调用这些交付 API。

GitLab 未配置 `ACP_GITLAB_TOKEN` 时使用 Git Push Options 原子 Push 并创建 MR；配置具备 `api` Scope 的 Token 后，交付层通过 GitLab API 幂等查找/创建 MR，并可核验合并状态。仅有 Git 写权限但没有 API Scope 的凭据不能用于 GitLab API。

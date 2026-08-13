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
POST /api/issues/{id}/archive
GET  /api/agent-runtimes
GET  /api/v1/state
GET  /api/v1/{issue_identifier}
POST /api/workers/{worker_id}/heartbeat
POST /api/maintenance/tick
```

Claim 使用版本号做原子 compare-and-set。Claim Token 只在成功领取时返回一次；普通查询只暴露 Worker 和到期时间。Lease 过期后进入 `retry_queued`，维护任务到期后重新置为 `ready`。

`GET /api/issues?state=ready&state=running` 和 `GET /api/issues?id=...` 构成 Control Plane Tracker Adapter 的标准读取内核。API 同时返回 `identifier`、`state`、`labels`、`blocked_by`、`native_ref`、`dispatchable` 等 Symphony 标准化字段。

Worker Heartbeat 携带 Runner 内存中生成的权威 `RuntimeState` 快照。Control Plane 缓存快照及采集时间；`GET /api/agent-runtimes` 对活跃 Agent 优先返回该快照中的 Phase、Session、Codex PID、最近事件、耗时、Token 和 Workspace，快照缺失或过期时才回退到 SQLite 历史状态。标准 `/api/v1/state` 聚合 Running、Retry Queue、Token、运行时长和最新 Rate Limit；没有新鲜 Worker Snapshot 时返回 `timeout` 或 `unavailable`。

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

`POST /api/issues/{id}/archive` 是适用于任意状态的显式强制清理门禁。未完成 Issue 会先原子地转为 `cancelled` 并结束当前 Attempt；系统等待 Runner 释放后永久删除本地 Workspace，保留 Issue、Attempt、Event 和 Artifact 登记记录，并写入 `archived_at` 与归档审计事件。若 Runner 未能及时退出，本次请求会停止在已取消状态并拒绝删除目录，操作者可稍后安全重试。

Issue 详情中的“改动总结”优先展示 Agent 最后一条完成说明，直接描述本次功能、行为、测试和文档大概改了什么；没有 Agent 说明时回退到本地提交信息。文件增删改、行数、目录和提交等 Git 统计收进次级详情。Workspace 存在时 Review API 返回实时总结；验收或强制归档时同步持久化到 Issue，因此删除 Workspace 后仍可追溯本次改动范围。

控制台 Issue 列表默认隐藏已归档或 `cancelled` 的任务；需要追溯历史时可开启“显示归档/取消”，该偏好保存在本机浏览器中。

GitLab 未配置 `ACP_GITLAB_TOKEN` 时使用 Git Push Options 原子 Push 并创建 MR；配置具备 `api` Scope 的 Token 后，交付层通过 GitLab API 幂等查找/创建 MR，并可核验合并状态。仅有 Git 写权限但没有 API Scope 的凭据不能用于 GitLab API。

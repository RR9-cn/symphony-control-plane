# 最小控制面后端

第二步后端使用 FastAPI、SQLAlchemy Async 和 SQLite，实现工作项持久化、依赖调度、原子领取、Lease、事件、Artifact 与人工决策。

## 本地启动

```powershell
python -m pip install -r requirements-dev.txt
$env:PYTHONPATH = "src"
$env:ACP_API_TOKEN = "replace-with-a-random-host-only-token"
alembic upgrade head
python -m uvicorn control_plane.app:app --host 127.0.0.1 --port 8080
```

默认数据库位于 `data/control-plane.db`。可复制 `.env.example` 修改路径和维护周期。管理看板位于 `/`，Swagger UI 位于 `/docs`；使用浏览器打开前请遵守当前工作区的浏览器授权规则。

看板和 API 同源部署，不需要额外前端服务或 Node 构建。启用 `ACP_API_TOKEN` 后，在看板右上角“API 凭据”中输入相同 Token；页面只把凭据保存到当前标签页的 `sessionStorage`，关闭标签页后自动清除。

## SQLite 并发策略

- 每个连接启用 `foreign_keys=ON`、WAL、`synchronous=NORMAL` 和 5 秒 `busy_timeout`。
- Claim 使用单条条件 `UPDATE`，同时校验 `id`、`status=ready`、`version` 和依赖全部完成。
- 只有更新行数为 1 的请求获得随机 claim token；数据库只保存 token 的 SHA-256 摘要。
- Heartbeat、运行期状态转换、事件、Artifact 和人工输入请求必须携带未过期 token。
- 后台维护任务回收过期 Lease，将任务送入 `retry_queued`，退避到期且依赖满足后恢复为 `ready`。
- SQLite 适合单机控制面和同一数据库文件上的多个 Worker。不要把数据库文件放在不可靠的网络文件系统上；需要多节点数据库服务时应单独规划迁移。

## API

核心接口：

```text
POST   /api/features
GET    /api/features
GET    /api/features/{id}

POST   /api/intake/manual/issues/preview
POST   /api/intake/manual/issues

POST   /api/work-items
GET    /api/work-items
GET    /api/work-items/candidates
GET    /api/work-items/{id}
PATCH  /api/work-items/{id}

POST   /api/work-items/{id}/claim
POST   /api/work-items/{id}/heartbeat
POST   /api/work-items/{id}/release
POST   /api/work-items/{id}/status

POST   /api/work-items/{id}/events
GET    /api/work-items/{id}/events
POST   /api/work-items/{id}/artifacts
POST   /api/work-items/{id}/decisions
GET    /api/work-items/{id}/decisions
GET    /api/work-items/{id}/attempts

GET    /api/agent-profiles

POST   /api/maintenance/tick
GET    /health
```

手工 Issue Intake 接收 Feature ID、需求说明、验收标准、仓库地址、Base Branch 和
完整 40 位 Git commit。本地仓库可先调用 `POST /api/repositories/resolve-head`，
传入绝对路径读取当前 `HEAD`，避免用户手工复制 commit。该接口不访问远程仓库，
也不接受相对路径。`preview` 只生成固定五阶段拆分草案，不写数据库；确认接口在一个
事务中创建 Feature、五个串行依赖的 WorkItem 和审计事件，只把第一个
Solution Architect 工作项置为 `ready`。本期不连接或同步外部 Issue 平台。

`claim` 请求兼容协议示例中的 camelCase：

```json
{
  "workerId": "symphony-01",
  "expectedVersion": 12,
  "leaseSeconds": 300,
  "profile": {
    "name": "backend_builder",
    "version": 3,
    "config": {
      "profile_name": "backend_builder",
      "profile_version": 3,
      "prompt_hash": "..."
    }
  }
}
```

Profile 字段由 Windows Runner 生成；旧客户端可以省略。控制面会登记不可变的 Profile 版本并把配置快照写入本次 Agent Attempt。同名同版本的不同配置会返回冲突。

Claim token 只在领取成功响应中返回一次。普通 WorkItem 响应仅展示 `worker_id` 和 `expires_at`，不会泄露 token。

设置 `ACP_API_TOKEN` 后，所有 `/api/*` 请求必须携带：

```http
Authorization: Bearer <token>
```

`/health` 保持公开并通过 `auth_enabled` 指示认证是否开启。生产部署必须设置高熵 Token；Symphony 端使用相同值配置 `CONTROL_PLANE_TOKEN`。该宿主 Token 不应传递给 Codex 子进程。

Agent 报告完成前必须先登记：

```text
orchestration/handoffs/<work-item-id>.yaml
```

否则 `running → stage_review` 会被拒绝。

## 人工模拟

服务启动后运行：

```powershell
python scripts/simulate_api.py --token $env:ACP_API_TOKEN
```

脚本会执行：创建 Feature 和 WorkItem、Claim、Heartbeat、写事件、请求人工决策、恢复 Ready、重新 Claim、登记 Handoff、进入 StageReview 并完成。

## 测试

```powershell
$env:PYTHONPATH = "src"
pytest -q
python scripts/validate_protocol.py
alembic check
```

当前版本已实现 Symphony 宿主 Bearer 认证、Claim Token 隔离、Agent Profile 版本登记和 Attempt 配置快照。服务默认仅监听 `127.0.0.1`；通过非 loopback 网络部署时应使用 HTTPS，并继续通过网络策略限制访问来源。

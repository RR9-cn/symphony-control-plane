# Fshows Symphony Control Plane

Windows 原生、SQLite 持久化的 OpenAI Symphony 风格 Agent 调度器。调度单位只有 `Issue`，项目仓库是执行配置、规则和能力的唯一来源。

```text
登记本机项目仓库
→ 校验并快照仓库 WORKFLOW.md
→ 手工创建归属于该项目的 Issue
→ 通用 Coding Agent 领取
→ 同一 Workspace / Attempt / Thread 内多 Turn 完成分析、实现与测试
→ 一次最终人工验收
→ 本地 Commit
→ 人工授权 Push 与创建 PR
→ 确认 PR 已合并
→ Done
```

## 组件

- Control Plane：FastAPI + SQLite，保存 Project Registry、Workflow Snapshot、Issue、Claim/Lease、Attempt/Turn、Event、Artifact、人工决策和交付门禁。
- Windows Symphony Runner：每个启用项目一个隔离 Runtime，只轮询该项目的 `ready` Issue，维护持久 Workspace，启动 `codex app-server` 并执行多 Turn。
- Codex App Server：一个通用 Coding Agent 负责完整 Issue，自动读取项目仓库的 `AGENTS.md` 和 `.codex/skills`。
- UI：以 Issue 和实际 Agent Runtime 为中心，查看 Thread、Turn、事件、产物和人工门禁。

## 启动

```powershell
python -m pip install -r requirements-dev.txt
python -m alembic upgrade head
python -m uvicorn control_plane.app:app --app-dir src --host 127.0.0.1 --port 8080
```

打开 `http://127.0.0.1:8080/`。若设置 `ACP_API_TOKEN`，API 需要 Bearer Token，UI 只在当前标签页的 `sessionStorage` 保存它。

先在 UI 登记本机 Git 仓库。缺少 `WORKFLOW.md` 时 Control Plane 会放入内置默认模板，但不会替你修改 Git 历史；检查并提交该文件后点击“重新校验”。校验通过后系统固定当前 HEAD，并可启动该项目独立 Runtime。也可手工运行某个项目：

```powershell
$env:CONTROL_PLANE_TOKEN = $env:ACP_API_TOKEN
$env:SYMPHONY_PROJECT_ID = "<project-uuid>"
$env:SYMPHONY_PROJECT_REPOSITORY = "D:\path\to\project"
$env:SYMPHONY_PROJECT_DEFAULT_BRANCH = "master"
fshows-symphony-windows D:\path\to\project\WORKFLOW.md
```

Issue 创建接口只接收 `project_id`，不能覆盖仓库、分支、Commit 或 Workflow。项目的 `WORKFLOW.md`、`AGENTS.md` 与 `.codex/skills` 随代码评审和版本控制。

## 验证

```powershell
python scripts/validate_protocol.py
python scripts/validate_workflow.py .\WORKFLOW.md
python -m pytest -q
```

详细设计见 [V2 项目仓库方案](docs/v2-project-repository-workflow.md)、[实施方案](docs/implementation-plan.md)、[后端协议](docs/backend.md) 和 [Windows Runner](docs/windows-runner.md)。

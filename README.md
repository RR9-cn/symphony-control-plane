# Fshows Symphony Control Plane

Windows 原生、SQLite 持久化的 OpenAI Symphony 风格 Agent 调度器。系统不再维护 Feature、WorkItem、固定角色或五阶段流水线；调度单位只有 `Issue`。

```text
手工创建 Issue
→ 通用 Coding Agent 领取
→ 同一 Workspace / Attempt / Thread 内多 Turn 完成分析、实现与测试
→ 一次最终人工验收
→ 本地 Commit
→ 人工授权 Push 与创建 PR
→ 确认 PR 已合并
→ Done
```

## 组件

- Control Plane：FastAPI + SQLite，保存 Issue、Claim/Lease、Attempt/Turn、Event、Artifact、人工决策和交付门禁。
- Windows Symphony Runner：轮询 `ready` Issue，维护持久 Workspace，启动 `codex app-server` 并执行多 Turn。
- Codex App Server：一个通用 Coding Agent 负责完整 Issue，可使用子 Agent 和配置的 Skill。
- UI：以 Issue 和实际 Agent Runtime 为中心，查看 Thread、Turn、事件、产物和人工门禁。

## 启动

```powershell
python -m pip install -r requirements-dev.txt
python -m alembic upgrade head
python -m uvicorn control_plane.app:app --app-dir src --host 127.0.0.1 --port 8080
```

打开 `http://127.0.0.1:8080/`。若设置 `ACP_API_TOKEN`，API 需要 Bearer Token，UI 只在当前标签页的 `sessionStorage` 保存它。

Runner 读取根目录的 `WORKFLOW.md`：

```powershell
$env:CONTROL_PLANE_TOKEN = $env:ACP_API_TOKEN
fshows-symphony-windows .\WORKFLOW.md
```

也可在 UI 中启动同机托管 Runner。

## 验证

```powershell
python scripts/validate_protocol.py
python scripts/validate_skills.py
python scripts/validate_workflow.py .\WORKFLOW.md --skip-skills
python -m pytest -q
```

正式 Skill 校验还需要配置 `FSHOWS_SKILLS_REPOSITORY` 和完整 40 位 `FSHOWS_SKILLS_REVISION`。

详细设计见 [实施方案](docs/implementation-plan.md)、[后端协议](docs/backend.md) 和 [Windows Runner](docs/windows-runner.md)。

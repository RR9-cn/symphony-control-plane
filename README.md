# Fshows Agent Control Plane

围绕 OpenAI Symphony、Codex App Server 与 `fshows-skills` 建设的自托管 Agent 任务控制面。

## 目标

将系统职责拆分为四层：

- Control Plane：管理工作项、状态、依赖、人工决策和执行历史。
- Symphony：轮询、领取、路由、并发控制、重试和工作区管理。
- Codex App Server：执行具体 Agent Thread 与 Turn。
- Skill：定义不同 Agent 的专业流程、规范和工具使用方式。

第一版优先跑通：

```text
技术分析
→ 后端开发
→ Code Review
→ 测试方案
→ 测试执行
→ 人工确认
→ Done
```

## 文档

- [实施方案与任务清单](docs/implementation-plan.md)
- [调度协议 v1](protocol/README.md)
- [最小控制面后端](docs/backend.md)
- [Windows 原生 Symphony Runner](docs/windows-runner.md)
- [可选 Symphony Elixir Adapter](integrations/symphony_elixir/README.md)

## 管理看板

启动控制面后访问根地址即可打开内置看板：

```powershell
python -m uvicorn control_plane.app:app --app-dir src --host 127.0.0.1 --port 8080
```

```text
http://127.0.0.1:8080/
```

看板提供手工 Issue 录入与五阶段拆分预览、Feature 筛选、WorkItem 状态列、可展开的 Attempt 执行详情、Event 时间线、Artifact、人工决策、阶段审批、返工、解阻、取消和重试维护操作。执行详情展示 Turn、Agent 消息、命令、工具和文件变更，不保存模型推理文本。后端启用 `ACP_API_TOKEN` 时，可在页面右上角录入 Token；Token 只保存在当前浏览器标签页的 `sessionStorage`。本期不连接外部 Issue 平台。

## 协议校验

安装开发依赖后，可在仓库根目录执行：

```powershell
python -m pip install -r requirements-dev.txt
python scripts/validate_protocol.py
python scripts/validate_skills.py
python scripts/validate_workflow.py .\WORKFLOW.md
python -m pytest -q
```

正式 Workflow 验收需要设置 `CONTROL_PLANE_TOKEN`、`FSHOWS_SKILLS_REPOSITORY`
和完整 40 位 `FSHOWS_SKILLS_REVISION`。验收只读取配置、Profile Prompt 和固定
Skill revision，不领取任务，也不启动 Codex。

## 当前状态

实施方案前六步、第七步最小看板和第八步正式 Workflow 已实现。后端采用 SQLite，提供带 Bearer 认证的 WorkItem API、依赖候选查询、原子 Claim、Lease/Heartbeat、状态事件、Artifact、Agent Attempt、Worker 心跳和人工决策。Windows 原生 Python Runner 可直接启动 Windows 版 `codex app-server`，实现安全工作区、PowerShell Hook、Profile 路由与限流、Heartbeat、单 Attempt 多 Turn、异常指数退避、子 Agent Thread 连续性和六个受限 Agent Tool，不依赖 WSL、Elixir 或 Mix。根目录 `WORKFLOW.md` 固化五个 Profile、Workspace Clone/Commit Checkout、固定 Skill revision 和无人值守规则，并由只读验收器阻止 Role、Prompt 与 Skill 漂移。Skill 使用固定 Git commit、Profile allowlist 物理注入、兼容性校验、Codex 用户 Skill 隔离、`skills/list` 启动验证和 revision/content hash 执行快照。内置响应式看板以 Agent 状态为中心，支持同机 Runner 启停、Thread/Turn 观测和人工门禁；手工 Issue 录入可从本地仓库自动读取不可变 HEAD。Elixir 叠加层保留为可选兼容参考。

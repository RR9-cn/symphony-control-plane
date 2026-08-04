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

## 协议校验

安装开发依赖后，可在仓库根目录执行：

```powershell
python -m pip install -r requirements-dev.txt
python scripts/validate_protocol.py
python scripts/validate_skills.py
python -m pytest -q
```

## 当前状态

实施方案前五步已实现。后端采用 SQLite，提供带 Bearer 认证的 WorkItem API、依赖候选查询、原子 Claim、Lease/Heartbeat、状态事件、Artifact、Agent Attempt 和人工决策。Windows 原生 Python Runner 可直接启动 Windows 版 `codex app-server`，实现安全工作区、PowerShell Hook、Profile 路由与限流、Heartbeat、指数退避和六个受限 Agent Tool，不依赖 WSL、Elixir 或 Mix。第五步增加固定 Git commit 的 Skill 仓库、Profile allowlist 物理注入、Skill 兼容性校验、Codex 用户 Skill 隔离、`skills/list` 启动验证、Skill revision/content hash 执行快照和 `NeedsHuman` 恢复闭环。Elixir 叠加层保留为可选兼容参考。

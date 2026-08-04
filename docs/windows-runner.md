# Windows 原生 Symphony Runner

第三步主链采用 Python 实现语言无关 Symphony SPEC 的 Windows 原生运行层，不依赖 WSL、Elixir 或 Mix。Runner 直接在每个 WorkItem 工作区启动 Windows 版 `codex app-server`，通过默认 stdio JSONL 协议驱动 Thread 和 Turn。

## 架构

```text
SQLite / FastAPI Control Plane
        ↑ Bearer + Claim Token
WindowsSymphony
├── ControlPlaneTracker
├── WorkspaceManager（PowerShell Hooks）
└── CodexAppServer（Windows 子进程）
        ↕ JSONL over stdio
    codex app-server
```

Control Plane Bearer Token 和 WorkItem Claim Token 只由 Runner 持有。启动 Codex 子进程前会删除 `ACP_API_TOKEN`、`CONTROL_PLANE_TOKEN` 和配置引用的 Token 环境变量。

## 前置条件

- Windows 10/11；
- Python 3.11 或更高版本；
- Windows 版 Codex CLI；
- PowerShell 5.1 或 PowerShell 7；
- 目标仓库所需的 Git、构建和测试工具。

确认 Codex 已安装和登录：

```powershell
codex --version
codex login status
codex app-server --help
```

Codex App Server 协议说明见 [OpenAI Codex App Server](https://learn.chatgpt.com/docs/app-server)。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

仓库根目录已经提供正式配置：

```powershell
Get-Item .\WORKFLOW.md
```

`WORKFLOW.windows.example.md` 只用于部署方创建其他变体，不再作为默认启动入口。
正式 `WORKFLOW.md` 使用 Symphony SPEC 的 YAML Front Matter 和 Liquid Prompt。
配置读取严格模式：缺少环境变量、未知 Prompt 变量、不安全 HTTP 地址、重复角色匹配、不存在或越界的 Prompt 文件和越界参数都会在启动前失败。

首次创建 WorkItem Workspace 时，正式 Workflow 会读取
`repository.url` 和完整 40 位 `repository.commit`，Clone 仓库后以 detached
HEAD 检出该 commit 并核对实际 HEAD。Continuation、NeedsHuman 恢复和 Rework
复用现有 Workspace，不会重置 Agent 已完成的工作。

## Agent Profile 路由

`agent_profiles` 是必填配置。每个 Profile 以 `match.agent_role` 精确匹配一个 WorkItem，并固定版本、Prompt、Skill 声明、沙箱、网络策略、并发上限和最大 Turn 数。例如：

```yaml
agent_profiles:
  backend_builder:
    version: 1
    match:
      agent_role: backend_builder
    prompt_file: workflows/backend-builder.md
    skills:
      - fskill-code-java-guide
      - fskill-knowledge-query
    sandbox: workspace-write
    network_access: true
    max_concurrent_agents: 4
    max_turns: 20
```

Profile 在 Claim 前解析。没有唯一匹配项的 WorkItem 会进入 `Blocked`，错误码为 `agent_profile_configuration_error`，不会静默使用其他角色。Profile Prompt 在 Runner 加载配置时读入内存；运行中的 Session 不受文件热更新影响。

每次 Claim 会在 SQLite 中登记或复用对应的 `agent_profiles(name, version)`，并将以下无凭据快照写入 `agent_attempts.profile_snapshot`：

```text
profile_name / profile_version / agent_role
prompt_file / prompt_hash / skills / model
effort / sandbox / network_access / max_concurrent_agents / max_turns
```

未配置 `agent_profiles.<name>.sandbox` 时，Profile 默认使用
`danger-full-access`；未配置 `codex.thread_sandbox` 或
`codex.turn_sandbox_policy` 时也默认使用完全访问。需要收紧角色权限时，应在
Profile 中显式设置 `workspace-write` 或 `read-only`，Profile 配置优先于全局默认值。

同一 Profile 名称和版本若出现不同快照会作为配置冲突阻断执行，配置变更必须递增版本。可通过以下接口还原执行历史：

```http
GET /api/agent-profiles
GET /api/work-items/{id}/attempts
```

当 Codex Turn 已结束但 WorkItem 尚未生成 Handoff 时，Runner 会把 `thread_id`
写入当前 `agent_attempts`。下一次 Claim 在 Profile 名称和版本一致时返回该线程，
Runner 使用 `thread/resume` 继续同一个上下文，因此子 Agent 的已完成结果不会因
`--once` 进程退出而丢失。`StageReview -> Rework -> Ready` 同样续接原线程；最大
Turn 数按 SQLite 中同线程的历史 Attempt 计算，不依赖单个 Runner 进程内存。

## Skill 仓库与版本固定

Runner 要求配置固定的 Git revision，不会直接复制不断变化的全局 Skill 目录：

```yaml
skill_repository:
  url: $FSHOWS_SKILLS_REPOSITORY
  revision: $FSHOWS_SKILLS_REVISION
  skills_path: skills
```

`revision` 必须是完整的 40 位 commit SHA。`url` 可以是本地 Git 仓库路径，也可以是 `https`、`ssh`、`git` 或 SCP 风格 Git 地址；URL 不允许内嵌密码或查询参数。首次启动会将指定 commit checkout 到宿主临时缓存，后续执行校验缓存的实际 `HEAD`。

当前仓库已从 `D:\saasProject\fshows-skills` 复制五个 Profile 使用的 7 个 Skill 到 `skills/`。提交这些副本后，可将当前仓库及包含它们的 commit 作为固定 Skill 来源：

```powershell
$env:FSHOWS_SKILLS_REPOSITORY = "."
$env:FSHOWS_SKILLS_REVISION = git -C $env:FSHOWS_SKILLS_REPOSITORY rev-parse HEAD
```

复制范围为：`fskill-analysis-tech`、`fskill-knowledge-query`、`fskill-tools-db`、`fskill-code-java-guide`、`fskill-code-review`、`fskill-test-explore` 和 `fskill-test-verify`。未复制源仓库 `delete/` 和当前五个 Profile 未引用的 Skill。

每个 Skill 必须包含带 `name` 和 `description` 的 `SKILL.md` Front Matter。可在 Front Matter 中声明兼容性信息：

```yaml
metadata:
  fshows:
    artifact_paths:
      - test/results/*.md
      - orchestration/handoffs/*.yaml
    human_confirmation:
      - external_publish
    external_writes:
      - git_push
    required_tools:
      - git
    required_credentials:
      - EXAMPLE_HOST_ONLY_TOKEN
    required_skills:
      - fskill-knowledge-query
```

Runner 会检查 Markdown 中的 `references/`、`scripts/`、`assets/` 和相对链接，拒绝断裂引用、符号链接、旧 Artifact 路径、未安装工具、缺少的宿主凭据，以及未包含在同一 Profile allowlist 内的 `required_skills`。声明外部写操作时必须同时声明人工确认点。

Codex 启动前，Runner 会用固定 revision 的副本完整替换：

```text
<workspace>/.agents/skills/
```

并生成 `.agents/skills.lock.json`。Codex 子进程使用隔离的用户 Home，避免加载 `$HOME/.agents/skills`；随后调用 `skills/list`，确保 Profile 声明的 Skill 来自当前 Workspace，并拒绝额外的 `fskill-*`。OpenAI 内置的 System Skill 仍由 Codex 自身提供，不属于业务 Profile allowlist。

执行快照为每个 Skill 记录固定 Git revision 和目录内容 SHA-256。Skill 所需凭据的环境变量名称可进入快照，值不会写入快照、Prompt、Tool Schema 或 Codex 子进程环境。

## 人工确认协议

Profile Prompt 统一要求 Skill 遇到人工确认点时调用 `work_item_request_human` 并结束当前 Turn。Runner 也会把 App Server approval/input request 转换为同一流程：

```text
Running → NeedsHuman（清除 Claim）
→ GET /api/work-items/{id}/decisions
→ 人工 resolve decision
→ Ready → Runner 重新领取
```

因此 Agent 不会在 Codex Turn 内持续等待用户输入。

## 启动

先准备宿主环境变量。Token 只提供给 Control Plane 和 Runner；固定 Skill revision
必须是包含当前 Profile Skill 的完整 Git commit：

```powershell
$env:ACP_API_TOKEN = "replace-with-a-random-host-only-token"
$env:CONTROL_PLANE_TOKEN = $env:ACP_API_TOKEN
$env:SYMPHONY_WORKER_ID = "windows-symphony-01"
$env:FSHOWS_SKILLS_REPOSITORY = (Get-Location).Path
$env:FSHOWS_SKILLS_REVISION = git rev-parse HEAD
```

启动任何 Worker 前先执行只读验收。该命令严格渲染五个 Profile Prompt，并 checkout
固定 Skill revision 做兼容性检查；不会 Claim WorkItem、启动 Codex 或修改目标项目：

```powershell
python scripts/validate_workflow.py .\WORKFLOW.md
```

先启动 SQLite 控制面：

```powershell
$env:PYTHONPATH = "src"
alembic upgrade head
python -m uvicorn control_plane.app:app --host 127.0.0.1 --port 8080
```

另开 PowerShell 启动 Runner：

```powershell
$env:PYTHONPATH = "src"
python -m symphony_windows .\WORKFLOW.md
```

也可以在控制面 Agent 状态中心点击“启动 Runner”。该方式由控制面托管同机 Windows
Runner 进程，使用 `ACP_MANAGED_RUNNER_WORKFLOW` 和
`ACP_MANAGED_RUNNER_WORKER_ID` 配置 Workflow 与 Worker ID；UI 停止会先写入优雅
停止请求，超时后才终止进程。外部宿主机仍使用上述命令独立启动。

只执行一次轮询并等待该批任务结束：

```powershell
python -m symphony_windows .\WORKFLOW.md --once
```

安装项目后也可使用入口命令：

```powershell
fshows-symphony-windows .\WORKFLOW.md
```

## 调度行为

每个 Poll Tick 执行：

1. 触发 Lease/Retry 维护；
2. 查询依赖已满足的 `Ready` WorkItem；
3. 按优先级、创建时间和 ID 排序；
4. 精确匹配 Agent Profile，并同时检查全局与 Profile 并发槽位；
5. 将 Profile 配置快照随原子 Claim 写入执行历史；
6. 创建经过 Windows 保留名、路径穿越和根目录边界校验的工作区；
7. 执行 `after_create`、`before_run` PowerShell Hook；
8. 以 Profile Prompt、模型、沙箱和网络策略启动独立 `codex app-server`，并注册六个受限工具；
9. 后台 Heartbeat，Claim 丢失时终止 Codex 进程树；
10. 完成、Blocked 或 NeedsHuman 时清除本机 Claim；
11. 普通 Turn 结束但未交接时安排 1 秒 continuation；达到 Profile `max_turns` 后阻断，异常按 10 秒指数退避；
12. 执行 `after_run` Hook，保留工作区供下一次尝试复用。

## 安全边界

- Agent Tool 始终绑定当前 WorkItem，不接受 `work_item_id`；
- Agent Tool Schema 和输出不包含 Claim Token；
- 不暴露任意 HTTP 工具；
- 非 loopback HTTP Control Plane 地址必须显式允许，生产部署优先 HTTPS；
- PowerShell Hooks 来自仓库拥有的 `WORKFLOW.md`，应只在可信仓库运行；
- 默认审批策略拒绝沙箱提升、规则绕过和 MCP elicitation，并将需要输入的任务转换为 `NeedsHuman`。

当前前五步实现覆盖核心轮询、并发、Claim、Heartbeat、工作区、Hook、Codex stdio、动态工具、失败退避、Agent Profile Router、Skill 物理注入、版本固定、兼容性校验和人工确认恢复协议。

## 验收

```powershell
python -m pytest -q
python scripts/validate_protocol.py
python scripts/validate_skills.py
ruff check src tests scripts
```

自动化测试使用假 Codex App Server 验证真实子进程 JSONL 往返，不产生模型调用或 API 费用。

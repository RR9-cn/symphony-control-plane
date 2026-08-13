# Symphony Control Plane V2：项目仓库驱动技术方案

状态：Draft
更新时间：2026-08-05

## 1. 方案结论

V2 将项目代码仓库作为配置与执行边界。每个项目仓库自行维护 `WORKFLOW.md`、`AGENTS.md`、仓库级 Codex Skills 和业务代码；Control Plane 只登记项目、管理 Issue 与运行状态，不再用中央 `WORKFLOW.md` 和中央 Skill 注入决定所有项目的执行方式。

```text
项目仓库
├── WORKFLOW.md          项目调度、Workspace Hook、Codex 参数和任务 Prompt
├── AGENTS.md            项目开发规则、安全规则和验证要求
├── .codex/skills/       仓库级 Codex Skills
└── 业务代码

Control Plane
→ 注册项目仓库
→ 加载项目 WORKFLOW.md
→ 接收属于该项目的 Issue
→ Project Runtime 调度 Issue
→ 创建 Issue Workspace
→ Clone/Checkout 项目仓库
→ 在 Workspace 内启动 Codex
→ Codex 自动发现 AGENTS.md 与仓库级 Skills
→ 单 Agent 多 Turn 完成整个 Issue
```

V2 继续坚持以下边界：

- `Issue` 是唯一调度单位；
- 一个 Issue 只有一个持久 Workspace、一个主 Thread 和连续多个 Turn；
- 不恢复 Feature、WorkItem DAG、固定角色 Agent、阶段 Handoff 或阶段看板；
- Project 只是仓库、Workflow 和运行配置的归属单位，不是新的流程引擎；
- Push、PR、Merge 和发布仍由人工交付门禁控制。

## 2. 与 OpenAI Symphony 的对应关系

官方 Symphony 将 `WORKFLOW.md` 定义为 repository-owned contract，并通过 `hooks.after_create` 等 Workspace Hook 完成仓库 Clone、Checkout 和依赖准备。标准 Issue 模型不负责携带仓库地址，Workspace 的代码来源属于 Workflow/实现配置。

V2 按这一原则调整，同时保留本项目的多项目 UI 和 SQLite Control Plane 扩展：

| 能力 | Symphony 核心语义 | 本项目 V2 |
|---|---|---|
| Workflow 所有权 | 项目仓库 | 项目仓库 |
| Issue 调度 | Tracker 范围内的 Issue | 手工 Tracker 中按 `project_id` 分区的 Issue |
| Workspace | 每个 Issue 一个目录 | 每个 Project/Issue 一个持久目录 |
| 仓库准备 | Workflow Hook，具体 VCS 策略由实现定义 | PowerShell Hook Clone 并 Checkout 固定 Commit |
| Skills | Codex/仓库资产，不属于 Symphony 核心配置 | 项目仓库 `.codex/skills`，Codex 自动发现 |
| 多项目 | 多个 Workflow/服务实例 | 一个 Supervisor 管理多个 Project Runtime |
| Web UI/数据库 | 非核心要求 | 保留为运维和人工门禁扩展 |

参考：

- <https://github.com/openai/symphony/blob/main/SPEC.md>
- <https://github.com/openai/symphony/blob/main/elixir/README.md>
- <https://github.com/openai/symphony/blob/main/elixir/WORKFLOW.md>

## 3. 目标用户流程

### 3.1 首次注册项目

```text
打开“项目”页面
→ 登记本地 Git 仓库路径
→ 系统读取项目名称、默认分支、HEAD 和 WORKFLOW.md
→ 校验 Workflow、Hooks、Codex 配置、AGENTS.md 和仓库级 Skills
→ 创建 Project Runtime
→ 项目进入 Available
```

第一期继续面向 Windows 本机开发，只接受绝对本地 Git 仓库路径。远程 URL 自动维护控制副本属于后续能力。

### 3.2 创建并执行 Issue

```text
选择项目
→ 填写标题、描述、优先级和验收标准
→ 进入 Ready 时解析并冻结项目 HEAD
→ Project Runtime 领取 Issue
→ 创建 .workspaces/<project-key>/<issue-key>
→ 执行项目 WORKFLOW.md 的 after_create
→ Clone/Checkout Issue 固定的 source_commit
→ 执行 before_run
→ 在 Workspace 内启动 codex app-server
→ Codex 读取 AGENTS.md 和 .codex/skills
→ 同一 Attempt 内连续多 Turn
→ 完成、请求人工输入、报告阻塞或进入重试
→ 完整实现进入一次 reviewing
→ 人工验收与交付门禁
```

### 3.3 Issue 创建表单

V2 表单只需要：

- Project；
- Issue ID/Identifier；
- 标题；
- 描述；
- 优先级；
- Labels；
- Blockers；
- 验收标准。

删除以下人工输入：

- 仓库本地路径或 URL；
- Base Branch；
- 不可变 Commit；
- “读取 HEAD”按钮。

这些值统一从 Project 解析，并在 Issue 进入 `ready` 时生成不可变执行快照。

## 4. 项目仓库约定

建议业务仓库结构：

```text
hengxi-cultural-tourism/
├── WORKFLOW.md
├── AGENTS.md
├── .codex/
│   └── skills/
│       ├── fskill-analysis-tech/
│       │   └── SKILL.md
│       ├── fskill-code-java-guide/
│       │   └── SKILL.md
│       └── fskill-test-verify/
│           └── SKILL.md
├── pom.xml
└── src/
```

### 4.1 `WORKFLOW.md`

项目 Workflow 继续采用 YAML Front Matter + Markdown Prompt：

```yaml
---
tracker:
  kind: fshows_control_plane
  required_labels: []
  active_states: [ready, running]
  terminal_states: [done, cancelled]

polling:
  interval_ms: 5000

workspace:
  root: D:/fshows-workspaces/hengxi-cultural-tourism

hooks:
  timeout_ms: 120000
  after_create: |
    $ErrorActionPreference = "Stop"
    git clone --no-checkout -- $env:SYMPHONY_PROJECT_REPOSITORY .
    git checkout --detach $env:SYMPHONY_SOURCE_COMMIT
  before_run: |
    $ErrorActionPreference = "Stop"
    git rev-parse --is-inside-work-tree *> $null
    if ($LASTEXITCODE -ne 0) { throw "Workspace is not a Git repository" }

agent:
  max_concurrent_agents: 4
  max_turns: 30
  max_retry_backoff_ms: 300000

codex:
  command: codex app-server
  approval_policy: never
  thread_sandbox: workspace-write
  turn_sandbox_policy:
    type: workspaceWrite
    networkAccess: true
---

You are responsible for Issue {{ issue.identifier }} in this repository.

Title: {{ issue.title }}
Description: {{ issue.description }}

Acceptance criteria:
{% for criterion in issue.acceptance_criteria %}
- {{ criterion }}
{% endfor %}
```

Control Plane 向 Hook 提供的非秘密环境变量：

- `SYMPHONY_PROJECT_ID`；
- `SYMPHONY_PROJECT_REPOSITORY`；
- `SYMPHONY_PROJECT_DEFAULT_BRANCH`；
- `SYMPHONY_SOURCE_COMMIT`；
- `SYMPHONY_WORKFLOW_REVISION`；
- `SYMPHONY_ISSUE_JSON`。

### 4.2 `AGENTS.md`

`AGENTS.md` 保存项目开发约束，例如：

- 模块边界；
- 构建和测试命令；
- 代码风格；
- 数据库变更规则；
- 禁止修改的目录；
- Push、发布和外部写入限制。

Runner 不解析其业务内容。Codex 在 Workspace 中按自身规则发现并应用。

### 4.3 仓库级 Skills

V2 删除中央 `skill_repository` 和 `agent.skills` 注入扩展。Skill 作为项目仓库内容随固定 Commit 一起进入 Workspace，Codex 从项目 `.codex/skills` 自动发现。

运行前后不再覆盖或恢复业务仓库的 `.agents`/`.codex` 目录。Runner 只需要：

1. 确认 Skills 路径位于 Workspace 内；
2. 启动 App Server 后调用 `skills/list`；
3. 记录实际发现的 Skill 名称、路径和内容哈希；
4. 禁止从 Workspace 外部注入隐式 Fshows Skill；
5. 将 Skill 快照写入 Attempt 配置快照和审计事件。

公共 Skill 可以继续在独立仓库开发，但发布到业务项目时必须复制、Subtree 或由独立同步工具固定到业务仓库。Symphony Runtime 不负责在线拉取中央 Skill。

## 5. 领域模型

### 5.1 Project

新增 `projects`：

| 字段 | 说明 |
|---|---|
| `id` | 稳定内部 ID |
| `key` | UI、日志和 Workspace 使用的唯一项目标识 |
| `name` | 展示名称 |
| `repository_path` | Windows 本地 Git 仓库绝对路径 |
| `default_branch` | 默认分支，注册时从 Git 解析，可人工修正 |
| `workflow_path` | 仓库内相对路径，默认 `WORKFLOW.md` |
| `enabled` | 是否允许调度 |
| `created_at` / `updated_at` | 审计时间 |

约束：

- `repository_path` 必须是绝对路径；
- 路径必须存在且为 Git Worktree；
- `workflow_path` 必须是安全相对路径，并解析在项目仓库内部；
- Project `key` 全局唯一，只允许 `[A-Za-z0-9._-]`；
- 同一规范化仓库路径不能重复注册。

### 5.2 Project Workflow Snapshot

新增 `project_workflow_snapshots`，用于审计和 Attempt 复现：

| 字段 | 说明 |
|---|---|
| `id` | 快照 ID |
| `project_id` | 所属项目 |
| `source_commit` | 读取 Workflow 时的项目 HEAD |
| `workflow_revision` | Workflow 内容 SHA-256 |
| `workflow_content` | 实际生效的完整内容 |
| `parsed_config` | 去除秘密值后的标准化配置 JSON |
| `status` | `valid` / `invalid` |
| `validation_error` | 校验错误 |
| `created_at` | 快照时间 |

Project Runtime 持有 Last-Known-Good Snapshot。新的 Workflow 无效时禁止新调度，但不终止已经运行的 Attempt；修复后自动恢复。

### 5.3 Issue

`issues` 调整：

- 新增必填 `project_id`；
- 保留内部 `source_commit`，但不允许用户提交；
- 新增 `workflow_snapshot_id`；
- 删除 API/UI 中的 `repository.url`、`base_branch` 和 `commit` 输入；
- `source_commit` 和 `workflow_snapshot_id` 在进入 `ready` 时冻结；
- Retry、NeedsHuman 恢复和同一 Issue 的所有 Attempt 继续使用同一个项目、Commit 和 Workspace；
- 若要基于新代码重新执行，应显式创建新 Issue 或执行受审计的 Rebase/Refresh 操作，不能静默改变起点。

### 5.4 Agent Attempt

Attempt 配置快照增加：

- Project ID/Key；
- Repository Path 的不可逆哈希或安全展示值；
- Source Commit；
- Workflow Snapshot ID/Revision；
- Codex 配置；
- 实际发现的 Skills 及内容哈希；
- AGENTS.md 内容哈希；
- Workspace Path；
- Thread ID 和 Turn 统计。

## 6. Project Runtime 与多项目调度

新增 `ProjectRuntimeSupervisor`：

```text
ProjectRuntimeSupervisor
├── project-a → ProjectRuntime → Tracker Adapter → WindowsSymphony
├── project-b → ProjectRuntime → Tracker Adapter → WindowsSymphony
└── project-c → ProjectRuntime → Tracker Adapter → WindowsSymphony
```

每个 `ProjectRuntime` 独立持有：

- 当前 Workflow 和 Last-Known-Good Snapshot；
- Tracker Adapter 的项目作用域；
- Project Workspace Manager；
- 并发、Retry 和 Running Map；
- Codex 配置；
- Workflow 文件监听状态；
- Worker 心跳与运行指标。

实现约束：

- Control Plane Tracker API 增加 `project_id` 过滤；
- Project Runtime 只能读取和 Claim 自己项目的 Issue；
- Worker ID 使用 `windows-symphony:<project-key>`；
- `max_concurrent_agents` 按项目生效；
- 可选的宿主机全局并发上限由 Supervisor 控制，但不写入项目 Workflow；
- 一个项目 Workflow 加载失败不能阻塞其他项目；
- 停止项目 Runtime 时只影响该项目的 Agent；
- 删除或禁用项目前必须完成 Active Run Reconciliation。

## 7. Workspace 与版本固定

Workspace 路径：

```text
<project-workspace-root>/<sanitized-issue-identifier>
```

执行不变量：

1. Issue 进入 `ready` 时读取项目 `HEAD^{commit}`，保存完整 40 位 `source_commit`；
2. Project Workflow 内容生成 `workflow_revision`；
3. Claim 返回 Project、Commit 和 Workflow Snapshot；
4. `after_create` 只能在新 Workspace 中执行；
5. Clone 后必须验证 `git rev-parse HEAD == source_commit`；
6. Codex 的 `cwd` 必须等于 Issue Workspace；
7. Workspace 必须位于项目配置的 Workspace Root 内；
8. Retry 和人工恢复不得 Reset、Clean 或重新 Clone 已运行的 Workspace；
9. 终态清理由 Retention Policy 和 `before_remove` 控制。

Workflow 热更新只影响后续 Claim。已经运行的 Attempt 继续使用领取时的 Workflow、Codex 和 Skill 快照。

## 8. API 设计

### 8.1 Project API

```text
POST   /api/projects
GET    /api/projects
GET    /api/projects/{project_id}
PATCH  /api/projects/{project_id}
DELETE /api/projects/{project_id}

POST   /api/projects/{project_id}/validate
POST   /api/projects/{project_id}/runtime/start
POST   /api/projects/{project_id}/runtime/stop
GET    /api/projects/{project_id}/runtime
GET    /api/projects/{project_id}/workflow-snapshots
```

`DELETE` 默认采用软删除或禁用；存在非终态 Issue、活跃 Attempt 或保留 Workspace 时拒绝物理删除。

### 8.2 Issue API

创建请求：

```json
{
  "id": "ISSUE-001",
  "project_id": "project-hengxi",
  "title": "新增用户详情接口",
  "description": "根据用户 ID 查询用户详情，复用现有权限与异常规范。",
  "priority": 2,
  "labels": ["backend"],
  "acceptance_criteria": [
    "接口可以查询用户详情",
    "用户不存在时返回统一业务异常",
    "相关自动化测试通过"
  ]
}
```

输出额外包含只读信息：

```json
{
  "project": {"id": "project-hengxi", "key": "hengxi-cultural-tourism"},
  "source_commit": "...40 chars...",
  "workflow_revision": "...sha256...",
  "workspace_path": null
}
```

标准 Tracker 读取内核调整为：

```text
GET /api/issues?project_id=<id>&state=ready&state=running
GET /api/issues?project_id=<id>&id=<issue-id>
```

### 8.3 Runtime API

现有单一 `/api/runner-control` 升级为 Supervisor 总览，同时保留项目级 Runtime API。总览展示：

- 已注册项目数；
- Available/Invalid/Stopped Runtime 数；
- 各项目 Worker PID、并发、活跃 Issue；
- Workflow Revision 和最近加载错误；
- Codex Token、Rate Limit、Turn Count 和运行时长。

## 9. UI 设计

新增“项目”一级页面：

- 项目列表、可用状态和 Runtime 状态；
- 仓库路径、默认分支和当前 HEAD；
- Workflow 校验结果与生效版本；
- 发现的 AGENTS.md 和 Skills；
- Workspace Root；
- 启动、停止、重新验证和禁用操作。

Issue 页面调整：

- 新建 Issue 必须选择 Project；
- 删除仓库路径、Base Branch、不可变 Commit和“读取 HEAD”；
- Issue 详情展示 Project、Source Commit、Workflow Revision；
- 看板和 Agent Runtime 默认支持按 Project 筛选；
- Project 不可用时禁止 Issue 进入 `ready`，并展示具体校验错误。

## 10. 安全边界

- Project 仓库路径和 Workflow 路径必须执行 Resolve/Containment 校验；
- 禁止 Workflow、Skill 和 Workspace 目录使用可逃逸的符号链接或 Junction；
- Hook 始终以 Workspace 为 `cwd`，并设置超时；
- Tracker Token、Claim Token 和宿主秘密变量不得继承到 Codex 子进程；
- `.codex/skills` 属于被信任的项目代码，仍需记录内容哈希和实际加载结果；
- `AGENTS.md`、Workflow 和 Skills 的变更必须在 Attempt 中可审计；
- 未经人工门禁，Agent 不得 Push、创建 PR、Merge、发布或使用生产凭据；
- Project Runtime 不能 Claim 其他 Project 的 Issue；
- 任何 Workflow 校验失败必须采用 Last-Known-Good 或停止新调度，不能部分应用。

## 11. 错误语义

| 错误 | 处理方式 |
|---|---|
| Project 路径不存在/不是 Git 仓库 | Project `invalid`，禁止调度 |
| `WORKFLOW.md` 缺失或无效 | 使用 Last-Known-Good；首次注册则 `invalid` |
| HEAD 无法解析 | Issue 不能进入 `ready` |
| Clone/Checkout 失败 | 当前 Attempt 失败并按 Retry Policy 处理 |
| Source Commit 不可用 | `blocked`，记录明确证据 |
| Skill 发现或解析失败 | Attempt 启动失败，不运行 Agent |
| AGENTS.md 规则冲突 | Agent 按 Codex 规则处理，事件中登记冲突摘要 |
| Project 被禁用 | 停止新 Claim，Reconcile 已运行 Issue |
| Workflow 热更新失败 | 保持 Last-Known-Good，持续暴露错误 |

## 12. 不兼容迁移方案

V2 不继续兼容“每个 Issue 自带仓库”和“中央 Skill 注入”协议。

代码层删除：

- 根项目中央 `WORKFLOW.md` 作为所有业务项目执行配置的语义；
- `skill_repository` 配置；
- `agent.skills` allowlist 扩展；
- `SkillManager.install/restore` 注入流程；
- Issue 创建/编辑中的仓库字段；
- 单一 Managed Runner 的假设。

数据层建议在开发阶段执行一次不兼容重建：

1. 停止 Control Plane 和所有 Runner；
2. 备份 SQLite；
3. 清理测试 Issue、Attempt、Worker 和 Workspace；
4. 创建 Project、Workflow Snapshot 和新 Issue Schema；
5. 注册至少一个真实项目；
6. 重新创建真实测试 Issue；
7. 不自动迁移旧 Issue 的仓库字段，避免错误归属。

正式执行清库前必须单独确认；方案文档本身不授权删除数据。

## 13. 分阶段实施

### 阶段 A：Project Registry

- 新增 Project/Workflow Snapshot 数据表和 API；
- 完成本地 Git 仓库、HEAD 和 Workflow 校验；
- 新增项目管理 UI；
- 增加 Project 协议测试。

### 阶段 B：Issue 项目归属

- Issue 必须关联 `project_id`；
- Ready 时生成 Source Commit 与 Workflow Snapshot；
- 删除 Issue 仓库输入；
- 更新 Tracker Adapter 的 Project Scope；
- 更新 UI 和 Schema。

### 阶段 C：Project Runtime Supervisor

- 将单一 WindowsSymphony 提取为 ProjectRuntime；
- 实现多 Project 生命周期、心跳、并发与故障隔离；
- 实现逐项目 Workflow 热更新和 Last-Known-Good；
- 更新 Runner Control API/UI。

### 阶段 D：仓库原生 Skills

- 删除中央 Skill Repository 与临时注入；
- 支持 Codex 自动发现 `.codex/skills`；
- 记录 skills/list 和内容哈希；
- 保证隔离 User Home 下不存在隐式公共 Skill；
- 增加缺失、无效、路径逃逸和依赖失败测试。

### 阶段 E：Workspace 与真实 E2E

- 使用 Project Hook Clone/Checkout 固定 Commit；
- 验证 Retry、NeedsHuman 和进程重启复用 Workspace/Thread；
- 验证多个项目同时执行且互不越界；
- 验证一次最终 Review 和交付门禁；
- 清理旧协议和演示数据。

## 14. Core Conformance 与验收标准

自动化测试至少覆盖：

- Project 路径、Workflow 路径和 Git 仓库校验；
- Project Key、仓库路径唯一性；
- Workflow 初次加载、热更新、失败和 Last-Known-Good；
- Issue 不能跨 Project Claim；
- Ready 时固定 Source Commit；
- Project/Issue Workspace 路径确定且不可逃逸；
- `after_create` 只在首次创建执行；
- 固定 Commit Clone/Checkout 和 HEAD 验证；
- `.codex/skills` 自动发现与快照；
- AGENTS.md 与 Skill 不从其他项目泄漏；
- Project 并发、状态并发、Required Labels 和 Blockers；
- Active Run Reconciliation；
- 单 Attempt 多 Turn、NeedsHuman 恢复和 Retry；
- Project Runtime 单点失败不影响其他项目；
- 未授权不能 Push/PR/Merge。

整体验收：

1. 注册 `D:\fws-repo-cache\hengxi-cultural-tourism` 后，系统自动识别其 `WORKFLOW.md`、HEAD、AGENTS.md 和 Skills；
2. 创建 Issue 时只选择“恒熙文旅”，无需填写任何仓库字段；
3. Runner 自动在项目 Workspace 下 Clone 固定 Commit；
4. Codex 只发现该项目仓库中的 Skills；
5. Agent 在同一 Workspace 与 Thread 中完成分析、代码和测试；
6. 另一个项目可使用完全不同的 Workflow、Skills 和并发配置并行运行；
7. 任一项目 Workflow 无效不会影响其他项目；
8. Issue 完成后仍经过一次人工 Review 和显式交付门禁。

## 15. 明确非目标

V2 本期不包含：

- 外部 Linear/GitHub/Jira Tracker 自动导入；
- 固定生命周期 Agent 或多角色 Handoff；
- Issue 类型到多阶段 Workflow DAG 的映射；
- Agent 自动维护或在线更新 Skill；
- 远程代码仓库控制副本服务；
- 分布式 Worker 调度；
- 自动 Push、自动 PR、自动 Merge 或自动发布。

这些能力如果后续需要，必须建立在 `Project → Issue → Workspace → Agent Session` 主模型之上，不能改变 Project 仓库拥有 Workflow、Rules 和 Skills 的基本边界。

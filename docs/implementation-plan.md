# Fshows Agent Control Plane 实施方案

## 1. 总体目标

自建 Agent Control Plane 作为任务与状态的唯一真相源，Symphony 负责任务调度，Codex App Server 负责执行，不同 Agent Profile 绑定不同 Prompt、Skill、权限和完成条件。

系统职责：

```text
Control Plane 负责：做什么、谁来做、做到哪、等待谁确认
Symphony 负责：何时执行、在哪里执行、并发与失败怎么办
Codex App Server 负责：执行具体 Agent Thread 和 Turn
Skill 负责：具体任务应该怎么做
```

第一版只验证核心研发闭环：

```text
Solution Architect
→ Backend Builder
→ Code Reviewer
→ Test Designer
→ Test Executor
→ Human Review
→ Done
```

暂不优先实现完整 PRD、DDL、文档发布、知识归档和复杂看板 UI。

---

## 2. 第一步：冻结调度协议

第一步先定义协议，不先实现看板页面。否则后续状态、接口、数据库和 Symphony Adapter 会反复重构。

### 2.1 定义 Agent Role

第一版定义五个角色：

```yaml
solution_architect:
  stage: tech_analysis

backend_builder:
  stage: implementation

code_reviewer:
  stage: code_review

test_designer:
  stage: test_design

test_executor:
  stage: test_execution
```

每个角色必须明确：

- 可以读取什么；
- 可以修改什么；
- 使用哪些 Skill；
- 使用什么沙箱；
- 是否允许网络；
- 是否允许修改业务代码；
- 完成条件；
- 遇到什么情况进入人工确认。

### 2.2 定义工作项模型

建议第一版字段：

```yaml
id: WI-001
feature_id: FEATURE-001
parent_id: null

title: M1 商品列表技术分析
description: "..."

stage: tech_analysis
agent_role: solution_architect
status: ready

priority: 2
version: 1

repository:
  url: git@github.com:org/repo.git
  base_branch: main
  head_branch: null
  commit: null
  pull_request: null

dependencies: []
input_artifacts: []
output_artifacts: []
acceptance_criteria: []
blocker: null

claim:
  worker_id: null
  token: null
  expires_at: null

created_at: null
updated_at: null
```

### 2.3 定义状态机

第一版保留以下状态：

```text
Draft
Ready
Running
NeedsHuman
Blocked
StageReview
Rework
RetryQueued
Done
Cancelled
```

业务阶段不要混入状态。例如：

```yaml
stage: test_execution
status: running
```

而不是增加“测试中”“技术分析中”等不断扩张的状态。

状态流转：

```text
Draft → Ready → Running → StageReview → Done
                    ├──→ NeedsHuman → Ready
                    ├──→ Blocked → Ready
                    ├──→ RetryQueued → Ready
                    └──→ Rework → Ready
```

### 2.4 定义结构化交接协议

每个 Agent 完成后生成：

```text
<featureRoot>/orchestration/handoffs/<work-item-id>.yaml
```

格式：

```yaml
schema_version: 1
work_item_id: WI-001
agent_role: solution_architect
result: completed

inputs:
  - path: task-split/task.md
    revision: abc123

outputs:
  - path: tech-analysis/M1.md

decisions:
  inherited: []
  added: []

validation:
  - name: architecture-review
    result: passed

risks: []
blockers: []
recommended_next_role: backend_builder
```

不同 Agent 之间通过 Git revision、PR、结构化 Artifact 和 Tracker Workpad 交接，不依赖上一个 Agent 的临时 workspace。

### 2.5 统一 Skill 产物路径

需要修复当前 `fshows-skills` 中的路径冲突：

- DDL：`ddl/{需求}-ddl.md` 与 `ddl/ddl.md`；
- 技术分析：`task/*.md` 与 `tech-analysis/*.md`；
- Review：`reviews/` 与 `review/`；
- Publish Skill 的扫描目录；
- 已删除 Skill 的残留引用。

建议统一目录：

```text
<featureRoot>/
├── prd/
│   └── original.md
├── analysis/
│   └── prd-consensus.yaml
├── ddl/
│   └── design.md
├── task-split/
│   ├── task.md
│   ├── api-spec.md
│   └── decision-log.md
├── tech-analysis/
├── reviews/
├── test/
└── orchestration/
    └── handoffs/
```

### 2.6 第一步验收标准

- 五种 Agent Role 有明确契约；
- WorkItem JSON/YAML Schema 固定；
- 状态转换表固定；
- Handoff Schema 固定；
- Skill 产物路径固定；
- 使用一个虚拟 Feature 可以人工推演完整状态流。

### 2.7 实现结果（2026-08-03）

第一步已实现并由本地校验器验收：

- `protocol/agent-roles.yaml` 固定五种 Agent Role 的读写范围、Skill、沙箱、网络权限、业务代码权限、完成条件和人工确认条件；
- `protocol/schemas/work-item.schema.json` 固定 WorkItem JSON/YAML 数据模型，并约束 Role/Stage 映射与 claim 不变量；
- `protocol/state-machine.yaml` 固定 10 个状态、21 条转换、事件、执行主体、守卫和副作用；
- `protocol/schemas/handoff.schema.json` 固定结构化交接格式；
- `protocol/artifact-layout.yaml` 固定产物路径并列出禁用旧别名；
- `protocol/examples/FEATURE-001/` 使用五个虚拟工作项推演主链路，并覆盖 `Blocked`、`RetryQueued`、`Rework` 和 `NeedsHuman` 恢复路径；
- `scripts/validate_protocol.py` 校验 Schema、状态转换、依赖 DAG、Role 权限、Artifact 路径、Handoff 关联和推演结果。

验收命令：

```powershell
python scripts/validate_protocol.py
```

---

## 3. 第二步：实现最小控制面后端

第二步只实现 API 和数据库，UI 暂时使用 Swagger、简单管理页或 CLI 代替。

第二步按当前部署决策使用 SQLite，并通过 WAL、busy timeout、乐观版本和单条条件更新保证单机并发一致性。若未来需要多节点数据库服务，再单独规划迁移。

### 3.1 创建核心数据表

```text
features
work_items
work_item_dependencies
work_item_artifacts
work_item_events
agent_profiles
agent_attempts
human_decisions
```

核心关系：

```text
Feature
├── WorkItem
│   ├── Dependency
│   ├── Artifact
│   ├── Event
│   └── Attempt
└── HumanDecision
```

### 3.2 实现工作项 API

最小接口：

```http
POST   /api/features
GET    /api/features/{id}

POST   /api/work-items
GET    /api/work-items
GET    /api/work-items/{id}
PATCH  /api/work-items/{id}

GET    /api/work-items/candidates
POST   /api/work-items/{id}/claim
POST   /api/work-items/{id}/heartbeat
POST   /api/work-items/{id}/release

POST   /api/work-items/{id}/status
POST   /api/work-items/{id}/events
POST   /api/work-items/{id}/artifacts
POST   /api/work-items/{id}/decisions
```

### 3.3 实现原子 Claim

必须使用数据库事务或乐观锁，避免两个 Symphony 实例同时领取同一个任务。

请求示例：

```json
{
  "workerId": "symphony-01",
  "expectedVersion": 12,
  "leaseSeconds": 300
}
```

成功条件：

```text
status = Ready
依赖全部 Done
没有有效 Claim
version = expectedVersion
```

领取成功后：

```text
status → Running
version + 1
生成 claim_token
设置 expires_at
记录 claimed 事件
```

### 3.4 实现 Lease 和 Heartbeat

- Worker 定期发送 heartbeat；
- Lease 过期后任务进入 `RetryQueued`；
- 原 Worker 的旧 claim token 失效；
- 所有运行期状态修改必须携带 claim token；
- 管理员取消任务时立即撤销 claim。

### 3.5 实现事件日志

至少记录：

```text
created
dependency_satisfied
claimed
agent_started
thread_started
turn_started
artifact_created
validation_passed
human_input_requested
blocked
retry_scheduled
review_failed
completed
cancelled
```

### 3.6 第二步验收标准

- 两个并发请求不能同时 claim 一个工作项；
- Worker 崩溃后 Lease 能自动过期；
- 有未完成依赖的任务不会进入 candidates；
- 每次状态改变都有事件；
- 可以通过 API 完成一次人工模拟调度。

### 3.7 实现结果（2026-08-04）

第二步已实现：

- FastAPI 提供 Feature、WorkItem、Candidates、Claim、Heartbeat、Release、Status、Event、Artifact 和 Human Decision API；
- SQLAlchemy 模型和 Alembic 初始迁移创建 `features`、`work_items`、`work_item_dependencies`、`work_item_artifacts`、`work_item_events`、`agent_profiles`、`agent_attempts`、`human_decisions` 八张表；
- SQLite 连接统一启用外键、WAL、`synchronous=NORMAL` 与 5 秒 busy timeout；
- Claim 使用 `status + version + dependency NOT EXISTS` 条件更新，两个并发请求只有一个成功；
- Claim token 只返回一次，数据库保存摘要，所有运行期写操作校验 token 与过期时间；
- 后台维护任务回收过期 Lease、撤销旧 token，并按 `RetryQueued → Ready` 恢复；
- 所有状态变化、Claim、Heartbeat、Artifact、人工决策和依赖满足都有事件记录；
- `scripts/simulate_api.py` 已人工走通包含 `NeedsHuman` 的完整 API 调度；
- 自动化测试覆盖依赖门禁、并发 Claim、Lease 恢复、旧 token 失效、人工决策、依赖环拒绝和审计事件。

---

## 4. 第三步：实现 Windows 原生 Symphony Runner

这一阶段按照语言无关 Symphony SPEC 自行实现 Windows 原生调度层，让系统从 Control Plane 领取任务并通过 stdio 驱动 Windows 版 Codex App Server。

### 4.1 新增 Windows Runner

核心模块：

```text
symphony_windows.workflow
symphony_windows.workspace
symphony_windows.tracker
symphony_windows.codex
symphony_windows.orchestrator
```

运行链：

```text
Poll candidates
→ 原子 Claim
→ 创建 Windows Workspace
→ 启动 codex app-server
→ initialize / thread/start / turn/start
→ 动态工具与 Heartbeat
→ 完成、人工确认、Blocked 或 Retry
```

### 4.2 字段映射

```text
WorkItem.id           → Issue.id
WorkItem.id           → Issue.identifier
WorkItem.title        → Issue.title
WorkItem.description  → Issue.description
WorkItem.status       → Issue.state
WorkItem.priority     → Issue.priority
WorkItem.dependencies → issue.blocked_by
WorkItem.agent_role   → issue.labels / profile selector
```

### 4.3 暴露受限动态工具

通过 `thread/start.dynamicTools` 向 Codex App Server 暴露：

```text
work_item_get
work_item_add_event
work_item_add_artifact
work_item_request_human
work_item_complete
work_item_block
```

第一版不暴露通用 HTTP 请求工具，防止 Agent 任意修改任务系统。

### 4.4 认证与秘密隔离

```yaml
tracker:
  kind: fshows_control_plane
  provider:
    endpoint: http://127.0.0.1:8080
    token: $CONTROL_PLANE_TOKEN
    worker_id: $SYMPHONY_WORKER_ID
```

Bearer Token 与 Claim Token 只在 Windows Runner 宿主进程使用；启动 Codex 子进程前删除对应秘密环境变量。

### 4.5 第三步验收标准

- 创建 `Ready` 任务后 Windows Runner 能发现；
- Windows Runner 能原子 claim；
- Codex 能读取当前工作项；
- Codex 能写事件和产物；
- 正常完成、失败和 blocked 都能回写；
- Runner 重启不会重复执行仍在有效 Lease 中的任务。

### 4.6 实现结果（2026-08-04）

第三步已改为并完成 Windows 原生实现：

- `Workflow` 严格解析仓库拥有的 YAML Front Matter，并使用 strict Liquid 渲染 Prompt；
- `WorkspaceManager` 校验 Windows 保留名、路径穿越、符号链接逃逸和 Workspace Root 边界，并执行 PowerShell 生命周期 Hook；
- `ControlPlaneTracker` 负责候选查询、原子 Claim、Heartbeat、Retry Release 和六个绑定当前 WorkItem 的动态工具；
- `CodexAppServer` 直接启动 Windows 子进程，通过 JSONL stdio 执行 `initialize`、`thread/start`、`turn/start` 和动态工具回包；
- `WindowsSymphony` 实现优先级调度、全局并发槽位、Claim 冲突处理、Heartbeat、Claim 丢失终止、同一进程内多 Turn continuation 和 10 秒起始的异常指数退避；
- Control Plane Release API 支持由 Runner 指定有界 Retry Delay；
- Bearer Token 与 Claim Token 不进入 Tool Schema、Prompt 或 Codex 子进程环境；
- 自动化端到端测试使用真实 Windows 子进程和假 Codex App Server，覆盖发现、Claim、六工具注册、事件、Handoff、完成和 Blocked 回写；
- 本机 Codex CLI 生成的 App Server JSON Schema 已用于字段核对，未发起模型调用。

官方 Elixir Adapter 叠加层仍保留在 `integrations/symphony_elixir/` 作为可选兼容参考，不再是 Windows 主运行链，也不再要求 Elixir/Mix 验收。

---

## 5. 第四步：增加 Agent Profile 路由

这是实现不同 Agent 行为的核心。

### 5.1 定义 Profile 配置

建议扩展 `WORKFLOW.md`：

```yaml
agent_profiles:
  solution_architect:
    match:
      agent_role: solution_architect
    prompt_file: workflows/solution-architect.md
    skills:
      - fskill-analysis-tech
      - fskill-knowledge-query
      - fskill-tools-db
    sandbox: danger-full-access
    max_concurrent_agents: 2
    max_turns: 10

  backend_builder:
    match:
      agent_role: backend_builder
    prompt_file: workflows/backend-builder.md
    skills:
      - fskill-code-java-guide
      - fskill-knowledge-query
      - fskill-tools-db
    sandbox: danger-full-access
    network_access: true
    max_concurrent_agents: 5
    max_turns: 20

  code_reviewer:
    match:
      agent_role: code_reviewer
    prompt_file: workflows/code-reviewer.md
    skills:
      - fskill-code-review
    sandbox: danger-full-access
    max_concurrent_agents: 3

  test_designer:
    match:
      agent_role: test_designer
    prompt_file: workflows/test-designer.md
    skills:
      - fskill-test-explore
    sandbox: danger-full-access

  test_executor:
    match:
      agent_role: test_executor
    prompt_file: workflows/test-executor.md
    skills:
      - fskill-test-verify
    sandbox: danger-full-access
    network_access: true
```

### 5.2 路由流程

```text
读取 WorkItem.agent_role
→ 匹配 Profile
→ 验证 Profile
→ 选择 Prompt
→ 准备 Skill
→ 解析沙箱
→ 启动 Codex App Server
```

找不到 Profile 时直接标记配置错误，不能静默回退到默认 Builder。

### 5.3 Profile 级并发限制

示例：

```text
Solution Architect：2
Backend Builder：5
Code Reviewer：3
Test Designer：2
Test Executor：2
```

Profile 并发限制之外仍需保留 Symphony 全局并发上限和每台 Worker 的并发上限。

### 5.4 Profile 配置快照

Agent Run 启动时记录：

```yaml
profile_name: backend_builder
profile_version: 3
prompt_hash: "..."
skills:
  fskill-code-java-guide: b61341e
model: "..."
sandbox: danger-full-access
```

运行过程中配置热更新不能改变已经启动的 Session。

### 5.5 第四步验收标准

- 同一 Symphony 实例可以执行不同 Agent Role；
- 不同角色使用不同 Prompt、Skill、沙箱；
- Review Agent 无法修改源码；
- Builder Agent 无法使用 Release 凭据；
- Profile 并发限制有效；
- 执行历史可以还原使用的 Profile 版本。

### 5.6 实现结果（2026-08-04）

第四步已完成 Windows 原生实现：

- `WORKFLOW.md` 必须声明至少一个 Agent Profile，配置加载时严格校验唯一 Role 匹配、版本、Prompt 文件边界、Skill 名称、沙箱、网络策略、并发上限和最大 Turn 数；
- Runner 在 Claim 前按 `WorkItem.agent_role` 精确路由，找不到或出现重复 Profile 时将任务置为 `Blocked`，不回退默认 Builder；
- 每个 Profile 使用独立 Prompt，并将 Skill allowlist、Profile 身份和执行策略写入最终 Prompt；
- Profile 沙箱和网络策略转换为独立 Codex `thread/start` / `turn/start` 参数；正式 v2 Profile 按部署授权统一使用 `danger-full-access`，Review 的只评不改约束由角色 Prompt 和 Handoff 协议执行；
- 调度器同时执行全局和 Profile 级并发限制；普通 continuation 在同一 Attempt、Claim、Thread 和 App Server 进程内执行，超过 `max_turns` 且未形成 Handoff 时才阻断；
- Claim 会原子登记 `agent_profiles` 版本，并把 Prompt SHA-256、Skill、模型、沙箱、网络和并发配置快照写入 `agent_attempts`；
- 同名同版本配置发生变化时拒绝复用，保证热更新不改变已启动 Session，配置变化必须递增 Profile 版本；
- 新增 Agent Profile 和 WorkItem Attempt 查询接口，执行历史可还原 Profile 版本和快照；
- Builder 的 Control Plane/Release 凭据继续由宿主隔离，不进入 Profile Snapshot、Prompt、Tool Schema 或 Codex 子进程环境。

---

## 6. 第五步：实现 Skill 注入和版本固定

### 6.1 Profile Skill Allowlist

只将 Profile 声明的 Skill 暴露到 workspace：

```text
.agents/skills/
├── fskill-code-java-guide
├── fskill-knowledge-query
└── fskill-tools-db
```

不要把全部 Skill 安装给所有 Agent。

### 6.2 固定 Skill 版本

不要让运行中的任务直接引用不断变化的全局目录。建议记录：

```yaml
skill_repository:
  url: "..."
  revision: b61341e
```

Workspace 创建时使用固定 revision。

### 6.3 Skill 兼容性校验

检查：

- Front Matter；
- 引用文件是否存在；
- 模板和脚本路径；
- 已删除 Skill 引用；
- 产物目录；
- 人工确认点；
- 外部写操作；
- 所需工具和凭据。

### 6.4 将人工确认转换为状态

统一约定：

```text
Skill 要求用户确认
→ Agent 写 decision request
→ WorkItem 进入 NeedsHuman
→ Agent 正常退出
→ 用户在看板选择或回复
→ WorkItem 回到 Ready
→ Symphony 继续
```

### 6.5 第五步验收标准

- 每个 Agent 只能看到自己的 Skill；
- 执行记录包含 Skill revision；
- Skill 引用不存在时在启动前失败；
- 不会在 Codex Turn 中无限等待人工输入。

### 6.6 实现结果（2026-08-04）

第五步已完成 Windows 原生实现：

- `WORKFLOW.md` 新增必填 `skill_repository.url`、完整 40 位 commit `revision` 和 `skills_path`，Runner 使用无交互 Git checkout 缓存固定 revision；
- Runner 启动时校验所有 Profile 的 Skill，任何缺失目录、缺失 `SKILL.md`、错误 Front Matter、断裂引用、符号链接逃逸、旧 Artifact 别名、未满足工具/凭据或越过 allowlist 的 Skill 依赖都会在 Claim 前失败；
- 每次执行都会原子替换工作区 `.agents/skills`，只复制当前 Profile allowlist，并生成 `.agents/skills.lock.json`；
- Profile Snapshot 将每个 Skill 的 Git revision、内容 SHA-256、工具、凭据名称、外部写操作和人工确认点写入 `agent_attempts`，源仓库的未提交或后续变化不会影响运行 Session；
- Codex 子进程使用工作区隔离的用户 Home，同时保留宿主 `CODEX_HOME` 认证状态；Thread 启动前通过 App Server `skills/list` 确认声明 Skill 来自当前工作区，并拒绝额外的 `fskill-*`；
- Skill 声明的凭据仅在宿主校验，名称进入审计快照，值继续从 Codex 子进程环境移除；
- Profile Prompt 统一要求人工确认时调用 `work_item_request_human` 并结束 Turn，App Server 的运行期输入请求也转换为相同状态协议；
- 新增人工决策查询接口，自动化测试覆盖 `Running → NeedsHuman → Ready`，Agent 不会在 Turn 中无限等待。
- 已将 `D:\saasProject\fshows-skills` 中五个 Profile 使用的 7 个 Skill 复制到本仓库 `skills/`；修正技术分析 Skill 的旧 DDL、Task 和 Research 产物路径，源仓库保持不变。
- `scripts/validate_skills.py` 固定 vendored Skill 集合并校验包结构、引用和内容摘要，纳入本地回归命令。

---

## 7. 第六步：跑通第一个端到端闭环

选择一个真实但低风险的后端模块作为试点。

### 7.1 创建五个工作项

```text
WI-001 M1 技术分析
WI-002 M1 后端实现
WI-003 M1 Code Review
WI-004 M1 测试方案
WI-005 M1 测试执行
```

依赖：

```text
WI-001
  ↓
WI-002
  ↓
WI-003
  ↓
WI-004
  ↓
WI-005
```

### 7.2 Solution Architect 验收

- 正确找到 PRD、DDL、Task Split；
- 产生技术分析；
- 产生 Handoff；
- 需要决策时进入 `NeedsHuman`。

### 7.3 Backend Builder 验收

- 只使用后端 Skill；
- 按技术分析实现；
- 运行目标测试；
- 产生 commit 信息和 Handoff；
- 默认不自动 push，需要推送时进入明确授权流程。

### 7.4 Code Reviewer 验收

- 使用只读沙箱；
- 生成 Standards/Spec 双轴报告；
- HIGH 问题使任务进入 Rework；
- 不直接修改代码。

### 7.5 Test Agent 验收

- Test Designer 只生成测试方案；
- Test Executor 严格按方案执行；
- 不降低不可替代断言；
- 业务代码缺陷退回 Builder；
- 测试成功后进入人工确认。

### 7.6 第六步验收标准

- 五个 Agent Profile 串联成功；
- Rework 能回到 Builder；
- NeedsHuman 可以恢复；
- Symphony 重启后任务可恢复；
- 所有产物和运行事件可追踪；
- 没有任务依赖临时 workspace 才能交接。

---

## 8. 第七步：开发最小看板 UI

后端协议稳定后再实现 UI。

### 8.1 第一版页面

1. Feature 列表；
2. WorkItem 看板；
3. 工作项详情；
4. Agent Attempt 时间线；
5. Workpad 和人工回复；
6. Artifact、PR 和测试报告；
7. Retry、Cancel、Approve 操作；
8. Profile 配置查看。

### 8.2 看板列

```text
Draft
Ready
Running
Needs Human
Stage Review
Rework
Blocked
Done
```

### 8.3 实时信息

通过 SSE 或 WebSocket 展示：

- 当前 Agent；
- 当前 Codex Thread/Turn；
- 最新事件；
- Token；
- 运行时长；
- 最后 Heartbeat；
- 当前 Workspace；
- Blocker；
- 重试时间。

### 8.4 实现结果（2026-08-04）

第七步最小看板已实现：

- FastAPI 根路径直接提供响应式管理看板，不增加 Node、外部 CDN 或独立部署单元；
- 新增 Feature 列表 API，看板支持 Feature、角色和关键词筛选；
- 九列看板完整覆盖 `Draft`、`Ready`、`Running`、`NeedsHuman`、`StageReview`、`Rework`、`RetryQueued`、`Blocked`、`Done/Cancelled`；
- 工作项详情展示依赖、验收标准、Claim/Lease、Attempt、Codex Thread、Event 时间线以及输入输出 Artifact；
- 支持人工决策回复、阶段批准、退回返工、Draft 就绪、解除阻塞、重试维护和管理员取消；
- 页面每 5 秒轮询并在重新聚焦时刷新，后续任务量增大后可无缝替换为 SSE；
- Bearer Token 仅保存在当前标签页 `sessionStorage`，不进入 URL、日志或持久化存储；
- 自动化测试覆盖 UI 入口、静态资源和 Feature 列表 API。

---

## 9. 第八步：固化并启用正式 WORKFLOW.md

在继续开发需求录入、外部 Issue 导入和上游 Agent 之前，先把 Windows
Runner 的仓库契约从示例提升为可验证、可启动、可恢复的正式配置。完成本阶段前，
不继续扩展 UI 信息架构或新增 Agent Role。

### 9.1 正式配置

仓库根目录必须提交 `WORKFLOW.md`，并完整声明：

- Control Plane Tracker、Bearer Token 环境变量、Worker ID 和 Lease；
- Poll 间隔、全局并发和 Retry 上限；
- Windows Workspace 根目录和 PowerShell 生命周期 Hook；
- 固定 Git revision 的 Skill Repository；
- 五个 Agent Profile 的 Prompt、Skill、权限、网络、并发和最大 Turn；
- Codex App Server 命令、审批策略、沙箱、超时和用户目录隔离；
- 所有 Profile 共用的无人值守执行、人工确认、Handoff 和完成规则。

`WORKFLOW.windows.example.md` 继续作为无秘密的配置模板，但 Runner、文档和验收
统一以根目录正式 `WORKFLOW.md` 为准。

### 9.2 Workspace 固定规则

首次创建 WorkItem Workspace 时必须：

```text
读取 WorkItem.repository.url 与 commit
→ Clone 到唯一 Workspace
→ checkout --detach 指定 commit
→ 校验实际 HEAD
→ 才允许启动 Codex
```

同一 WorkItem 的 continuation、NeedsHuman 恢复和 Rework 必须复用现有 Workspace，
不得在 `before_run` 中 reset 或覆盖 Agent 已产生的工作状态。

### 9.3 启动前验收

提供只读 Workflow 验收器，在不 Claim WorkItem、不启动 Codex 的情况下验证：

- YAML Front Matter 和 Liquid Prompt 可严格加载；
- Profile 集合与协议中的 Agent Role 完全一致；
- Prompt 文件均位于仓库内且可渲染；
- Skill Repository revision 是完整 commit，五个 Profile 的 Skill 均存在且兼容；
- Workspace 路径、Tracker Endpoint、秘密引用和 Codex 策略满足安全约束。

### 9.4 完成标准

- 根目录正式 `WORKFLOW.md` 可通过仓库验收器；
- 固定 Skill revision 可以完成 checkout 和兼容性检查；
- 五个 Profile 均能对协议示例 WorkItem 完成严格 Prompt 渲染；
- Windows Runner 文档不再要求复制示例文件；
- 自动化测试会阻止正式 Workflow 与 Role、Prompt、Skill 契约发生漂移；
- 配置验证不读取或输出 Token 值，也不启动 Agent 或修改目标项目。

完成本阶段后，下一阶段才实现统一 Requirement Intake、UI 创建需求、WorkItem
拆分确认和外部 Issue 幂等导入。

### 9.5 实现结果（2026-08-04）

第八步正式 Workflow 已实现：

- 根目录新增正式 `WORKFLOW.md`，完整配置 Tracker、Workspace Hook、五个 Agent
  Profile、固定 Skill Repository、Codex 策略和全局无人值守执行规则；
- `after_create` 严格要求 WorkItem 提供完整 40 位 `repository.commit`，Clone 后以
  detached HEAD 检出并核对实际 commit；
- 新 Workspace 的 Clone/Checkout Hook 失败时清理半成品目录，保证后续 Dispatch
  可以重新执行 `after_create`；
- 新增 `scripts/validate_workflow.py`，只读验证正式 Workflow、Profile Prompt、Role
  集合以及固定 revision Skill，不 Claim 任务、不启动 Codex；
- 自动化测试覆盖正式配置漂移、固定 Skill checkout、真实 PowerShell Clone/Checkout
  和失败清理；
- Runner 文档改为直接使用正式 `WORKFLOW.md`，并明确环境变量、启动前验收和常驻
  Runner 命令。

---

## 10. 第九步：手工 Issue Intake

本期只支持用户在控制面手工录入 Issue。GitHub、Linear、云效等外部 Issue 导入、
Webhook 和双向状态同步全部延期，不进入本期范围。

### 10.1 录入字段

- Feature ID、标题和需求描述；
- 优先级；
- 仓库地址或本机绝对路径；
- Base Branch 和完整 40 位不可变 Git commit；
- 一条或多条验收标准。

### 10.2 拆分与确认

第一版不启动 Task Planner Agent，而是使用确定性的
`five_stage_backend_v1` 模板：

```text
Solution Architect（Ready）
→ Backend Builder（Draft）
→ Code Reviewer（Draft）
→ Test Designer（Draft）
→ Test Executor（Draft）
```

UI 必须先请求只读拆分预览，显示 WorkItem ID、Role、顺序和初始状态；用户确认后，
控制面在单个 SQLite 事务中创建 Feature、五个 WorkItem、依赖和审计事件。重复
Feature ID 或生成的 WorkItem ID 冲突时整体失败，不允许产生半条链路。

### 10.3 完成标准

- UI 可以手工录入 Issue；本地仓库自动读取 `HEAD`，远程仓库允许手工填写并校验
  不可变 commit；
- 拆分预览不写数据库；
- 确认创建具有事务原子性；
- 只有首个 WorkItem 是 Runner Candidate；
- 五个角色、Stage 和依赖顺序与已冻结协议一致；
- 自动化测试覆盖预览、创建、冲突和非法 commit；
- 不包含任何外部 Issue Provider、凭据或同步任务。

### 10.4 实现结果（2026-08-04）

第九步手工 Issue Intake 已实现：

- 新增手工 Issue 拆分预览与确认创建 API，预览无数据库写入；
- 固定 `five_stage_backend_v1` 模板生成五个 Role/Stage 串行 WorkItem，仅技术分析
  初始为 `Ready`；
- 确认创建在单个 SQLite 事务中写入 Feature、WorkItem、依赖和来源审计事件；
- 手工录入强制完整 40 位 Git commit，与正式 `WORKFLOW.md` 的 detached checkout
  规则一致；
- 管理看板新增录入表单、五阶段拆分预览和确认创建操作；
- 自动化测试覆盖预览无副作用、成功创建、重复创建、生成 ID 冲突原子回滚和非法
  commit；
- 本期没有新增外部 Issue Provider、Webhook、凭据或同步后台任务。

---

## 11. 第十步：Runner 运维与 Agent 可观测性

手工 Issue 能产生候选任务后，优先补齐执行面的长期运行与可观测闭环，不立即扩展
外部 Issue Provider 或更多研发角色。Symphony Runner 是长期轮询服务，不按 Issue
临时创建一次性进程；控制面必须能够区分“任务 Ready”和“Worker 在线并已领取”。

### 11.1 Worker 生命周期

- Runner 启动时注册 Worker ID、宿主机、PID、版本、并发容量和 Profile 集合；
- 空闲和执行期间持续上报 Worker 心跳、当前 WorkItem 与 Profile；
- 超过离线阈值未心跳时由查询端标记 `offline`，不覆盖最后一次执行快照；
- 控制面可以请求优雅停止，Runner 停止领取新任务并安全释放执行中的 Claim；
- Windows 本地部署提供受控 Runner Supervisor，支持 UI 启动、停止和查看最近日志。

### 11.2 Agent Runtime

Agent Runtime 以 WorkItem 的最新 Attempt 为事实来源，展示：

- `waiting_dependency`、`ready`、`starting`、`running`；
- `waiting_human`、`reviewing`、`retrying`、`rework`、`blocked`；
- `completed`、`cancelled`；
- Worker、Profile、Attempt、Codex Thread/Turn 和启动时间。

Codex 创建 Thread 和 Turn 后立即更新 Attempt 上下文，不能只在 WorkItem 结束时补写，
从而避免实际已经运行但 UI 长时间显示“正在准备”。人工门禁必须保留 Thread ID，人工
回复后 Runner 才能延续原上下文。

### 11.3 完成标准

- UI 以 Agent 状态为第一视角，同时保留 WorkItem 看板；
- 没有 Runner 时 Ready 明确显示为“尚未分配 Worker”；
- Runner 空闲时仍在线，停止或异常退出后可识别；
- UI 可启动和优雅停止本机托管 Runner；
- Thread/Turn 在执行中可见，Needs Human 能关联原 Thread；
- 每个 Attempt 可展开结构化执行详情，显示 Turn、Agent 消息、命令、工具调用、
  文件变更、退出码和错误；运行中的详情随看板轮询刷新；
- 执行事件按 Claim Token 绑定当前 Attempt，敏感字段和常见凭据模式双重脱敏；
  Reasoning 只记录开始/完成状态，不持久化推理文本；
- 自动化测试覆盖 Worker 注册、心跳、停止请求、Attempt 上下文和运行态映射；
- Runner 重启不会重复执行有效 Lease，恢复规则继续遵守正式 `WORKFLOW.md`。

### 11.4 实现结果（2026-08-04）

- 新增 Worker SQLite 表和迁移，记录 Runner 身份、宿主、PID、容量、Profile、心跳、
  当前任务、停止请求和离线状态；
- Windows Runner 在空闲与执行期间注册并持续心跳，收到停止请求后不再领取新任务，
  安全释放 Claim 后退出；
- 新增 Agent Runtime API，将 WorkItem 与最新 Attempt 映射为 Agent 视角状态；
- Codex Thread/Turn 在创建后立即写入 Attempt，人工门禁保存 Thread，并在人工回复后
  通过 `thread/resume` 延续相同上下文；
- 新增同机 Runner Supervisor 和启停 API，停止时先请求优雅退出，超时才终止进程；
- 看板新增 Agent 状态中心、Worker 在线卡片、Runner 启停和执行上下文入口；
- 新增 `agent_attempt_events` SQLite 表和 Attempt Event API；Runner 把 Codex App
  Server 结构化事件归一化后实时上报，命令输出采用长度上限，载荷使用字段白名单和
  服务端二次脱敏；
- Attempt 卡片支持展开执行时间线，展示命令退出码、工具名称、文件路径和可折叠输出；
  旧 Attempt 没有事件时显示明确的历史数据提示；
- 自动化测试覆盖 Worker 生命周期、离线识别、运行态映射、Thread/Turn 写入以及
  Needs Human 原 Thread 恢复、事件鉴权/排序/脱敏和 Reasoning 排除；本地托管 Runner
  启动、注册、心跳、停止冒烟验证通过。

### 11.5 Symphony SPEC 对齐任务（2026-08-04）

在继续扩展研发角色和外部 Issue Provider 前，先完成当前 `openai/symphony` SPEC 的
核心运行语义。看板只表达 WorkItem 的宏观工作流状态；Turn、工具调用和子 Agent 属于
Attempt 内部执行过程，不驱动 WorkItem 在 `Running`、`RetryQueued`、`Ready` 之间抖动。

| ID | 状态 | 任务 | 完成标准 |
|---|---|---|---|
| SPEC-001 | 已完成 | 单 Attempt 多 Turn | 同一 Claim、Attempt、Thread 和 App Server 进程内连续执行多个 Turn；未交付时 WorkItem 保持 `Running` |
| SPEC-002 | 待开始 | Active Run Reconciliation 与 Workspace 清理 | 每个 Tick 刷新运行中 WorkItem；终态停止执行、调用 `before_remove` 并安全清理 Workspace |
| SPEC-003 | 待开始 | `WORKFLOW.md` 热更新 | 文件变化后重新校验并应用；无效版本不影响上一份有效配置和在途执行 |
| SPEC-004 | 待开始 | Token、Rate Limit、Turn Count 和结构化日志 | 运行态记录 Session、Turn 数、Token、Rate Limit、运行时长和带上下文字段的结构化日志 |
| SPEC-005 | 待开始 | Tracker、CLI 和 API 标准兼容层 | 补齐 state/ID Tracker 读取、标准配置字段、默认 Workflow 路径和 `/api/v1` 兼容接口 |

`SPEC-001` 的 Attempt 只在交付、Needs Human、Blocked、取消、真实失败或达到执行上限时
结束。普通 Turn 完成只触发同一 Thread 的下一 Turn，不释放 Claim，也不创建新 Attempt。

`SPEC-001` 已实现并通过真实子进程协议集成测试：第一个 Turn 未交付时，同一 App
Server 进程接收第二个 `turn/start`，复用 Thread、Claim、Attempt 和 Workspace；第二
个 Turn 交付后只产生一个 Attempt，WorkItem 全程未进入 `RetryQueued`。达到 Profile
`max_turns` 时才以 `max_turns_exceeded` 阻断，真实异常仍使用独立重试 Attempt。若
Attempt 因 Needs Human 结束，人工回复后的新 Claim 会携带全部已解决决策，并把问题、
选项和回复作为权威上下文注入恢复 Turn，避免 Agent 在后续 Blocked、
Retry 或新会话中丢失结论或重复询问。Needs Human 与 Retry 复用原 Thread；环境类
Blocked 解阻后启动新 Thread，避免沿用已经固化错误运行根的旧 Session。

Thread 必须把当前 Feature Workspace 的规范化绝对路径显式写入
`runtimeWorkspaceRoots`，`workspaceWrite` Turn 同时写入
`sandboxPolicy.writableRoots`。不能只依赖 `cwd` 隐式推断，否则 Codex App Server 可能
把受管 Workspace 判定为 project 外部并拒绝 `apply_patch`，或拒绝 Shell 写入。

### 11.6 Feature 共享 Workspace 与交付闭环（2026-08-04）

Symphony 规格中的调度单位是 Issue；本项目的 Issue 对应 `Feature`，而不是拆分后的单个
`WorkItem`。因此 Workspace 必须按 `Feature.id` 确定，Solution Architect、Backend、
Review、Test Designer 和 Test Executor 在同一持久 Git Workspace 和累计变更集上串行
工作。Profile Skill 只在 Agent 运行期间安装，结束后恢复原始 `.agents`，不能污染业务
代码差异或最终 Commit。

Feature 交付状态为：

```text
active
→ Test Executor Stage Review 通过
→ 创建 codex/<feature> 本地 Commit
→ awaiting_publish
→ 人工授权 Push 与创建 PR
→ pr_open
→ PR 在 Provider 侧核验为 MERGED
→ done
```

WorkItem `done` 只表示该阶段交接完成，不等于 Feature 已交付。`git push`、创建 PR 和合并
确认是三个显式外部写入门禁；没有用户授权不得执行。Feature 只有在 PR 被 GitHub CLI
核验为已合并后才能进入 `done`。隔离 Workspace 的改动不得直接复制覆盖原项目工作树。

---

## 12. 第十一步：扩展完整研发链路

核心闭环稳定后，再增加上游和下游角色。

### 12.1 上游 Agent

- Requirement Agent；
- Data Architect Agent；
- Task Planner Agent。

### 12.2 下游 Agent

- Frontend Builder Agent；
- Document Publisher；
- Knowledge Archivist。

完整流程：

```text
PRD
→ 需求探索
→ DDL
→ 任务拆分
→ 技术分析
→ 开发
→ Review
→ 测试
→ 人工合并
→ 文档发布
→ 知识归档
```

---

## 13. 权限模型

| Agent | 建议权限 |
|---|---|
| Requirement Agent | 读 PRD、写需求文档；不改业务代码 |
| Data Architect Agent | 数据库只允许 schema/SELECT；禁止 UPDATE/DELETE |
| Task Planner Agent | 只写任务、接口和决策文档 |
| Solution Architect Agent | 读代码和数据库；不写业务代码 |
| Backend/Frontend Builder | 工作区写权限、构建与测试；外部发布需明确策略 |
| Code Reviewer | 源代码只读，只允许写 Review 报告 |
| Test Designer | 源代码只读，只写测试方案 |
| Test Executor | 可写测试和 Harness；业务缺陷默认退回 Builder |
| Release/Knowledge Agent | 仅合并后运行；使用独立受控凭据 |

默认情况下，`git push`、合并、外部批量创建、知识归档和删除清理均应设置显式人工门禁，除非部署方针对具体 Profile 明确授权。

---

## 14. 总任务清单

| ID | 任务 | 优先级 | 依赖 |
|---|---|---:|---|
| ARC-001 | 定义 Agent Role | P0 | 无 |
| ARC-002 | 定义状态机 | P0 | ARC-001 |
| ARC-003 | 定义 WorkItem Schema | P0 | ARC-001 |
| ARC-004 | 定义 Handoff Schema | P0 | ARC-003 |
| ARC-005 | 统一 Skill 产物路径 | P0 | 无 |
| ACP-001 | 创建控制面数据库 | P0 | ARC-002、ARC-003 |
| ACP-002 | 实现 WorkItem CRUD | P0 | ACP-001 |
| ACP-003 | 实现 Claim/Lease | P0 | ACP-001 |
| ACP-004 | 实现事件记录 | P0 | ACP-001 |
| ACP-005 | 实现依赖调度 | P0 | ACP-002 |
| SYM-001 | 实现 Control Plane Tracker Adapter | P0 | ACP-002、ACP-003 |
| SYM-002 | 实现受限 Agent 动态工具 | P0 | ACP-004 |
| SYM-003 | 实现 Agent Profile Schema | P0 | ARC-001 |
| SYM-004 | 实现 Profile Router | P0 | SYM-003 |
| SYM-005 | 实现 Profile 并发限制 | P1 | SYM-004 |
| SYM-006 | 实现配置快照 | P1 | SYM-004 |
| SKL-001 | 修复 Skill 路径冲突 | P0 | ARC-005 |
| SKL-002 | 增加 Skill Allowlist | P0 | SYM-004 |
| SKL-003 | 固定 Skill Revision | P1 | SKL-002 |
| SKL-004 | 转换人工确认协议 | P0 | ARC-002 |
| E2E-001 | 验证 Solution Architect | P0 | SYM、SKL 主链路 |
| E2E-002 | 验证 Backend Builder | P0 | E2E-001 |
| E2E-003 | 验证 Code Reviewer | P0 | E2E-002 |
| E2E-004 | 验证 Test Designer/Executor | P0 | E2E-003 |
| UI-001 | Feature/WorkItem 看板 | P1 | ACP 稳定 |
| UI-002 | Attempt 时间线 | P1 | ACP-004 |
| UI-003 | 人工确认面板 | P1 | SKL-004 |
| WFL-001 | 提交正式 WORKFLOW.md | P0 | SYM-003、SKL-003 |
| WFL-002 | 固定 Workspace Clone/Checkout Hook | P0 | WFL-001 |
| WFL-003 | 实现仓库级 Workflow 验收器 | P0 | WFL-001 |
| WFL-004 | 验证正式 Profile Prompt 与 Skill | P0 | WFL-003 |
| WFL-005 | 更新 Runner 启动与运维文档 | P0 | WFL-004 |
| INT-001 | 统一 Requirement Intake API | P0 | WFL-005 |
| INT-002 | UI 创建 Feature 和拆分预览 | P1 | INT-001 |
| INT-003 | 外部 Issue 幂等导入（后续期） | P2 | INT-001 |
| RUN-001 | Worker 注册、心跳和离线识别 | P0 | INT-002 |
| RUN-002 | Attempt Thread/Turn 实时上下文 | P0 | RUN-001 |
| RUN-003 | Agent Runtime 查询与状态映射 | P0 | RUN-002 |
| RUN-004 | Windows 本地 Runner Supervisor | P1 | RUN-001 |
| RUN-005 | Agent 状态中心与 Runner 控制 UI | P1 | RUN-003、RUN-004 |
| SPEC-001 | 单 Attempt 多 Turn，保持会话和 Running 稳定 | P0 | RUN-002 |
| SPEC-002 | Active Run Reconciliation 和 Workspace 清理 | P0 | SPEC-001 |
| SPEC-003 | WORKFLOW.md 动态热更新 | P0 | SPEC-002 |
| SPEC-004 | Token、Rate Limit、Turn Count 和结构化日志 | P1 | SPEC-001 |
| SPEC-005 | Tracker、CLI、API 标准兼容层 | P1 | SPEC-002、SPEC-003、SPEC-004 |
| HARD-001 | 权限和秘密隔离 | P0 | SYM-004 |
| HARD-002 | 重启恢复测试 | P0 | ACP-003 |
| HARD-003 | 并发 Claim 测试 | P0 | ACP-003 |
| HARD-004 | 审计与幂等测试 | P1 | ACP-004 |

---

## 15. 启动顺序

最初的两个开发阶段：

1. 完成 `ARC-001` 至 `ARC-005`，冻结协议和产物目录；
2. 完成 `ACP-001` 至 `ACP-005`，实现没有复杂 UI 的最小控制面后端。

这两步完成后再接入 Symphony，不反过来先修改 Symphony。

截至 2026-08-04，协议、控制面、Windows Runner、Profile/Skill、端到端验证、最小
UI 以及 `WFL-001` 至 `WFL-005` 已完成。本期先完成 `INT-001` 手工录入 API、
`INT-002` UI 创建与拆分预览，再完成 `RUN-001` 至 `RUN-005` 的 Runner/Agent
运行闭环；下一阶段按 `SPEC-001` 至 `SPEC-005` 完成 Symphony SPEC 对齐，之后再扩展
完整研发角色；`INT-003` 外部 Issue 导入延期。

第一阶段完成标志不是“看板能打开”，而是可以用固定 Schema 和 API 人工走通：

```text
创建任务
→ 满足依赖
→ 原子领取
→ Heartbeat
→ 写事件与产物
→ 请求人工确认
→ 恢复执行
→ 完成
```

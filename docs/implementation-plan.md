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
- `WindowsSymphony` 实现优先级调度、全局并发槽位、Claim 冲突处理、Heartbeat、Claim 丢失终止、1 秒 continuation 和 10 秒起始的指数退避；
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
    sandbox: workspace-write
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
    sandbox: workspace-write
    network_access: true
    max_concurrent_agents: 5
    max_turns: 20

  code_reviewer:
    match:
      agent_role: code_reviewer
    prompt_file: workflows/code-reviewer.md
    skills:
      - fskill-code-review
    sandbox: read-only
    max_concurrent_agents: 3

  test_designer:
    match:
      agent_role: test_designer
    prompt_file: workflows/test-designer.md
    skills:
      - fskill-test-explore
    sandbox: workspace-write

  test_executor:
    match:
      agent_role: test_executor
    prompt_file: workflows/test-executor.md
    skills:
      - fskill-test-verify
    sandbox: workspace-write
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
sandbox: workspace-write
```

运行过程中配置热更新不能改变已经启动的 Session。

### 5.5 第四步验收标准

- 同一 Symphony 实例可以执行不同 Agent Role；
- 不同角色使用不同 Prompt、Skill、沙箱；
- Review Agent 无法修改源码；
- Builder Agent 无法使用 Release 凭据；
- Profile 并发限制有效；
- 执行历史可以还原使用的 Profile 版本。

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

---

## 9. 第八步：扩展完整研发链路

核心闭环稳定后，再增加上游和下游角色。

### 9.1 上游 Agent

- Requirement Agent；
- Data Architect Agent；
- Task Planner Agent。

### 9.2 下游 Agent

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

## 10. 权限模型

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

## 11. 总任务清单

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
| HARD-001 | 权限和秘密隔离 | P0 | SYM-004 |
| HARD-002 | 重启恢复测试 | P0 | ACP-003 |
| HARD-003 | 并发 Claim 测试 | P0 | ACP-003 |
| HARD-004 | 审计与幂等测试 | P1 | ACP-004 |

---

## 12. 启动顺序

最先开始的两个开发阶段：

1. 完成 `ARC-001` 至 `ARC-005`，冻结协议和产物目录；
2. 完成 `ACP-001` 至 `ACP-005`，实现没有复杂 UI 的最小控制面后端。

这两步完成后再接入 Symphony，不反过来先修改 Symphony。

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

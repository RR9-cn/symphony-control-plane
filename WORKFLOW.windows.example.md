---
tracker:
  kind: fshows_control_plane
  provider:
    endpoint: http://127.0.0.1:8080
    token: $CONTROL_PLANE_TOKEN
    worker_id: $SYMPHONY_WORKER_ID
    lease_seconds: 300
    request_timeout_seconds: 30

polling:
  interval_ms: 5000

workspace:
  root: ./.workspaces

hooks:
  timeout_ms: 120000
  after_create: |
    $ErrorActionPreference = "Stop"
    $issue = $env:SYMPHONY_ISSUE_JSON | ConvertFrom-Json
    $repository = [string]$issue.repository.url
    $commit = [string]$issue.repository.commit
    if ([string]::IsNullOrWhiteSpace($repository)) { throw "WorkItem repository.url is required" }
    if ($commit -notmatch '^[0-9a-fA-F]{40}$') { throw "WorkItem repository.commit must be a full 40-character Git commit" }
    & git clone --no-checkout -- $repository .
    if ($LASTEXITCODE -ne 0) { throw "git clone failed with exit code $LASTEXITCODE" }
    & git checkout --detach $commit
    if ($LASTEXITCODE -ne 0) { throw "git checkout failed with exit code $LASTEXITCODE" }
    $actual = (& git rev-parse HEAD).Trim().ToLowerInvariant()
    if ($actual -ne $commit.ToLowerInvariant()) { throw "Workspace HEAD does not match WorkItem repository.commit" }
  before_run: |
    $ErrorActionPreference = "Stop"
    & git rev-parse --is-inside-work-tree *> $null
    if ($LASTEXITCODE -ne 0) { throw "WorkItem workspace is not a Git repository" }

agent:
  max_concurrent_agents: 4
  max_retry_backoff_ms: 300000

skill_repository:
  url: $FSHOWS_SKILLS_REPOSITORY
  revision: $FSHOWS_SKILLS_REVISION
  skills_path: skills
  cache_root: ./.symphony-cache/skills

agent_profiles:
  solution_architect:
    version: 1
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
    version: 1
    match:
      agent_role: backend_builder
    prompt_file: workflows/backend-builder.md
    skills:
      - fskill-code-java-guide
      - fskill-knowledge-query
      - fskill-tools-db
    sandbox: workspace-write
    network_access: true
    max_concurrent_agents: 4
    max_turns: 20

  code_reviewer:
    version: 1
    match:
      agent_role: code_reviewer
    prompt_file: workflows/code-reviewer.md
    skills:
      - fskill-code-review
    sandbox: read-only
    max_concurrent_agents: 3
    max_turns: 10

  test_designer:
    version: 1
    match:
      agent_role: test_designer
    prompt_file: workflows/test-designer.md
    skills:
      - fskill-test-explore
    sandbox: workspace-write
    max_concurrent_agents: 2
    max_turns: 10

  test_executor:
    version: 1
    match:
      agent_role: test_executor
    prompt_file: workflows/test-executor.md
    skills:
      - fskill-test-verify
    sandbox: workspace-write
    network_access: true
    max_concurrent_agents: 2
    max_turns: 15

codex:
  command: codex app-server
  isolate_user_home: true
  approval_policy: never
  thread_sandbox: danger-full-access
  turn_sandbox_policy:
    type: dangerFullAccess
  turn_timeout_ms: 3600000
  read_timeout_ms: 5000
  stall_timeout_ms: 300000
---

You are working on WorkItem {{ issue.identifier }}.

Title: {{ issue.title }}

Description:
{{ issue.description }}

Agent role: {{ issue.agent_role }}
Stage: {{ issue.stage }}
Attempt: {{ attempt | default: 0 }}

Use only the bound `work_item_*` tools for Control Plane writes. Register the
required Handoff Artifact before calling `work_item_complete`. If execution
cannot continue, call `work_item_block` or `work_item_request_human`.

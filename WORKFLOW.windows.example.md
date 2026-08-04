---
tracker:
  kind: fshows_control_plane
  provider:
    endpoint: http://127.0.0.1:8080
    token: $CONTROL_PLANE_TOKEN
    worker_id: $SYMPHONY_WORKER_ID
    lease_seconds: 300

polling:
  interval_ms: 5000

workspace:
  root: ./.workspaces

hooks:
  timeout_ms: 60000
  after_create: |
    $repository = ($env:SYMPHONY_ISSUE_JSON | ConvertFrom-Json).repository.url
    if ($repository) { git clone $repository . }

agent:
  max_concurrent_agents: 4
  max_retry_backoff_ms: 300000

skill_repository:
  url: $FSHOWS_SKILLS_REPOSITORY
  revision: $FSHOWS_SKILLS_REVISION
  skills_path: skills

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
  approval_policy:
    reject:
      sandbox_approval: true
      rules: true
      mcp_elicitations: true
  thread_sandbox: workspace-write
  turn_sandbox_policy:
    type: workspaceWrite
    networkAccess: false
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

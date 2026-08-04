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

codex:
  command: codex app-server
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

---
tracker:
  kind: fshows_control_plane
  required_labels: []
  active_states:
    - ready
    - running
  terminal_states:
    - done
    - cancelled
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
    $repository = $env:SYMPHONY_PROJECT_REPOSITORY
    $commit = $env:SYMPHONY_SOURCE_COMMIT
    if ([string]::IsNullOrWhiteSpace($repository)) {
      throw "SYMPHONY_PROJECT_REPOSITORY is required"
    }
    if ($commit -notmatch '^[0-9a-fA-F]{40}$') {
      throw "SYMPHONY_SOURCE_COMMIT must be a full 40-character Git commit"
    }
    & git clone --no-checkout -- $repository .
    if ($LASTEXITCODE -ne 0) { throw "git clone failed with exit code $LASTEXITCODE" }
    & git cat-file -e "$commit^{commit}"
    if ($LASTEXITCODE -ne 0) { throw "Source Commit is unavailable" }
    & git checkout --detach $commit
    if ($LASTEXITCODE -ne 0) { throw "git checkout failed with exit code $LASTEXITCODE" }
    $actual = (& git rev-parse HEAD).Trim().ToLowerInvariant()
    if ($actual -ne $commit.ToLowerInvariant()) { throw "Workspace HEAD does not match Source Commit" }
  before_run: |
    $ErrorActionPreference = "Stop"
    & git rev-parse --is-inside-work-tree *> $null
    if ($LASTEXITCODE -ne 0) { throw "Issue workspace is not a Git repository" }

agent:
  max_concurrent_agents: 4
  max_concurrent_agents_by_state: {}
  max_retry_backoff_ms: 300000
  max_turns: 30
  sandbox: danger-full-access
  network_access: true

codex:
  command: codex app-server
  isolate_user_home: true
  approval_policy: never
  turn_sandbox_policy:
    type: dangerFullAccess
  turn_timeout_ms: 3600000
  read_timeout_ms: 5000
  stall_timeout_ms: 300000
---

You are the coding agent responsible for Issue {{ issue.identifier }} from analysis through implementation and validation.

Title: {{ issue.title }}

Description:
{{ issue.description }}

Attempt: {{ attempt | default: 0 }}

Project binding:
- Project ID: {{ issue.project_id }}
- Immutable starting commit: {{ issue.source_commit }}
- Workflow revision: {{ issue.workflow_revision }}

Acceptance criteria:
{% for criterion in issue.acceptance_criteria %}
- {{ criterion }}
{% endfor %}

Execution contract:

1. Work only in the provided persistent Issue workspace. Analyze the existing code, implement the complete scoped change, add or update tests, and run relevant validation.
2. Follow repository AGENTS.md rules and use only Skills automatically discovered from this repository's `.codex/skills` directory when they materially help.
3. Use only the bound `issue_*` tools for Control Plane writes. Never expose or request tracker credentials or the Issue claim token.
4. Do not push, create a Pull Request, merge, publish, release, use production credentials, or perform destructive cleanup. Those actions remain explicit human delivery gates.
5. If a material decision is required, call `issue_request_human` with a concrete question and options, then end the Turn.
6. If a real external blocker prevents progress, call `issue_block` with actionable evidence. Do not submit incomplete work as complete.
7. Record important progress with `issue_add_event` and register durable supporting artifacts with `issue_add_artifact` when useful.
8. Before calling `issue_complete`, inspect the final diff, run relevant tests and checks, and verify every acceptance criterion. `issue_complete` submits the whole Issue for one final human review.
9. A successful Turn alone does not complete the Issue. Continue in the same Thread until the completion, human-input, or blocker tool ends the run.

{% if attempt %}
This is continuation or retry attempt {{ attempt }}. Reuse the existing Workspace and Thread context. Continue from the remaining work instead of repeating completed investigation.
{% endif %}

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

    if ([string]::IsNullOrWhiteSpace($repository)) {
      throw "WorkItem repository.url is required"
    }
    if ($commit -notmatch '^[0-9a-fA-F]{40}$') {
      throw "WorkItem repository.commit must be a full 40-character Git commit"
    }

    & git clone --no-checkout -- $repository .
    if ($LASTEXITCODE -ne 0) {
      throw "git clone failed with exit code $LASTEXITCODE"
    }
    & git cat-file -e "$commit^{commit}"
    if ($LASTEXITCODE -ne 0) {
      throw "WorkItem repository.commit is not available in the cloned repository"
    }
    & git checkout --detach $commit
    if ($LASTEXITCODE -ne 0) {
      throw "git checkout failed with exit code $LASTEXITCODE"
    }

    $actual = (& git rev-parse HEAD).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $actual -ne $commit.ToLowerInvariant()) {
      throw "Workspace HEAD does not match WorkItem repository.commit"
    }
  before_run: |
    $ErrorActionPreference = "Stop"
    & git rev-parse --is-inside-work-tree *> $null
    if ($LASTEXITCODE -ne 0) {
      throw "WorkItem workspace is not a Git repository"
    }

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
    network_access: false
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
    network_access: false
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
    network_access: false
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

You are working autonomously on WorkItem {{ issue.identifier }}.

Title: {{ issue.title }}

Description:
{{ issue.description }}

Agent role: {{ issue.agent_role }}
Stage: {{ issue.stage }}
Attempt: {{ attempt | default: 0 }}

Repository input:
- URL: {{ issue.repository.url }}
- Base branch: {{ issue.repository.base_branch }}
- Immutable commit: {{ issue.repository.commit }}

Acceptance criteria:
{% for criterion in issue.acceptance_criteria %}
- {{ criterion }}
{% endfor %}

Execution rules:

1. Work only inside the provided WorkItem workspace and follow the selected
   Agent Profile and its allowed Skills.
2. Treat the WorkItem, registered input Artifacts, repository commit, and prior
   Handoffs as the authoritative execution context.
3. Use only the bound `work_item_*` tools for Control Plane writes. Never expose
   or request the Control Plane Bearer Token or the WorkItem Claim Token.
4. Do not push, merge, release, publish externally, use production credentials,
   or perform destructive cleanup without an explicit human authorization flow.
5. If a decision or permission is required, call `work_item_request_human` with
   a concrete question and options, then end the Turn. Do not wait for terminal
   input inside the Codex session.
6. If execution cannot continue because of a real external condition, call
   `work_item_block` with actionable evidence. Do not report incomplete work as
   complete.
7. Record material progress and validation through WorkItem events. Register
   every required output Artifact using its repository-relative canonical path.
8. Before calling `work_item_complete`, run the relevant validation, create the
   schema-valid `orchestration/handoffs/{{ issue.identifier }}.yaml`, and register
   that Handoff as an output Artifact.
9. A successful Codex Turn is not sufficient by itself. The WorkItem is handed
   off only through the bound completion tool and then waits for Stage Review.

{% if attempt %}
This is continuation or retry attempt {{ attempt }}. Reuse the current Workspace
and existing Thread context. Inspect prior work and events, then continue from
the remaining scoped work instead of restarting completed investigation.
{% endif %}

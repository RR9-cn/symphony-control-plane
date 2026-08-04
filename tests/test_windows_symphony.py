from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import feature_payload, work_item_payload
from symphony_windows.codex import CodexRunResult
from symphony_windows.orchestrator import WindowsSymphony
from symphony_windows.skill import SkillError, SkillManager
from symphony_windows.tracker import ClaimLease, ControlPlaneTracker, ToolExecution
from symphony_windows.workflow import WorkflowError, load_workflow
from symphony_windows.workspace import workspace_key


def create_skill_repository(path: Path) -> tuple[Path, str]:
    repository = path / "skill-repository"
    skills = {
        "fskill-analysis-tech": "tech-analysis/*.md",
        "fskill-code-java-guide": "test/results/*.md",
    }
    for name, artifact_path in skills.items():
        skill_root = repository / "skills" / name
        references = skill_root / "references"
        references.mkdir(parents=True, exist_ok=True)
        (skill_root / "SKILL.md").write_text(
            f"""---
name: {name}
description: Test fixture for {name}.
metadata:
  fshows:
    artifact_paths:
      - {artifact_path}
      - orchestration/handoffs/*.yaml
    human_confirmation:
      - explicit_operator_decision
    external_writes: []
    required_tools: []
    required_credentials: []
    required_skills: []
---
Follow [the fixture guide](references/guide.md).
""",
            encoding="utf-8",
        )
        (references / "guide.md").write_text("Pinned fixture.\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test Runner"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repository), "add", "skills"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "--quiet", "-m", "fixture skills"],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repository, revision


def write_workflow(path: Path, command: str, *, include_backend: bool = False) -> Path:
    skill_repository, skill_revision = create_skill_repository(path)
    prompt_directory = path / "workflows"
    prompt_directory.mkdir(exist_ok=True)
    (prompt_directory / "solution-architect.md").write_text(
        "Architect WorkItem {{ issue.identifier }}: {{ issue.title }}.",
        encoding="utf-8",
    )
    (prompt_directory / "backend-builder.md").write_text(
        "Build WorkItem {{ issue.identifier }}: {{ issue.title }}.",
        encoding="utf-8",
    )
    backend_profile = (
        """
  backend_builder:
    version: 2
    match:
      agent_role: backend_builder
    prompt_file: workflows/backend-builder.md
    skills:
      - fskill-code-java-guide
    sandbox: workspace-write
    network_access: true
    max_concurrent_agents: 1
    max_turns: 20
"""
        if include_backend
        else ""
    )
    workflow = path / "WORKFLOW.md"
    workflow.write_text(
        f"""---
tracker:
  kind: fshows_control_plane
  provider:
    endpoint: http://127.0.0.1:8080
    token: $CONTROL_PLANE_TOKEN
    worker_id: windows-test
    lease_seconds: 30
polling:
  interval_ms: 100
workspace:
  root: ./workspaces
agent:
  max_concurrent_agents: 2
  max_retry_backoff_ms: 300000
skill_repository:
  url: "{skill_repository.as_posix()}"
  revision: {skill_revision}
  skills_path: skills
  cache_root: ./skill-cache
agent_profiles:
  solution_architect:
    version: 1
    match:
      agent_role: solution_architect
    prompt_file: workflows/solution-architect.md
    skills:
      - fskill-analysis-tech
    sandbox: workspace-write
    max_concurrent_agents: 1
    max_turns: 10
{backend_profile}
codex:
  command: {command}
  read_timeout_ms: 5000
  stall_timeout_ms: 10000
---
Execute WorkItem {{{{ issue.identifier }}}}: {{{{ issue.title }}}}.
Attempt {{{{ attempt | default: 0 }}}}.
""",
        encoding="utf-8",
    )
    return workflow


async def test_windows_runner_drives_codex_and_completes_work_item(
    authenticated_api,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = {"Authorization": "Bearer integration-secret"}
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "integration-secret")
    fake_server = Path(__file__).with_name("fake_codex_app_server.py")
    command = subprocess.list2cmdline([sys.executable, str(fake_server)])
    workflow = load_workflow(write_workflow(tmp_path, command))

    created_feature = await authenticated_api.post(
        "/api/features", json=feature_payload(), headers=headers
    )
    assert created_feature.status_code == 201, created_feature.text
    created_item = await authenticated_api.post(
        "/api/work-items",
        json=work_item_payload("WI-001", status="ready"),
        headers=headers,
    )
    assert created_item.status_code == 201, created_item.text

    tracker = ControlPlaneTracker(
        workflow.tracker,
        transport=httpx.ASGITransport(app=authenticated_api.app),
    )
    try:
        async with WindowsSymphony(workflow, tracker=tracker) as orchestrator:
            outcomes = await orchestrator.run_once()
    finally:
        await tracker.close()

    assert len(outcomes) == 1
    assert outcomes[0].status == "work_item_released"
    assert outcomes[0].thread_id == "thread-test"
    item = (
        await authenticated_api.get("/api/work-items/WI-001", headers=headers)
    ).json()
    assert item["status"] == "stage_review"
    assert item["claim"] == {"worker_id": None, "expires_at": None}
    events = (
        await authenticated_api.get("/api/work-items/WI-001/events", headers=headers)
    ).json()
    event_types = [event["event_type"] for event in events]
    assert event_types == [
        "created",
        "claimed",
        "agent_started",
        "turn_started",
        "artifact_created",
        "agent_completed",
    ]
    profiles = (
        await authenticated_api.get("/api/agent-profiles", headers=headers)
    ).json()
    assert len(profiles) == 1
    assert profiles[0]["name"] == "solution_architect"
    assert profiles[0]["version"] == 1
    attempts = (
        await authenticated_api.get("/api/work-items/WI-001/attempts", headers=headers)
    ).json()
    assert len(attempts) == 1
    assert attempts[0]["status"] == "stage_review"
    assert attempts[0]["profile_snapshot"]["profile_name"] == "solution_architect"
    skill_snapshot = attempts[0]["profile_snapshot"]["skills"]
    assert list(skill_snapshot) == ["fskill-analysis-tech"]
    assert len(skill_snapshot["fskill-analysis-tech"]["revision"]) == 40
    assert len(skill_snapshot["fskill-analysis-tech"]["content_hash"]) == 64
    assert len(attempts[0]["profile_snapshot"]["prompt_hash"]) == 64
    execution_events = (
        await authenticated_api.get(
            f"/api/work-items/WI-001/attempts/{attempts[0]['id']}/events",
            headers=headers,
        )
    ).json()
    execution_types = [event["event_type"] for event in execution_events]
    assert "turn_started" in execution_types
    assert "command_completed" in execution_types
    assert "agent_message_completed" in execution_types
    assert "tool_call_started" in execution_types
    serialized_events = str(execution_events)
    assert "trace-secret" not in serialized_events
    assert "private reasoning must never be persisted" not in serialized_events
    reasoning = next(
        event for event in execution_events if event["item_type"] == "reasoning"
    )
    assert reasoning["detail"] is None
    installed = tmp_path / "workspaces" / "WI-001" / ".agents" / "skills"
    assert [path.name for path in installed.iterdir()] == ["fskill-analysis-tech"]


async def test_bound_block_tool_releases_claim(
    authenticated_api,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = {"Authorization": "Bearer integration-secret"}
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "integration-secret")
    workflow = load_workflow(write_workflow(tmp_path, "codex app-server"))
    await authenticated_api.post(
        "/api/features", json=feature_payload(), headers=headers
    )
    await authenticated_api.post(
        "/api/work-items",
        json=work_item_payload("WI-001", status="ready"),
        headers=headers,
    )
    tracker = ControlPlaneTracker(
        workflow.tracker,
        transport=httpx.ASGITransport(app=authenticated_api.app),
    )
    try:
        candidate = (await tracker.candidates())[0]
        lease = await tracker.claim(candidate)
        result = await tracker.execute_tool(
            lease,
            "work_item_block",
            {
                "code": "missing_dependency",
                "message": "Required service is unavailable.",
            },
        )
    finally:
        await tracker.close()

    assert result.response["success"]
    assert result.stop_agent
    assert not lease.active
    item = (
        await authenticated_api.get("/api/work-items/WI-001", headers=headers)
    ).json()
    assert item["status"] == "blocked"
    assert item["blocker"]["code"] == "missing_dependency"


async def test_codex_failure_releases_claim_to_retry_queue(
    authenticated_api,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = {"Authorization": "Bearer integration-secret"}
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "integration-secret")
    monkeypatch.setenv("FAKE_CODEX_MODE", "fail")
    fake_server = Path(__file__).with_name("fake_codex_app_server.py")
    command = subprocess.list2cmdline([sys.executable, str(fake_server)])
    workflow = load_workflow(write_workflow(tmp_path, command))
    await authenticated_api.post(
        "/api/features", json=feature_payload(), headers=headers
    )
    await authenticated_api.post(
        "/api/work-items",
        json=work_item_payload("WI-001", status="ready"),
        headers=headers,
    )
    tracker = ControlPlaneTracker(
        workflow.tracker,
        transport=httpx.ASGITransport(app=authenticated_api.app),
    )
    try:
        async with WindowsSymphony(workflow, tracker=tracker) as orchestrator:
            outcomes = await orchestrator.run_once()
    finally:
        await tracker.close()

    assert outcomes[0].status == "retry_queued"
    assert "simulated failure" in (outcomes[0].error or "")
    item = (
        await authenticated_api.get("/api/work-items/WI-001", headers=headers)
    ).json()
    assert item["status"] == "retry_queued"
    assert item["retry_at"] is not None
    assert item["claim"] == {"worker_id": None, "expires_at": None}


async def test_codex_continuation_stays_in_one_attempt_and_live_process(
    authenticated_api,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = {"Authorization": "Bearer integration-secret"}
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "integration-secret")
    monkeypatch.setenv("FAKE_CODEX_MODE", "continuation")
    fake_server = Path(__file__).with_name("fake_codex_app_server.py")
    command = subprocess.list2cmdline([sys.executable, str(fake_server)])
    workflow = load_workflow(write_workflow(tmp_path, command))
    await authenticated_api.post(
        "/api/features", json=feature_payload(), headers=headers
    )
    await authenticated_api.post(
        "/api/work-items",
        json=work_item_payload("WI-001", status="ready"),
        headers=headers,
    )

    tracker = ControlPlaneTracker(
        workflow.tracker,
        transport=httpx.ASGITransport(app=authenticated_api.app),
    )
    try:
        async with WindowsSymphony(workflow, tracker=tracker) as orchestrator:
            outcomes = await orchestrator.run_once()
    finally:
        await tracker.close()

    assert outcomes[0].status == "work_item_released"
    assert outcomes[0].thread_id == "thread-test"
    assert outcomes[0].turn_id == "turn-test-2"
    item = (
        await authenticated_api.get("/api/work-items/WI-001", headers=headers)
    ).json()
    assert item["status"] == "stage_review"
    attempts = (
        await authenticated_api.get("/api/work-items/WI-001/attempts", headers=headers)
    ).json()
    assert [attempt["attempt_number"] for attempt in attempts] == [1]
    assert attempts[0]["thread_id"] == "thread-test"
    assert attempts[0]["turn_id"] == "turn-test-2"
    execution_events = (
        await authenticated_api.get(
            f"/api/work-items/WI-001/attempts/{attempts[0]['id']}/events",
            headers=headers,
        )
    ).json()
    assert [
        event["event_type"] for event in execution_events
    ].count("turn_started") == 2
    work_item_events = (
        await authenticated_api.get("/api/work-items/WI-001/events", headers=headers)
    ).json()
    work_item_statuses = [event["to_status"] for event in work_item_events]
    assert "retry_queued" not in work_item_statuses


async def test_multi_turn_attempt_blocks_only_after_session_turn_limit(
    authenticated_api,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = {"Authorization": "Bearer integration-secret"}
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "integration-secret")
    monkeypatch.setenv("FAKE_CODEX_MODE", "exhaust")
    fake_server = Path(__file__).with_name("fake_codex_app_server.py")
    command = subprocess.list2cmdline([sys.executable, str(fake_server)])
    workflow = load_workflow(write_workflow(tmp_path, command))
    workflow = replace(
        workflow,
        agent_profiles=(replace(workflow.agent_profiles[0], max_turns=2),),
    )
    await authenticated_api.post(
        "/api/features", json=feature_payload(), headers=headers
    )
    await authenticated_api.post(
        "/api/work-items",
        json=work_item_payload("WI-001", status="ready"),
        headers=headers,
    )
    tracker = ControlPlaneTracker(
        workflow.tracker,
        transport=httpx.ASGITransport(app=authenticated_api.app),
    )
    try:
        async with WindowsSymphony(workflow, tracker=tracker) as orchestrator:
            outcomes = await orchestrator.run_once()
    finally:
        await tracker.close()

    assert outcomes[0].status == "max_turns_exceeded"
    item = (
        await authenticated_api.get("/api/work-items/WI-001", headers=headers)
    ).json()
    assert item["status"] == "blocked"
    assert item["blocker"]["code"] == "max_turns_exceeded"
    attempts = (
        await authenticated_api.get("/api/work-items/WI-001/attempts", headers=headers)
    ).json()
    assert len(attempts) == 1
    assert attempts[0]["status"] == "blocked"
    execution_events = (
        await authenticated_api.get(
            f"/api/work-items/WI-001/attempts/{attempts[0]['id']}/events",
            headers=headers,
        )
    ).json()
    assert [
        event["event_type"] for event in execution_events
    ].count("turn_started") == 2
    work_item_events = (
        await authenticated_api.get("/api/work-items/WI-001/events", headers=headers)
    ).json()
    assert "retry_queued" not in [
        event["to_status"] for event in work_item_events
    ]


async def test_skill_human_confirmation_exits_and_resumes_ready(
    authenticated_api,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = {"Authorization": "Bearer integration-secret"}
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "integration-secret")
    monkeypatch.setenv("FAKE_CODEX_MODE", "human")
    fake_server = Path(__file__).with_name("fake_codex_app_server.py")
    command = subprocess.list2cmdline([sys.executable, str(fake_server)])
    workflow = load_workflow(write_workflow(tmp_path, command))
    await authenticated_api.post(
        "/api/features", json=feature_payload(), headers=headers
    )
    await authenticated_api.post(
        "/api/work-items",
        json=work_item_payload("WI-001", status="ready"),
        headers=headers,
    )
    tracker = ControlPlaneTracker(
        workflow.tracker,
        transport=httpx.ASGITransport(app=authenticated_api.app),
    )
    try:
        async with WindowsSymphony(workflow, tracker=tracker) as orchestrator:
            outcomes = await orchestrator.run_once()
    finally:
        await tracker.close()

    assert outcomes[0].status == "work_item_released"
    item = (
        await authenticated_api.get("/api/work-items/WI-001", headers=headers)
    ).json()
    assert item["status"] == "needs_human"
    assert item["claim"] == {"worker_id": None, "expires_at": None}
    decisions = (
        await authenticated_api.get("/api/work-items/WI-001/decisions", headers=headers)
    ).json()
    assert len(decisions) == 1
    assert decisions[0]["status"] == "open"
    resolved = await authenticated_api.post(
        "/api/work-items/WI-001/decisions",
        headers=headers,
        json={
            "action": "resolve",
            "decision_id": decisions[0]["id"],
            "response": "approve",
            "actor_id": "human-reviewer",
        },
    )
    assert resolved.status_code == 200, resolved.text
    resumed = (
        await authenticated_api.get("/api/work-items/WI-001", headers=headers)
    ).json()
    assert resumed["status"] == "ready"
    attempts = (
        await authenticated_api.get("/api/work-items/WI-001/attempts", headers=headers)
    ).json()
    assert attempts[0]["status"] == "needs_human"
    assert attempts[0]["thread_id"] == "thread-test"
    assert attempts[0]["turn_id"] == "turn-test"

    monkeypatch.setenv("FAKE_CODEX_MODE", "resume")
    resumed_tracker = ControlPlaneTracker(
        workflow.tracker,
        transport=httpx.ASGITransport(app=authenticated_api.app),
    )
    try:
        async with WindowsSymphony(workflow, tracker=resumed_tracker) as orchestrator:
            resumed_outcomes = await orchestrator.run_once()
    finally:
        await resumed_tracker.close()
    assert resumed_outcomes[0].thread_id == "thread-test"
    attempts = (
        await authenticated_api.get("/api/work-items/WI-001/attempts", headers=headers)
    ).json()
    assert [attempt["thread_id"] for attempt in attempts] == [
        "thread-test",
        "thread-test",
    ]


async def test_missing_profile_is_blocked_without_builder_fallback(
    authenticated_api,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = {"Authorization": "Bearer integration-secret"}
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "integration-secret")
    workflow = load_workflow(write_workflow(tmp_path, "codex app-server"))
    await authenticated_api.post(
        "/api/features", json=feature_payload(), headers=headers
    )
    await authenticated_api.post(
        "/api/work-items",
        json=work_item_payload("WI-002", status="ready"),
        headers=headers,
    )
    tracker = ControlPlaneTracker(
        workflow.tracker,
        transport=httpx.ASGITransport(app=authenticated_api.app),
    )
    try:
        async with WindowsSymphony(workflow, tracker=tracker) as orchestrator:
            outcomes = await orchestrator.run_once()
    finally:
        await tracker.close()

    assert outcomes[0].status == "blocked"
    assert "backend_builder" in (outcomes[0].error or "")
    item = (
        await authenticated_api.get("/api/work-items/WI-002", headers=headers)
    ).json()
    assert item["status"] == "blocked"
    assert item["blocker"]["code"] == "agent_profile_configuration_error"
    attempts = (
        await authenticated_api.get("/api/work-items/WI-002/attempts", headers=headers)
    ).json()
    assert attempts[0]["profile_snapshot"] == {}


async def test_profile_concurrency_is_enforced_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "integration-secret")
    workflow = load_workflow(
        write_workflow(tmp_path, "codex app-server", include_backend=True)
    )

    def candidate(item_id: str, role: str) -> dict[str, Any]:
        return {
            "id": item_id,
            "identifier": item_id,
            "title": item_id,
            "description": "profile concurrency fixture",
            "agent_role": role,
            "stage": "tech_analysis"
            if role == "solution_architect"
            else "implementation",
            "status": "ready",
            "version": 1,
            "priority": 1,
            "created_at": item_id,
        }

    class FakeTracker:
        def __init__(self) -> None:
            self.claimed: list[tuple[str, dict[str, Any] | None]] = []
            self.items = [
                candidate("WI-001", "solution_architect"),
                candidate("WI-002", "backend_builder"),
                candidate("WI-004", "solution_architect"),
            ]

        async def register_worker(
            self, *, capacity: int, profiles: list[str]
        ) -> dict[str, Any]:
            return {"capacity": capacity, "profiles": profiles}

        async def heartbeat_worker(
            self, *, active_profiles: dict[str, str]
        ) -> dict[str, Any]:
            return {"active_profiles": active_profiles, "stop_requested": False}

        async def worker_stopped(self) -> dict[str, Any]:
            return {"state": "stopped"}

        async def maintenance_tick(self) -> dict[str, int]:
            return {"expired": 0, "released": 0}

        async def candidates(self, limit: int = 100) -> list[dict[str, Any]]:
            return self.items[:limit]

        async def claim(
            self, item: dict[str, Any], profile: dict[str, Any] | None = None
        ) -> ClaimLease:
            self.claimed.append((item["id"], profile))
            return ClaimLease(
                item={**item, "status": "running"},
                token="x" * 32,
                attempt={"attempt_number": 1},
            )

        async def execute_tool(
            self, lease: ClaimLease, name: str, arguments: Any
        ) -> ToolExecution:
            return ToolExecution({"success": True, "result": {}}, stop_agent=False)

        async def release(
            self, lease: ClaimLease, reason: str, *, retry_delay_seconds: int
        ) -> dict[str, Any]:
            lease.active = False
            return lease.item

        async def heartbeat(self, lease: ClaimLease) -> dict[str, Any]:
            return lease.item

    class HoldingCodex:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def run(self, *_args: Any, **_kwargs: Any) -> CodexRunResult:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    tracker = FakeTracker()
    async with WindowsSymphony(
        workflow,
        tracker=tracker,  # type: ignore[arg-type]
        codex_factory=HoldingCodex,  # type: ignore[arg-type]
    ) as orchestrator:
        dispatched = await orchestrator.tick()
        assert dispatched == ["WI-001", "WI-002"]
        assert [item_id for item_id, _profile in tracker.claimed] == [
            "WI-001",
            "WI-002",
        ]
        assert tracker.claimed[0][1]["name"] == "solution_architect"  # type: ignore[index]
        assert tracker.claimed[1][1]["name"] == "backend_builder"  # type: ignore[index]


async def test_skill_allowlist_uses_pinned_revision_not_live_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "integration-secret")
    workflow = load_workflow(write_workflow(tmp_path, "codex app-server"))
    live_instruction = (
        tmp_path / "skill-repository" / "skills" / "fskill-analysis-tech" / "SKILL.md"
    )
    live_instruction.write_text(
        live_instruction.read_text(encoding="utf-8") + "\nUNPINNED LIVE CHANGE\n",
        encoding="utf-8",
    )

    manager = SkillManager(workflow.skill_repository, workflow.agent_profiles)
    await manager.initialize()
    workspace = tmp_path / "isolated-workspace"
    workspace.mkdir()
    lock = manager.install(workflow.agent_profiles[0], workspace)

    installed = (
        workspace / ".agents" / "skills" / "fskill-analysis-tech" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "UNPINNED LIVE CHANGE" not in installed
    assert lock["skill_repository"]["revision"] == workflow.skill_repository.revision
    assert list(lock["skills"]) == ["fskill-analysis-tech"]


async def test_missing_or_incompatible_skill_fails_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "integration-secret")
    workflow_path = write_workflow(tmp_path, "codex app-server")
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace(
            "- fskill-analysis-tech", "- removed-skill"
        ),
        encoding="utf-8",
    )
    missing = load_workflow(workflow_path)
    with pytest.raises(SkillError, match="does not exist at pinned revision"):
        await SkillManager(
            missing.skill_repository, missing.agent_profiles
        ).initialize()

    valid = load_workflow(
        write_workflow(tmp_path / "invalid-reference", "codex app-server")
    )
    repository = tmp_path / "invalid-reference" / "skill-repository"
    instruction = repository / "skills" / "fskill-analysis-tech" / "SKILL.md"
    instruction.write_text(
        instruction.read_text(encoding="utf-8")
        + "\nRead [missing guidance](references/missing.md).\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repository), "add", "skills"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "--quiet", "-m", "break reference"],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    incompatible = replace(
        valid,
        skill_repository=replace(valid.skill_repository, revision=revision),
    )
    with pytest.raises(SkillError, match="references missing file"):
        await SkillManager(
            incompatible.skill_repository, incompatible.agent_profiles
        ).initialize()


def test_workflow_is_strict_and_workspace_keys_are_windows_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "integration-secret")
    workflow = load_workflow(write_workflow(tmp_path, "codex app-server"))
    assert workflow.codex.approval_policy == "never"
    assert workflow.codex.thread_sandbox == "danger-full-access"
    assert workflow.codex.turn_sandbox_policy == {"type": "dangerFullAccess"}
    with pytest.raises(WorkflowError, match="prompt rendering failed"):
        replace(
            workflow.agent_profiles[0], prompt_template="{{ issue.unknown }}"
        ).render_prompt({"id": "WI-001"}, None)
    with pytest.raises(WorkflowError, match="no agent profile matches"):
        workflow.profile_for({"agent_role": "backend_builder"})

    reviewer = replace(
        workflow.agent_profiles[0],
        name="code_reviewer",
        agent_role="code_reviewer",
        sandbox="read-only",
        network_access=False,
    )
    reviewer_codex = reviewer.codex_config(workflow.codex)
    assert reviewer_codex.thread_sandbox == "read-only"
    assert reviewer_codex.turn_sandbox_policy == {"type": "readOnly"}

    workflow_path = workflow.path
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace(
            "    sandbox: workspace-write\n", ""
        ),
        encoding="utf-8",
    )
    defaulted = load_workflow(workflow_path)
    assert defaulted.agent_profiles[0].sandbox == "danger-full-access"
    defaulted_codex = defaulted.agent_profiles[0].codex_config(defaulted.codex)
    assert defaulted_codex.thread_sandbox == "danger-full-access"
    assert defaulted_codex.turn_sandbox_policy == {"type": "dangerFullAccess"}

    assert workspace_key("WI-001") == "WI-001"
    assert workspace_key("CON").startswith("CON-")
    assert workspace_key("team/item 1").startswith("team_item_1-")

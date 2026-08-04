from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from conftest import feature_payload, work_item_payload
from symphony_windows.orchestrator import WindowsSymphony
from symphony_windows.tracker import ControlPlaneTracker
from symphony_windows.workflow import WorkflowError, load_workflow
from symphony_windows.workspace import workspace_key


def write_workflow(path: Path, command: str) -> Path:
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


async def test_bound_block_tool_releases_claim(
    authenticated_api,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = {"Authorization": "Bearer integration-secret"}
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "integration-secret")
    workflow = load_workflow(write_workflow(tmp_path, "codex app-server"))
    await authenticated_api.post("/api/features", json=feature_payload(), headers=headers)
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
            {"code": "missing_dependency", "message": "Required service is unavailable."},
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
    await authenticated_api.post("/api/features", json=feature_payload(), headers=headers)
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


def test_workflow_is_strict_and_workspace_keys_are_windows_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "integration-secret")
    workflow = load_workflow(write_workflow(tmp_path, "codex app-server"))
    with pytest.raises(WorkflowError, match="prompt rendering failed"):
        workflow.__class__(
            **{**workflow.__dict__, "prompt_template": "{{ issue.unknown }}"}
        ).render_prompt({"id": "WI-001"}, None)

    assert workspace_key("WI-001") == "WI-001"
    assert workspace_key("CON").startswith("CON-")
    assert workspace_key("team/item 1").startswith("team_item_1-")

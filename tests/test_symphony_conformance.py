from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from symphony_windows.orchestrator import (
    AttemptOutcome,
    WindowsSymphony,
    issue_dispatch_eligible,
    issue_routable,
    state_concurrency_available,
)
from symphony_windows.tracker import ClaimLease
from symphony_windows.workflow import WorkflowError, load_workflow


ROOT = Path(__file__).resolve().parents[1]


def _workflow(tmp_path: Path, monkeypatch, edits: tuple[tuple[str, str], ...] = ()):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setenv("FSHOWS_SKILLS_REPOSITORY", "https://example.test/skills.git")
    monkeypatch.setenv("FSHOWS_SKILLS_REVISION", "1" * 40)
    source = (ROOT / "WORKFLOW.md").read_text(encoding="utf-8")
    for before, after in edits:
        source = source.replace(before, after)
    path = tmp_path / "WORKFLOW.md"
    path.write_text(source, encoding="utf-8")
    return load_workflow(path)


def test_workflow_normalizes_dispatch_policy_and_state_limits(tmp_path, monkeypatch):
    workflow = _workflow(
        tmp_path,
        monkeypatch,
        (
            ("required_labels: []", "required_labels: [Backend, API]"),
            (
                "max_concurrent_agents_by_state: {}",
                "max_concurrent_agents_by_state:\n    READY: 1\n    running: 2",
            ),
        ),
    )
    assert workflow.tracker.required_labels == ("backend", "api")
    assert workflow.tracker.active_states == ("ready", "running")
    assert workflow.tracker.terminal_states == ("done", "cancelled")
    assert workflow.agent.max_concurrent_agents_by_state == {"ready": 1, "running": 2}


def test_workflow_rejects_overlapping_active_and_terminal_states(tmp_path, monkeypatch):
    with pytest.raises(WorkflowError, match="must be disjoint"):
        _workflow(
            tmp_path,
            monkeypatch,
            (("    - cancelled\n  provider:", "    - running\n  provider:"),),
        )


def test_dispatch_eligibility_is_scheduler_owned(tmp_path, monkeypatch):
    base = _workflow(tmp_path, monkeypatch)
    workflow = replace(
        base,
        tracker=replace(base.tracker, required_labels=("backend",)),
        agent=replace(base.agent, max_concurrent_agents_by_state={"ready": 1}),
    )
    eligible = {
        "id": "opaque-1",
        "identifier": "TEAM-1",
        "title": "Implement API",
        "state": "READY",
        "labels": ["backend", "api"],
        "dispatchable": True,
    }
    assert issue_routable(eligible, workflow) is True
    assert issue_dispatch_eligible(eligible, workflow) is True
    assert issue_dispatch_eligible({**eligible, "dispatchable": False}, workflow) is False
    assert issue_dispatch_eligible({**eligible, "labels": ["api"]}, workflow) is False
    assert issue_dispatch_eligible({**eligible, "state": "done"}, workflow) is False
    assert state_concurrency_available("READY", 0, workflow) is True
    assert state_concurrency_available("ready", 1, workflow) is False
    assert state_concurrency_available("running", 3, workflow) is True


async def test_tick_enforces_required_labels_and_per_state_concurrency(
    tmp_path, monkeypatch
):
    base = _workflow(tmp_path, monkeypatch)
    workflow = replace(
        base,
        tracker=replace(base.tracker, required_labels=("backend",)),
        agent=replace(
            base.agent,
            max_concurrent_agents=4,
            max_concurrent_agents_by_state={"ready": 1},
        ),
    )
    candidates = [
        {
            "id": "opaque-1",
            "identifier": "TEAM-1",
            "title": "First",
            "state": "ready",
            "status": "ready",
            "labels": ["backend"],
            "dispatchable": True,
            "priority": 1,
            "version": 1,
        },
        {
            "id": "opaque-2",
            "identifier": "TEAM-2",
            "title": "Second",
            "state": "ready",
            "status": "ready",
            "labels": ["backend"],
            "dispatchable": True,
            "priority": 2,
            "version": 1,
        },
        {
            "id": "opaque-3",
            "identifier": "TEAM-3",
            "title": "Wrong label",
            "state": "ready",
            "status": "ready",
            "labels": ["frontend"],
            "dispatchable": True,
            "priority": 1,
            "version": 1,
        },
    ]

    class Adapter:
        config = workflow.tracker

        async def fetch_issues_by_states(self, states):
            return [] if set(states) == set(workflow.tracker.terminal_states) else candidates

        async def fetch_issues_by_ids(self, _ids):
            return []

        async def register_worker(self, **_kwargs):
            return {}

        async def heartbeat_worker(self, **_kwargs):
            return {"stop_requested": False}

        async def maintenance_tick(self):
            return {}

        async def claim(self, issue, _snapshot):
            return ClaimLease(
                issue={**issue, "state": "running", "status": "running"},
                token="token",
                attempt={"attempt_number": 1},
            )

        async def worker_stopped(self):
            return {}

        async def close(self):
            return None

    class Skills:
        async def initialize(self):
            return None

        def snapshot(self):
            return {"kind": "coding_agent"}

    class Workspaces:
        async def remove(self, _issue):
            return False

    symphony = WindowsSymphony(
        workflow,
        tracker=Adapter(),  # type: ignore[arg-type]
        workspace_manager=Workspaces(),  # type: ignore[arg-type]
    )
    never = asyncio.Event()

    async def hold(entry) -> AttemptOutcome:
        await never.wait()
        return AttemptOutcome(entry.issue_id, "completed")

    symphony._run_claimed = hold  # type: ignore[method-assign]
    try:
        assert await symphony.tick() == ["opaque-1"]
    finally:
        await symphony._stop_running()

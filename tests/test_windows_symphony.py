from __future__ import annotations

import httpx

from symphony_windows.tracker import ControlPlaneTracker
from symphony_windows.workflow import TrackerConfig
from conftest import issue_payload


def _tracker(api) -> ControlPlaneTracker:
    return ControlPlaneTracker(
        TrackerConfig(
            endpoint="http://test",
            token="unused",
            worker_id="windows-symphony-test",
            project_id=api.project_id,
            lease_seconds=300,
        ),
        transport=httpx.ASGITransport(app=api.app),
    )


async def test_tracker_claims_issue_and_exposes_only_issue_tools(api):
    await api.post("/api/issues", json=issue_payload())
    tracker = _tracker(api)
    try:
        await tracker.register_worker(capacity=1)
        candidate = (await tracker.candidates())[0]
        lease = await tracker.claim(candidate, {"kind": "coding_agent", "max_turns": 20})
        assert lease.id == "ISSUE-001"
        assert lease.attempt["attempt_number"] == 1
        names = {tool["name"] for tool in tracker.tool_specs()}
        assert names == {
            "issue_get",
            "issue_add_event",
            "issue_add_artifact",
            "issue_request_human",
            "issue_complete",
            "issue_block",
        }
        assert not any("work_item" in name or "handoff" in name for name in names)
    finally:
        await tracker.close()


async def test_tracker_completion_ends_claim_and_enters_review(api):
    await api.post("/api/issues", json=issue_payload())
    tracker = _tracker(api)
    try:
        issue = (await tracker.candidates())[0]
        lease = await tracker.claim(issue, {"kind": "coding_agent"})
        await tracker.update_attempt_context(
            lease, thread_id="thread-one", turn_id="turn-two", turn_count=2
        )
        result = await tracker.execute_tool(
            lease, "issue_complete", {}, thread_id="thread-one"
        )
        assert result.stop_agent is True
        assert result.response["success"] is True
        assert lease.active is False
        current = await tracker.get_issue("ISSUE-001")
        assert current["status"] == "reviewing"
    finally:
        await tracker.close()


async def test_tracker_human_request_preserves_thread_for_resume(api):
    await api.post("/api/issues", json=issue_payload())
    tracker = _tracker(api)
    try:
        lease = await tracker.claim((await tracker.candidates())[0], {"kind": "coding_agent"})
        result = await tracker.execute_tool(
            lease,
            "issue_request_human",
            {"question": "Choose behavior", "options": ["A", "B"]},
            thread_id="thread-human",
        )
        assert result.stop_agent is True
        decisions = (await api.get("/api/issues/ISSUE-001/decisions")).json()
        await api.post(
            "/api/issues/ISSUE-001/decisions",
            json={
                "action": "resolve",
                "decision_id": decisions[0]["id"],
                "response": "A",
                "actor_id": "tester",
            },
        )
        resumed = await tracker.claim((await tracker.candidates())[0], {"kind": "coding_agent"})
        assert resumed.resume_thread_id == "thread-human"
        assert resumed.resume_decisions[0]["response"] == "A"
    finally:
        await tracker.close()


async def test_tracker_claim_includes_human_follow_up_instruction(api):
    await api.post("/api/issues", json=issue_payload())
    tracker = _tracker(api)
    try:
        lease = await tracker.claim((await tracker.candidates())[0], {"kind": "coding_agent"})
        await tracker.update_attempt_context(
            lease, thread_id="thread-follow-up", turn_id="turn-analysis", turn_count=1
        )
        await tracker.execute_tool(
            lease, "issue_complete", {}, thread_id="thread-follow-up"
        )
        reviewing = (await api.get("/api/issues/ISSUE-001")).json()
        response = await api.post(
            "/api/issues/ISSUE-001/continue",
            json={
                "expectedVersion": reviewing["version"],
                "instruction": "Continue with implementation and tests.",
            },
        )
        assert response.status_code == 200, response.text

        resumed = await tracker.claim(
            (await tracker.candidates())[0], {"kind": "coding_agent"}
        )
        assert resumed.resume_thread_id == "thread-follow-up"
        assert resumed.resume_instructions == [
            "Continue with implementation and tests."
        ]
        assert resumed.resume_decisions[-1]["requested_by"] == (
            "control-plane-followup"
        )
        assert resumed.resume_decisions[-1]["response"] == (
            "Continue with implementation and tests."
        )
    finally:
        await tracker.close()


async def test_tracker_rejects_unsafe_artifact_path_without_mutating_issue(api):
    await api.post("/api/issues", json=issue_payload())
    tracker = _tracker(api)
    try:
        lease = await tracker.claim((await tracker.candidates())[0], {"kind": "coding_agent"})
        result = await tracker.execute_tool(
            lease, "issue_add_artifact", {"path": "../secret", "revision": "tree"}
        )
        assert result.response["success"] is False
        assert lease.active is True
    finally:
        await tracker.close()


async def test_tracker_refreshes_active_claim_and_lists_terminal_issues(api):
    await api.post("/api/issues", json=issue_payload())
    tracker = _tracker(api)
    try:
        lease = await tracker.claim(
            (await tracker.candidates())[0], {"kind": "coding_agent"}
        )
        assert await tracker.refresh_claim(lease) is True
        assert [row["id"] for row in await tracker.issues_by_ids(["ISSUE-001"])] == [
            "ISSUE-001"
        ]
        cancelled = await api.post(
            "/api/issues/ISSUE-001/status",
            json={
                "toStatus": "cancelled",
                "event": "cancelled",
                "actorType": "human",
                "actorId": "operator",
                "payload": {},
            },
        )
        assert cancelled.status_code == 200
        assert await tracker.refresh_claim(lease) is False
        assert lease.active is False
        assert [row["id"] for row in await tracker.terminal_issues()] == ["ISSUE-001"]
    finally:
        await tracker.close()


async def test_tracker_adapter_fetches_and_normalizes_issues_by_state_and_id(api):
    payload = issue_payload()
    payload.update(
        {
            "identifier": "TEAM-9",
            "labels": ["Backend", "API"],
            "blocked_by": [{"identifier": "TEAM-8", "state": "running"}],
            "dispatchable": True,
        }
    )
    assert (await api.post("/api/issues", json=payload)).status_code == 201
    tracker = _tracker(api)
    try:
        by_state = await tracker.fetch_issues_by_states(["ready"])
        assert len(by_state) == 1
        issue = by_state[0]
        assert issue["identifier"] == "TEAM-9"
        assert issue["state"] == "ready"
        assert issue["labels"] == ["backend", "api"]
        assert issue["blocked_by"] == [
            {"id": None, "identifier": "TEAM-8", "state": "running"}
        ]
        assert issue["dispatchable"] is False
        assert (await tracker.fetch_issues_by_ids(["ISSUE-001"]))[0]["id"] == "ISSUE-001"
    finally:
        await tracker.close()


async def test_tracker_adapter_skips_provider_request_for_empty_filters(api):
    tracker = _tracker(api)
    try:
        assert await tracker.fetch_issues_by_states([]) == []
        assert await tracker.fetch_issues_by_ids([]) == []
    finally:
        await tracker.close()

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

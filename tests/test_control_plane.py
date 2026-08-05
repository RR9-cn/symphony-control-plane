from __future__ import annotations

import asyncio

from control_plane.delivery import IssueDeliveryManager
from conftest import claim_payload, issue_payload


async def _create_and_claim(api):
    created = (await api.post("/api/issues", json=issue_payload())).json()
    response = await api.post("/api/issues/ISSUE-001/claim", json=claim_payload(created["version"]))
    assert response.status_code == 200, response.text
    return response.json()


async def test_dashboard_and_issue_crud(api):
    dashboard = await api.get("/")
    assert dashboard.status_code == 200
    assert 'id="issue-cancel-button" type="submit" value="cancel" formnovalidate' in dashboard.text
    assert 'id="issue-close-button" type="submit" value="cancel" formnovalidate' in dashboard.text
    assert '/ui/assets/app.js?v=20260805-v2' in dashboard.text
    created = await api.post("/api/issues", json=issue_payload())
    assert created.status_code == 201
    assert created.json()["status"] == "ready"
    assert [row["id"] for row in (await api.get("/api/issues/candidates", params={"project_id": api.project_id})).json()] == ["ISSUE-001"]

    patched = await api.patch(
        "/api/issues/ISSUE-001",
        json={"expectedVersion": 1, "title": "Updated endpoint"},
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "Updated endpoint"
    assert patched.json()["version"] == 2


async def test_issue_exposes_normalized_symphony_dispatch_fields(api):
    payload = issue_payload()
    payload.update(
        {
            "identifier": "TEAM-42",
            "url": "https://tracker.test/TEAM-42",
            "assignee_id": "user-7",
            "labels": ["Backend", "API"],
            "blocked_by": [
                {"id": "dependency-1", "identifier": "TEAM-41", "state": "DONE"}
            ],
            "native_ref": {"project_item_id": "PVTI_1"},
            "dispatchable": True,
            "branch_name": "team-42",
        }
    )
    response = await api.post("/api/issues", json=payload)
    assert response.status_code == 201, response.text
    issue = response.json()
    assert issue["identifier"] == "TEAM-42"
    assert issue["state"] == "ready"
    assert issue["labels"] == ["backend", "api"]
    assert issue["blocked_by"][0]["state"] == "done"
    assert issue["native_ref"] == {"project_item_id": "PVTI_1"}
    assert issue["dispatchable"] is True
    assert issue["branch_name"] == "team-42"


async def test_issue_identifier_is_unique(api):
    first = issue_payload("ISSUE-001")
    first["identifier"] = "TEAM-1"
    second = issue_payload("ISSUE-002")
    second["identifier"] = "TEAM-1"
    assert (await api.post("/api/issues", json=first)).status_code == 201
    duplicate = await api.post("/api/issues", json=second)
    assert duplicate.status_code == 409


async def test_claim_is_atomic_and_attempt_is_created(api):
    await api.post("/api/issues", json=issue_payload())
    first, second = await asyncio.gather(
        api.post("/api/issues/ISSUE-001/claim", json=claim_payload()),
        api.post("/api/issues/ISSUE-001/claim", json=claim_payload()),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    winner = first if first.status_code == 200 else second
    body = winner.json()
    assert body["issue"]["status"] == "running"
    assert body["attempt"]["attempt_number"] == 1
    assert body["attempt"]["config_snapshot"]["kind"] == "coding_agent"


async def test_running_issue_accepts_routing_metadata_updates_only(api):
    lease = await _create_and_claim(api)
    updated = await api.patch(
        "/api/issues/ISSUE-001",
        json={
            "expectedVersion": lease["issue"]["version"],
            "dispatchable": False,
            "labels": ["Backend"],
            "blocked_by": [{"identifier": "ISSUE-000", "state": "running"}],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["dispatchable"] is False
    assert updated.json()["labels"] == ["backend"]
    rejected = await api.patch(
        "/api/issues/ISSUE-001",
        json={
            "expectedVersion": updated.json()["version"],
            "title": "Cannot rewrite a running task",
        },
    )
    assert rejected.status_code == 409


async def test_human_can_cancel_running_issue_without_claim_token(api):
    lease = await _create_and_claim(api)
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
    assert cancelled.status_code == 200, cancelled.text
    issue = cancelled.json()
    assert issue["status"] == "cancelled"
    assert issue["claim"]["worker_id"] is None
    assert issue["claim"]["expires_at"] is None
    attempts = (await api.get("/api/issues/ISSUE-001/attempts")).json()
    assert attempts[0]["id"] == lease["attempt"]["id"]
    assert attempts[0]["status"] == "cancelled"


async def test_single_attempt_supports_multiple_turns_and_completion(api):
    lease = await _create_and_claim(api)
    token = lease["claim_token"]
    attempt_id = lease["attempt"]["id"]
    context = await api.post(
        "/api/issues/ISSUE-001/attempt-context",
        json={"claimToken": token, "threadId": "thread-1", "turnId": "turn-3", "turnCount": 3},
    )
    assert context.status_code == 200
    assert context.json()["turn_count"] == 3

    event = await api.post(
        f"/api/issues/ISSUE-001/attempts/{attempt_id}/events",
        json={
            "claimToken": token,
            "event_type": "item_completed",
            "item_type": "commandExecution",
            "status": "completed",
            "summary": "pytest passed",
        },
    )
    assert event.status_code == 201
    completed = await api.post(
        "/api/issues/ISSUE-001/status",
        json={
            "toStatus": "reviewing",
            "event": "agent_completed",
            "actorType": "worker",
            "claimToken": token,
            "payload": {"thread_id": "thread-1"},
        },
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "reviewing"
    attempts = (await api.get("/api/issues/ISSUE-001/attempts")).json()
    assert attempts[0]["status"] == "reviewing"
    assert attempts[0]["thread_id"] == "thread-1"
    assert attempts[0]["turn_count"] == 3
    assert attempts[0]["session_id"] == "thread-1"
    assert attempts[0]["status_reason"] is None
    assert attempts[0]["duration_seconds"] >= 0


async def test_release_retries_and_resumes_same_thread(api):
    lease = await _create_and_claim(api)
    released = await api.post(
        "/api/issues/ISSUE-001/release",
        json={
            "claimToken": lease["claim_token"],
            "reason": "continuation_after_max_turns",
            "retryDelaySeconds": 0,
            "threadId": "thread-keep",
        },
    )
    assert released.json()["status"] == "retry_queued"
    attempts = (await api.get("/api/issues/ISSUE-001/attempts")).json()
    assert attempts[0]["status_reason"] == "continuation_after_max_turns"
    assert attempts[0]["session_id"] == "thread-keep"
    assert attempts[0]["duration_seconds"] >= 0
    assert (await api.post("/api/maintenance/tick")).json()["readied"] == 1
    current = (await api.get("/api/issues/ISSUE-001")).json()
    resumed = await api.post("/api/issues/ISSUE-001/claim", json=claim_payload(current["version"]))
    assert resumed.status_code == 200
    assert resumed.json()["resume_thread_id"] == "thread-keep"
    assert resumed.json()["attempt"]["attempt_number"] == 2


async def test_human_decision_returns_issue_to_ready_and_resume_context(api):
    lease = await _create_and_claim(api)
    requested = await api.post(
        "/api/issues/ISSUE-001/decisions",
        json={
            "action": "request",
            "question": "Which compatibility behavior?",
            "options": ["None", "Legacy"],
            "claimToken": lease["claim_token"],
            "threadId": "thread-human",
        },
    )
    assert requested.status_code == 200
    decision = requested.json()
    assert (await api.get("/api/issues/ISSUE-001")).json()["status"] == "needs_human"
    resolved = await api.post(
        "/api/issues/ISSUE-001/decisions",
        json={"action": "resolve", "decision_id": decision["id"], "response": "None", "actor_id": "user"},
    )
    assert resolved.status_code == 200
    current = (await api.get("/api/issues/ISSUE-001")).json()
    resumed = (await api.post("/api/issues/ISSUE-001/claim", json=claim_payload(current["version"]))).json()
    assert resumed["resume_thread_id"] == "thread-human"
    assert resumed["resume_decisions"][0]["response"] == "None"


async def test_artifacts_events_workers_and_runtime(api):
    lease = await _create_and_claim(api)
    token = lease["claim_token"]
    artifact = await api.post(
        "/api/issues/ISSUE-001/artifacts",
        json={"path": "docs/result.md", "revision": "working-tree", "claimToken": token},
    )
    assert artifact.status_code == 201
    assert (await api.post(
        "/api/issues/ISSUE-001/artifacts",
        json={"path": "../escape", "revision": "bad", "claimToken": token},
    )).status_code == 409

    registered = await api.post(
        "/api/workers/register",
        json={"workerId": "windows-symphony-managed", "projectId": api.project_id, "hostname": "host", "processId": 100, "version": "0.2", "capacity": 4},
    )
    assert registered.status_code == 200
    heartbeat = await api.post(
        "/api/workers/windows-symphony-managed/heartbeat",
        json={
            "state": "running",
            "activeIssues": ["ISSUE-001"],
            "runtimeSnapshot": {
                "generated_at": "2026-08-05T12:00:00Z",
                "project_id": api.project_id,
                "worker_id": "windows-symphony-managed",
                "running": [
                    {
                        "issue_id": "ISSUE-001",
                        "issue_identifier": "ISSUE-001",
                        "state": "running",
                        "attempt_id": lease["attempt"]["id"],
                        "attempt_number": 1,
                        "thread_id": "thread-live",
                        "turn_id": "turn-live",
                        "session_id": "thread-live",
                        "turn_count": 2,
                        "phase": "streaming_turn",
                        "codex_app_server_pid": 4321,
                        "last_event": "item/started",
                        "last_message": "Command started: pytest",
                        "started_at": "2026-08-05T11:59:00Z",
                        "last_event_at": "2026-08-05T12:00:00Z",
                        "duration_seconds": 60,
                        "workspace_path": "D:/workspaces/ISSUE-001",
                        "tokens": {
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "total_tokens": 120,
                        },
                    }
                ],
                "retrying": [],
                "codex_totals": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                    "seconds_running": 60,
                },
                "rate_limits": None,
            },
        },
    )
    assert heartbeat.json()["active_issues"] == ["ISSUE-001"]
    assert heartbeat.json()["runtime_snapshot_at"] is not None
    runtimes = (await api.get("/api/agent-runtimes")).json()
    assert runtimes[0]["state"] == "running"
    assert runtimes[0]["attempt_number"] == 1
    assert runtimes[0]["runtime_source"] == "orchestrator"
    assert runtimes[0]["session_id"] == "thread-live"
    assert runtimes[0]["phase"] == "streaming_turn"
    assert runtimes[0]["codex_app_server_pid"] == 4321
    assert runtimes[0]["last_event"] == "item/started"
    assert runtimes[0]["tokens"]["total_tokens"] == 120


async def test_authentication_protects_api_but_not_dashboard(authenticated_api):
    assert (await authenticated_api.get("/")).status_code == 200
    assert (await authenticated_api.get("/api/issues")).status_code == 401
    response = await authenticated_api.get(
        "/api/issues", headers={"Authorization": "Bearer integration-secret"}
    )
    assert response.status_code == 200


async def test_delivery_gates_use_compare_and_set_transactions(api, monkeypatch):
    lease = await _create_and_claim(api)
    await api.post(
        "/api/issues/ISSUE-001/status",
        json={
            "toStatus": "reviewing",
            "event": "agent_completed",
            "claimToken": lease["claim_token"],
            "payload": {},
        },
    )

    async def prepare(_self, issue_id, title):
        assert issue_id == "ISSUE-001"
        assert title == "Add user detail endpoint"
        return "codex/issue-001", "2" * 40

    async def publish(_self, issue_id, **kwargs):
        assert issue_id == "ISSUE-001"
        assert kwargs["commit"] == "2" * 40
        return "https://github.com/example/catalog/pull/1"

    async def verify(_self, repository_url, pull_request):
        assert repository_url.endswith("project")
        assert pull_request.endswith("/pull/1")

    monkeypatch.setattr(IssueDeliveryManager, "prepare_local_commit", prepare)
    monkeypatch.setattr(IssueDeliveryManager, "publish", publish)
    monkeypatch.setattr(IssueDeliveryManager, "verify_merged", verify)

    reviewing = (await api.get("/api/issues/ISSUE-001")).json()
    approved = await api.post(
        "/api/issues/ISSUE-001/delivery",
        json={"action": "approve_result", "expectedVersion": reviewing["version"], "authorization": True},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "awaiting_publish"

    published = await api.post(
        "/api/issues/ISSUE-001/delivery",
        json={"action": "authorize_publish", "expectedVersion": approved.json()["version"], "authorization": True},
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "pr_open"

    merged = await api.post(
        "/api/issues/ISSUE-001/delivery",
        json={"action": "confirm_merge", "expectedVersion": published.json()["version"], "authorization": True},
    )
    assert merged.status_code == 200, merged.text
    assert merged.json()["status"] == "done"


async def test_invalid_issue_shape_rejected(api):
    payload = issue_payload("ISSUE-002")
    payload["acceptance_criteria"] = ["same", "same"]
    assert (await api.post("/api/issues", json=payload)).status_code == 422

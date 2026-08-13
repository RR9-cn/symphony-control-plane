from __future__ import annotations

import asyncio
import hashlib
import subprocess
from pathlib import Path

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
    assert '/ui/assets/app.js?v=20260807-v13' in dashboard.text
    assert 'id="show-inactive" type="checkbox"' in dashboard.text
    unavailable = await api.get("/api/v1/state")
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "unavailable"
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


async def test_force_archive_removes_terminal_workspace_and_keeps_history(api):
    created = (await api.post("/api/issues", json=issue_payload())).json()
    workspace = Path(created["workspace_path"])
    source_repository = workspace.parents[1]
    subprocess.run(
        ["git", "clone", "--quiet", str(source_repository), str(workspace)],
        check=True,
        capture_output=True,
    )
    (workspace / "unpublished.txt").write_text("discarded\n", encoding="utf-8")
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

    archived = await api.post(
        "/api/issues/ISSUE-001/archive",
        json={
            "expectedVersion": cancelled.json()["version"],
            "authorization": True,
        },
    )

    assert archived.status_code == 200, archived.text
    body = archived.json()
    assert body["status"] == "cancelled"
    assert body["archived_at"] is not None
    assert body["change_summary"]["available"] is True
    assert body["change_summary"]["files_untracked"] == 1
    assert "unpublished.txt" in body["change_summary"]["changed_paths"]
    assert body["version"] == cancelled.json()["version"] + 1
    assert not workspace.exists()
    events = (await api.get("/api/issues/ISSUE-001/events")).json()
    archive_event = next(
        event for event in events if event["event"] == "workspace_force_archived"
    )
    assert archive_event["payload"]["removed"] is True
    assert archive_event["payload"]["workspace"] == str(workspace.resolve())

    repeated = await api.post(
        "/api/issues/ISSUE-001/archive",
        json={"expectedVersion": body["version"], "authorization": True},
    )
    assert repeated.status_code == 200
    assert repeated.json()["version"] == body["version"]
    assert repeated.json()["archived_at"] == body["archived_at"]


async def test_force_archive_cancels_ready_issue_and_requires_confirmation(api):
    created = await api.post("/api/issues", json=issue_payload())
    assert created.status_code == 201
    workspace = Path(created.json()["workspace_path"])
    workspace.mkdir(parents=True)

    unauthorized = await api.post(
        "/api/issues/ISSUE-001/archive",
        json={"expectedVersion": created.json()["version"], "authorization": False},
    )
    active = await api.post(
        "/api/issues/ISSUE-001/archive",
        json={"expectedVersion": created.json()["version"], "authorization": True},
    )

    assert unauthorized.status_code == 422
    assert active.status_code == 200, active.text
    assert active.json()["status"] == "cancelled"
    assert active.json()["archived_at"] is not None
    assert active.json()["version"] == created.json()["version"] + 2
    assert not workspace.exists()
    events = (await api.get("/api/issues/ISSUE-001/events")).json()
    assert any(event["event"] == "force_archive_cancelled" for event in events)
    assert any(event["event"] == "workspace_force_archived" for event in events)


async def test_force_archive_cancels_running_attempt_before_removal(api):
    lease = await _create_and_claim(api)
    workspace = Path(lease["issue"]["workspace_path"])
    workspace.mkdir(parents=True)
    (workspace / "agent-output.txt").write_text("partial\n", encoding="utf-8")
    overview = "新增账号归档流程，并补齐取消运行中任务的保护逻辑。"
    event = await api.post(
        f"/api/issues/ISSUE-001/attempts/{lease['attempt']['id']}/events",
        json={
            "claimToken": lease["claim_token"],
            "event_type": "agent_message_completed",
            "item_type": "agentMessage",
            "summary": "Agent message completed",
            "detail": overview,
        },
    )
    assert event.status_code == 201

    archived = await api.post(
        "/api/issues/ISSUE-001/archive",
        json={
            "expectedVersion": lease["issue"]["version"],
            "authorization": True,
        },
    )

    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "cancelled"
    assert archived.json()["claim"] == {"worker_id": None, "expires_at": None}
    assert archived.json()["archived_at"] is not None
    assert archived.json()["change_summary"]["overview"] == overview
    assert not workspace.exists()
    attempts = (await api.get("/api/issues/ISSUE-001/attempts")).json()
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
    assert attempts[0]["session_id"] == "thread-1-turn-3"
    assert attempts[0]["status_reason"] is None
    assert attempts[0]["duration_seconds"] >= 0


async def test_human_follow_up_resumes_reviewing_issue_in_same_workspace_and_thread(api):
    lease = await _create_and_claim(api)
    token = lease["claim_token"]
    await api.post(
        "/api/issues/ISSUE-001/artifacts",
        json={"path": "docs/analysis.md", "revision": "working-tree", "claimToken": token},
    )
    await api.post(
        "/api/issues/ISSUE-001/attempt-context",
        json={"claimToken": token, "threadId": "thread-analysis", "turnId": "turn-1", "turnCount": 1},
    )
    completed = await api.post(
        "/api/issues/ISSUE-001/status",
        json={
            "toStatus": "reviewing",
            "event": "agent_completed",
            "actorType": "worker",
            "claimToken": token,
            "payload": {"thread_id": "thread-analysis"},
        },
    )
    reviewing = completed.json()
    instruction = "方案确认，继续完成代码实现和测试。"
    continued = await api.post(
        "/api/issues/ISSUE-001/continue",
        json={"expectedVersion": reviewing["version"], "instruction": f"  {instruction}  "},
    )
    assert continued.status_code == 200, continued.text
    assert continued.json()["status"] == "ready"
    assert continued.json()["workspace_path"] == reviewing["workspace_path"]
    assert continued.json()["artifacts"][0]["path"] == "docs/analysis.md"
    events = (await api.get("/api/issues/ISSUE-001/events")).json()
    assert events[-1]["event"] == "human_followup_requested"
    assert events[-1]["payload"]["instruction"] == instruction

    resumed = (
        await api.post(
            "/api/issues/ISSUE-001/claim",
            json=claim_payload(continued.json()["version"]),
        )
    ).json()
    assert resumed["attempt"]["attempt_number"] == 2
    assert resumed["resume_thread_id"] == "thread-analysis"
    assert resumed["resume_instructions"] == [instruction]
    assert resumed["resume_decisions"][-1]["requested_by"] == "control-plane-followup"
    assert resumed["resume_decisions"][-1]["response"] == instruction

    await api.post(
        "/api/issues/ISSUE-001/attempt-context",
        json={
            "claimToken": resumed["claim_token"],
            "threadId": "thread-analysis",
            "turnId": "turn-2",
            "turnCount": 1,
        },
    )
    reviewed_again = await api.post(
        "/api/issues/ISSUE-001/status",
        json={
            "toStatus": "reviewing",
            "event": "agent_completed",
            "actorType": "worker",
            "claimToken": resumed["claim_token"],
        },
    )
    next_instruction = "不要再调整文档，直接实现当前方案。"
    continued_again = await api.post(
        "/api/issues/ISSUE-001/continue",
        json={
            "expectedVersion": reviewed_again.json()["version"],
            "instruction": next_instruction,
        },
    )
    resumed_again = (
        await api.post(
            "/api/issues/ISSUE-001/claim",
            json=claim_payload(continued_again.json()["version"]),
        )
    ).json()
    assert resumed_again["attempt"]["attempt_number"] == 3
    assert resumed_again["resume_instructions"] == [next_instruction]
    follow_up_decisions = [
        decision
        for decision in resumed_again["resume_decisions"]
        if decision["requested_by"] == "control-plane-followup"
    ]
    assert [decision["response"] for decision in follow_up_decisions] == [
        next_instruction
    ]


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
    assert attempts[0]["session_id"] is None
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


async def test_workspace_review_does_not_fall_back_to_parent_repository(api):
    created = await api.post("/api/issues", json=issue_payload())
    assert created.status_code == 201
    workspace = Path(created.json()["workspace_path"])
    parent = workspace.parent
    parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=parent, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=parent, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=parent, check=True)
    (parent / "parent.txt").write_text("parent repository\n", encoding="utf-8")
    subprocess.run(["git", "add", "parent.txt"], cwd=parent, check=True)
    subprocess.run(
        ["git", "commit", "-m", "parent"],
        cwd=parent,
        check=True,
        capture_output=True,
    )
    workspace.mkdir()

    review = await api.get("/api/issues/ISSUE-001/review")

    assert review.status_code == 409
    assert review.json()["error"]["message"] == (
        "Issue workspace Git checkout is incomplete; "
        "wait for initialization or retry the Issue"
    )


async def test_workspace_review_hides_unborn_head_git_error(api):
    created = await api.post("/api/issues", json=issue_payload())
    assert created.status_code == 201
    workspace = Path(created.json()["workspace_path"])
    workspace.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)

    review = await api.get("/api/issues/ISSUE-001/review")

    assert review.status_code == 409
    message = review.json()["error"]["message"]
    assert message == (
        "Issue workspace Git checkout is incomplete; "
        "wait for initialization or retry the Issue"
    )
    assert "ambiguous argument 'HEAD'" not in message


async def test_artifacts_events_workers_and_runtime(api):
    lease = await _create_and_claim(api)
    token = lease["claim_token"]
    issue = (await api.get("/api/issues/ISSUE-001")).json()
    workspace = Path(issue["workspace_path"])
    source_repository = workspace.parents[1]
    subprocess.run(
        ["git", "clone", "--quiet", str(source_repository), str(workspace)],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
    base = workspace / "base.txt"
    base.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=workspace, check=True, capture_output=True)
    base.write_text("after\n", encoding="utf-8")
    artifact_file = workspace / "docs" / "result.md"
    artifact_file.parent.mkdir(parents=True)
    artifact_bytes = b"# Result\n\nValidated.\n"
    artifact_file.write_bytes(artifact_bytes)
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    overview = "新增用户详情接口，补充权限校验，并完善接口测试与开发文档。"
    agent_message = await api.post(
        f"/api/issues/ISSUE-001/attempts/{lease['attempt']['id']}/events",
        json={
            "claimToken": token,
            "event_type": "agent_message_completed",
            "item_type": "agentMessage",
            "summary": "Agent message completed",
            "detail": overview,
        },
    )
    assert agent_message.status_code == 201
    artifact = await api.post(
        "/api/issues/ISSUE-001/artifacts",
        json={
            "path": "docs/result.md",
            "revision": "working-tree",
            "media_type": "text/markdown",
            "sha256": artifact_sha256,
            "claimToken": token,
        },
    )
    assert artifact.status_code == 201
    preview = await api.get(
        f"/api/issues/ISSUE-001/artifacts/{artifact.json()['id']}"
    )
    assert preview.status_code == 200
    assert preview.json()["content"] == artifact_bytes.decode()
    assert preview.json()["current_sha256"] == artifact_sha256
    assert preview.json()["registered_sha256_matches"] is True
    review = await api.get("/api/issues/ISSUE-001/review")
    assert review.status_code == 200
    assert review.json()["base_commit"] == issue["source_commit"]
    assert review.json()["head_commit"] != issue["source_commit"]
    assert review.json()["commits"][0].endswith("base")
    assert any("base.txt" in path for path in review.json()["changed_files"])
    assert " M base.txt" in review.json()["status"]
    assert "?? docs/result.md" in review.json()["status"]
    assert "base.txt" in review.json()["diff_stat"]
    assert "+after" in review.json()["diff"]
    summary = review.json()["change_summary"]
    assert summary["available"] is True
    assert summary["overview"] == overview
    assert summary["files_total"] == 2
    assert summary["files_added"] == 1
    assert summary["files_untracked"] == 1
    assert summary["commit_count"] == 1
    assert summary["additions"] >= 1
    assert set(summary["changed_paths"]) == {"base.txt", "docs/result.md"}
    assert (await api.get("/api/issues/ISSUE-001")).json()["change_summary"][
        "overview"
    ] == overview
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
                        "session_id": "thread-live-turn-live",
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
                "retrying": [
                    {
                        "issue_id": "ISSUE-RETRY",
                        "issue_identifier": "ISSUE-RETRY",
                        "attempt": 2,
                        "due_at": "2026-08-05T12:01:00Z",
                        "error": "temporary failure",
                    }
                ],
                "codex_totals": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                    "seconds_running": 60,
                },
                "rate_limits": {"primary": {"used_percent": 42}},
            },
        },
    )
    assert heartbeat.json()["active_issues"] == ["ISSUE-001"]
    assert heartbeat.json()["runtime_snapshot_at"] is not None
    runtimes = (await api.get("/api/agent-runtimes")).json()
    assert runtimes[0]["state"] == "running"
    assert runtimes[0]["attempt_number"] == 1
    assert runtimes[0]["runtime_source"] == "orchestrator"
    assert runtimes[0]["session_id"] == "thread-live-turn-live"
    assert runtimes[0]["phase"] == "streaming_turn"
    assert runtimes[0]["codex_app_server_pid"] == 4321
    assert runtimes[0]["last_event"] == "item/started"
    assert runtimes[0]["tokens"]["total_tokens"] == 120
    standard = await api.get("/api/v1/state")
    assert standard.status_code == 200
    assert standard.json()["counts"] == {"running": 1, "retrying": 1}
    assert standard.json()["codex_totals"]["total_tokens"] == 120
    assert standard.json()["rate_limits"]["primary"]["used_percent"] == 42
    details = await api.get("/api/v1/ISSUE-001")
    assert details.status_code == 200
    assert details.json()["running"]["session_id"] == "thread-live-turn-live"


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

    async def base_unchanged(_self, **kwargs):
        assert kwargs["base_branch"] == "master"
        assert len(kwargs["expected_commit"]) == 40

    async def base_is_ancestor(_self, issue_id, **kwargs):
        assert issue_id == "ISSUE-001"
        assert kwargs["base_branch"] == "master"
        assert kwargs["commit"] == "2" * 40

    async def publish(_self, issue_id, **kwargs):
        assert issue_id == "ISSUE-001"
        assert kwargs["commit"] == "2" * 40
        return "https://github.com/example/catalog/pull/1"

    async def verify(_self, repository_url, pull_request):
        assert repository_url.endswith("project")
        assert pull_request.endswith("/pull/1")

    monkeypatch.setattr(IssueDeliveryManager, "prepare_local_commit", prepare)
    monkeypatch.setattr(IssueDeliveryManager, "assert_base_unchanged", base_unchanged)
    monkeypatch.setattr(
        IssueDeliveryManager, "assert_base_is_ancestor", base_is_ancestor
    )
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

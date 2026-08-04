from __future__ import annotations

import asyncio
import subprocess
from datetime import timedelta

from sqlalchemy import update

from control_plane.models import WorkItem, utc_now

from conftest import feature_payload, work_item_payload


async def create_fixture(api, *work_items: dict[str, object]) -> None:
    response = await api.post("/api/features", json=feature_payload())
    assert response.status_code == 201, response.text
    for payload in work_items:
        response = await api.post("/api/work-items", json=payload)
        assert response.status_code == 201, response.text


async def test_dashboard_and_feature_list(api) -> None:
    dashboard = await api.get("/")
    assert dashboard.status_code == 200
    assert "Fshows Agent Control Plane" in dashboard.text
    assert "/ui/assets/app.js" in dashboard.text
    assert "手工录入 Issue" in dashboard.text

    stylesheet = await api.get("/ui/assets/styles.css")
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    javascript = await api.get("/ui/assets/app.js")
    assert javascript.status_code == 200
    assert "/api/intake/manual/issues/preview" in javascript.text
    assert "/api/repositories/resolve-head" in javascript.text

    for feature in (
        feature_payload(),
        {
            "id": "FEATURE-002",
            "title": "Second feature",
            "description": "Feature list fixture",
        },
    ):
        response = await api.post("/api/features", json=feature)
        assert response.status_code == 201, response.text

    features = await api.get("/api/features")
    assert features.status_code == 200, features.text
    assert {feature["id"] for feature in features.json()} == {
        "FEATURE-001",
        "FEATURE-002",
    }


async def test_resolve_local_repository_head(api, tmp_path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test Runner"],
        check=True,
    )
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "--quiet", "-m", "fixture"],
        check=True,
    )
    expected = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    response = await api.post(
        "/api/repositories/resolve-head", json={"path": str(repository)}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"path": str(repository.resolve()), "commit": expected}


async def test_resolve_repository_head_rejects_non_local_or_invalid_path(api, tmp_path) -> None:
    relative = await api.post(
        "/api/repositories/resolve-head", json={"path": "relative/repository"}
    )
    assert relative.status_code == 422
    assert relative.json()["error"]["code"] == "repository_resolution_failed"

    non_repository = tmp_path / "not-a-repository"
    non_repository.mkdir()
    invalid = await api.post(
        "/api/repositories/resolve-head", json={"path": str(non_repository)}
    )
    assert invalid.status_code == 422


async def test_manual_issue_preview_and_atomic_creation(api) -> None:
    payload = {
        "feature_id": "FEATURE-7001",
        "title": "查询用户详情",
        "description": "新增只读接口，根据用户 ID 查询用户详情。",
        "priority": 1,
        "repository": {
            "url": r"D:\fws-repo-cache\hengxi-cultural-tourism",
            "base_branch": "master",
            "head_branch": None,
            "commit": "a" * 40,
            "pull_request": None,
        },
        "acceptance_criteria": [
            "存在用户返回详情",
            "不存在用户返回明确的业务错误",
        ],
    }

    preview = await api.post("/api/intake/manual/issues/preview", json=payload)
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["template"] == "five_stage_backend_v1"
    assert plan["feature"]["id"] == "FEATURE-7001"
    assert [item["agent_role"] for item in plan["work_items"]] == [
        "solution_architect",
        "backend_builder",
        "code_reviewer",
        "test_designer",
        "test_executor",
    ]
    assert [item["id"] for item in plan["work_items"]] == [
        "WI-700101",
        "WI-700102",
        "WI-700103",
        "WI-700104",
        "WI-700105",
    ]
    assert plan["work_items"][0]["status"] == "ready"
    assert all(item["status"] == "draft" for item in plan["work_items"][1:])
    assert plan["work_items"][1]["dependencies"] == ["WI-700101"]
    assert (await api.get("/api/features")).json() == []

    created = await api.post("/api/intake/manual/issues", json=payload)
    assert created.status_code == 201, created.text
    result = created.json()
    assert result["feature"]["id"] == "FEATURE-7001"
    assert len(result["work_items"]) == 5
    candidates = (await api.get("/api/work-items/candidates")).json()
    assert [item["id"] for item in candidates] == ["WI-700101"]
    items = (await api.get("/api/work-items", params={"feature_id": "FEATURE-7001"})).json()
    assert len(items) == 5
    assert items[1]["blocked_by"] == ["WI-700101"]

    duplicate = await api.post("/api/intake/manual/issues", json=payload)
    assert duplicate.status_code == 409
    assert len((await api.get("/api/work-items")).json()) == 5


async def test_manual_issue_requires_immutable_commit(api) -> None:
    response = await api.post(
        "/api/intake/manual/issues/preview",
        json={
            "feature_id": "FEATURE-7002",
            "title": "Invalid input",
            "description": "The repository revision is not immutable.",
            "repository": {
                "url": "https://example.invalid/repository.git",
                "base_branch": "main",
                "commit": "main",
            },
            "acceptance_criteria": ["must not be accepted"],
        },
    )
    assert response.status_code == 422


async def test_manual_issue_creation_rolls_back_on_generated_id_conflict(api) -> None:
    existing_feature = {
        "id": "FEATURE-7999",
        "title": "Existing feature",
        "description": "Owns a colliding WorkItem ID.",
    }
    assert (await api.post("/api/features", json=existing_feature)).status_code == 201
    colliding_item = work_item_payload("WI-700101")
    colliding_item["feature_id"] = "FEATURE-7999"
    assert (await api.post("/api/work-items", json=colliding_item)).status_code == 201

    response = await api.post(
        "/api/intake/manual/issues",
        json={
            "feature_id": "FEATURE-7001",
            "title": "Must remain atomic",
            "description": "Generated IDs collide with an existing work item.",
            "repository": {
                "url": "https://example.invalid/repository.git",
                "base_branch": "main",
                "commit": "b" * 40,
            },
            "acceptance_criteria": ["No partial feature is persisted"],
        },
    )

    assert response.status_code == 409
    assert (await api.get("/api/features/FEATURE-7001")).status_code == 404
    assert len((await api.get("/api/work-items")).json()) == 1


async def test_dependencies_gate_candidates_and_status_events(api) -> None:
    await create_fixture(
        api,
        work_item_payload("WI-001", status="ready"),
        work_item_payload("WI-002", dependencies=["WI-001"]),
    )
    candidates = (await api.get("/api/work-items/candidates")).json()
    assert [item["id"] for item in candidates] == ["WI-001"]
    blocked = (await api.get("/api/work-items/WI-002")).json()
    assert blocked["dependencies"] == ["WI-001"]
    assert blocked["blocked_by"] == ["WI-001"]

    claim = await api.post(
        "/api/work-items/WI-001/claim",
        json={"workerId": "symphony-01", "expectedVersion": 1, "leaseSeconds": 60},
    )
    assert claim.status_code == 200, claim.text
    token = claim.json()["claim_token"]
    artifact = await api.post(
        "/api/work-items/WI-001/artifacts",
        json={
            "direction": "output",
            "path": "orchestration/handoffs/WI-001.yaml",
            "revision": "abc1234",
            "claim_token": token,
        },
    )
    assert artifact.status_code == 201, artifact.text
    completed = await api.post(
        "/api/work-items/WI-001/status",
        json={
            "to_status": "stage_review",
            "event": "agent_completed",
            "claim_token": token,
        },
    )
    assert completed.status_code == 200, completed.text
    approved = await api.post(
        "/api/work-items/WI-001/status",
        json={"to_status": "done", "event": "stage_approved", "actor_type": "human"},
    )
    assert approved.status_code == 200, approved.text
    readied = await api.post(
        "/api/work-items/WI-002/status",
        json={
            "to_status": "ready",
            "event": "work_item_readied",
            "actor_type": "control_plane",
        },
    )
    assert readied.status_code == 200, readied.text

    candidates = (await api.get("/api/work-items/candidates")).json()
    assert [item["id"] for item in candidates] == ["WI-002"]
    event_types = [
        event["event_type"]
        for event in (await api.get("/api/work-items/WI-001/events")).json()
    ]
    assert event_types == [
        "created",
        "claimed",
        "artifact_created",
        "agent_completed",
        "stage_approved",
    ]
    dependent_events = (await api.get("/api/work-items/WI-002/events")).json()
    assert "dependency_satisfied" in [event["event_type"] for event in dependent_events]


async def test_concurrent_claim_allows_exactly_one_winner(api) -> None:
    await create_fixture(api, work_item_payload("WI-001", status="ready"))

    async def claim(worker_id: str):
        return await api.post(
            "/api/work-items/WI-001/claim",
            json={"worker_id": worker_id, "expected_version": 1, "lease_seconds": 60},
        )

    responses = await asyncio.gather(claim("worker-a"), claim("worker-b"))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response for response in responses if response.status_code == 200)
    item = (await api.get("/api/work-items/WI-001")).json()
    assert item["status"] == "running"
    assert (
        item["claim"]["worker_id"] == winner.json()["work_item"]["claim"]["worker_id"]
    )
    events = (await api.get("/api/work-items/WI-001/events")).json()
    assert [event["event_type"] for event in events].count("claimed") == 1


async def test_feature_root_handoff_satisfies_completion_guard(api) -> None:
    await create_fixture(api, work_item_payload("WI-001", status="ready"))
    claim = await api.post(
        "/api/work-items/WI-001/claim",
        json={"workerId": "symphony-01", "expectedVersion": 1, "leaseSeconds": 60},
    )
    token = claim.json()["claim_token"]
    artifact = await api.post(
        "/api/work-items/WI-001/artifacts",
        json={
            "direction": "output",
            "path": (
                "docs/iterations/2026-08-user-detail-pilot/"
                "orchestration/handoffs/WI-001.yaml"
            ),
            "revision": "abc1234",
            "claim_token": token,
        },
    )
    assert artifact.status_code == 201, artifact.text

    completed = await api.post(
        "/api/work-items/WI-001/status",
        json={
            "to_status": "stage_review",
            "event": "agent_completed",
            "claim_token": token,
        },
    )
    assert completed.status_code == 200, completed.text


async def test_worker_release_uses_requested_retry_delay(api) -> None:
    await create_fixture(api, work_item_payload("WI-001", status="ready"))
    claimed = await api.post(
        "/api/work-items/WI-001/claim",
        json={
            "worker_id": "windows-runner",
            "expected_version": 1,
            "lease_seconds": 60,
        },
    )
    token = claimed.json()["claim_token"]
    released = await api.post(
        "/api/work-items/WI-001/release",
        json={
            "claim_token": token,
            "reason": "agent_attempt_failed",
            "retry_delay_seconds": 0,
            "thread_id": "thread-resumable",
        },
    )
    assert released.status_code == 200, released.text
    assert released.json()["status"] == "retry_queued"
    attempts = (await api.get("/api/work-items/WI-001/attempts")).json()
    assert attempts[0]["thread_id"] == "thread-resumable"
    tick = await api.post("/api/maintenance/tick")
    assert tick.status_code == 200, tick.text
    assert (await api.get("/api/work-items/WI-001")).json()["status"] == "ready"


async def test_stage_rework_claim_resumes_completed_codex_thread(api) -> None:
    await create_fixture(api, work_item_payload("WI-001", status="ready"))
    profile = {
        "name": "solution_architect",
        "version": 1,
        "config": {
            "profile_name": "solution_architect",
            "profile_version": 1,
        },
    }
    claimed = await api.post(
        "/api/work-items/WI-001/claim",
        json={
            "worker_id": "windows-runner",
            "expected_version": 1,
            "lease_seconds": 60,
            "profile": profile,
        },
    )
    token = claimed.json()["claim_token"]
    await api.post(
        "/api/work-items/WI-001/artifacts",
        json={
            "direction": "output",
            "path": "orchestration/handoffs/WI-001.yaml",
            "revision": "reviewed",
            "claim_token": token,
        },
    )
    delivered = await api.post(
        "/api/work-items/WI-001/status",
        json={
            "to_status": "stage_review",
            "event": "agent_completed",
            "claim_token": token,
            "payload": {"thread_id": "thread-rework"},
        },
    )
    assert delivered.status_code == 200, delivered.text
    rejected = await api.post(
        "/api/work-items/WI-001/status",
        json={
            "to_status": "rework",
            "event": "stage_rejected",
            "actor_type": "human",
            "payload": {"rework_reason": "fix handoff metadata"},
        },
    )
    queued = await api.post(
        "/api/work-items/WI-001/status",
        json={
            "to_status": "ready",
            "event": "rework_queued",
            "actor_type": "control_plane",
            "payload": {"rework_scope": "handoff only"},
        },
    )
    reclaimed = await api.post(
        "/api/work-items/WI-001/claim",
        json={
            "worker_id": "windows-runner",
            "expected_version": queued.json()["version"],
            "lease_seconds": 60,
            "profile": profile,
        },
    )
    assert rejected.status_code == 200, rejected.text
    assert reclaimed.status_code == 200, reclaimed.text
    assert reclaimed.json()["resume_thread_id"] == "thread-rework"
    assert reclaimed.json()["continuation_turn_count"] == 0


async def test_expired_lease_is_retried_and_old_token_is_revoked(api) -> None:
    await create_fixture(api, work_item_payload("WI-001", status="ready"))
    response = await api.post(
        "/api/work-items/WI-001/claim",
        json={"worker_id": "worker-a", "expected_version": 1, "lease_seconds": 60},
    )
    old_token = response.json()["claim_token"]

    database = api.app.state.database  # type: ignore[attr-defined]
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(WorkItem)
            .where(WorkItem.id == "WI-001")
            .values(claim_expires_at=utc_now() - timedelta(seconds=1))
        )

    tick = await api.post("/api/maintenance/tick")
    assert tick.status_code == 200, tick.text
    assert tick.json() == {"expired": 1, "readied": 1}
    item = (await api.get("/api/work-items/WI-001")).json()
    assert item["status"] == "ready"
    assert item["claim"] == {"worker_id": None, "expires_at": None}
    rejected = await api.post(
        "/api/work-items/WI-001/heartbeat",
        json={"claim_token": old_token, "lease_seconds": 60},
    )
    assert rejected.status_code == 409
    reclaimed = await api.post(
        "/api/work-items/WI-001/claim",
        json={
            "worker_id": "worker-b",
            "expected_version": item["version"],
            "lease_seconds": 60,
        },
    )
    assert reclaimed.status_code == 200, reclaimed.text
    assert reclaimed.json()["claim_token"] != old_token


async def test_human_decision_releases_claim_and_resumes_ready(api) -> None:
    await create_fixture(api, work_item_payload("WI-001", status="ready"))
    claim = await api.post(
        "/api/work-items/WI-001/claim",
        json={"worker_id": "worker-a", "expected_version": 1, "lease_seconds": 60},
    )
    token = claim.json()["claim_token"]
    requested = await api.post(
        "/api/work-items/WI-001/decisions",
        json={
            "action": "request",
            "question": "Choose cursor format",
            "options": ["opaque", "plain"],
            "actor_id": "agent-1",
            "claim_token": token,
        },
    )
    assert requested.status_code == 200, requested.text
    decision = requested.json()
    waiting = (await api.get("/api/work-items/WI-001")).json()
    assert waiting["status"] == "needs_human"
    assert waiting["claim"]["worker_id"] is None

    bypass = await api.post(
        "/api/work-items/WI-001/status",
        json={
            "to_status": "ready",
            "event": "human_decision_resolved",
            "actor_type": "human",
        },
    )
    assert bypass.status_code == 409

    resolved = await api.post(
        "/api/work-items/WI-001/decisions",
        json={
            "action": "resolve",
            "decision_id": decision["id"],
            "response": "opaque",
            "actor_id": "human-1",
        },
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"
    resumed = (await api.get("/api/work-items/WI-001")).json()
    assert resumed["status"] == "ready"
    event_types = [
        event["event_type"]
        for event in (await api.get("/api/work-items/WI-001/events")).json()
    ]
    assert event_types[-2:] == ["human_input_requested", "human_decision_resolved"]


async def test_stage_review_can_request_human_and_dependency_cycles_are_rejected(
    api,
) -> None:
    await create_fixture(
        api,
        work_item_payload("WI-001"),
        work_item_payload("WI-002", dependencies=["WI-001"]),
        work_item_payload("WI-003", status="ready"),
    )
    cycle = await api.patch(
        "/api/work-items/WI-001",
        json={"expected_version": 1, "dependencies": ["WI-002"]},
    )
    assert cycle.status_code == 409

    claim = await api.post(
        "/api/work-items/WI-003/claim",
        json={"worker_id": "reviewer", "expected_version": 1, "lease_seconds": 60},
    )
    token = claim.json()["claim_token"]
    rejected_event = await api.post(
        "/api/work-items/WI-003/events",
        json={"event_type": "agent_started", "actor_id": "foreign-agent"},
    )
    assert rejected_event.status_code == 409
    await api.post(
        "/api/work-items/WI-003/artifacts",
        json={
            "direction": "output",
            "path": "orchestration/handoffs/WI-003.yaml",
            "revision": "review-revision",
            "claim_token": token,
        },
    )
    delivered = await api.post(
        "/api/work-items/WI-003/status",
        json={
            "to_status": "stage_review",
            "event": "agent_completed",
            "claim_token": token,
        },
    )
    assert delivered.status_code == 200, delivered.text
    decision = await api.post(
        "/api/work-items/WI-003/decisions",
        json={
            "action": "request",
            "question": "Accept the stage result?",
            "options": ["approve", "reject"],
            "actor_id": "review-policy",
        },
    )
    assert decision.status_code == 200, decision.text
    assert (await api.get("/api/work-items/WI-003")).json()["status"] == "needs_human"


async def test_bearer_auth_and_multi_state_queries(authenticated_api) -> None:
    unauthorized = await authenticated_api.get("/api/work-items")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "Bearer"
    assert (await authenticated_api.get("/health")).json()["auth_enabled"] is True

    headers = {"Authorization": "Bearer integration-secret"}
    feature = await authenticated_api.post(
        "/api/features", json=feature_payload(), headers=headers
    )
    assert feature.status_code == 201, feature.text
    for payload in (
        work_item_payload("WI-001", status="ready"),
        work_item_payload("WI-002"),
    ):
        response = await authenticated_api.post(
            "/api/work-items", json=payload, headers=headers
        )
        assert response.status_code == 201, response.text

    response = await authenticated_api.get(
        "/api/work-items",
        params=[("status", "ready"), ("status", "draft")],
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert {item["status"] for item in response.json()} == {"ready", "draft"}

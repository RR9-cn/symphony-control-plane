from __future__ import annotations

from scripts.validate_symphony_integration import EXPECTED_TOOLS, validate_sources

from conftest import feature_payload, work_item_payload


def test_symphony_overlay_contract() -> None:
    validate_sources()
    assert len(EXPECTED_TOOLS) == 6


async def test_symphony_host_claim_heartbeat_and_completion(authenticated_api) -> None:
    headers = {"Authorization": "Bearer integration-secret"}
    assert (
        await authenticated_api.post(
            "/api/features", json=feature_payload(), headers=headers
        )
    ).status_code == 201
    created = await authenticated_api.post(
        "/api/work-items",
        json=work_item_payload("WI-001", status="ready"),
        headers=headers,
    )
    assert created.status_code == 201, created.text

    candidates = await authenticated_api.get(
        "/api/work-items/candidates", headers=headers
    )
    assert [item["id"] for item in candidates.json()] == ["WI-001"]

    owner = await authenticated_api.post(
        "/api/work-items/WI-001/claim",
        json={"workerId": "symphony-01", "expectedVersion": 1, "leaseSeconds": 60},
        headers=headers,
    )
    assert owner.status_code == 200, owner.text
    token = owner.json()["claim_token"]

    foreign = await authenticated_api.post(
        "/api/work-items/WI-001/claim",
        json={"workerId": "symphony-02", "expectedVersion": 1, "leaseSeconds": 60},
        headers=headers,
    )
    assert foreign.status_code == 409
    assert (await authenticated_api.get(
        "/api/work-items/candidates", headers=headers
    )).json() == []

    heartbeat = await authenticated_api.post(
        "/api/work-items/WI-001/heartbeat",
        json={"claimToken": token, "leaseSeconds": 60},
        headers=headers,
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["claim"]["worker_id"] == "symphony-01"

    handoff = await authenticated_api.post(
        "/api/work-items/WI-001/artifacts",
        json={
            "direction": "output",
            "path": "orchestration/handoffs/WI-001.yaml",
            "revision": "symphony-revision",
            "claim_token": token,
        },
        headers=headers,
    )
    assert handoff.status_code == 201, handoff.text
    completed = await authenticated_api.post(
        "/api/work-items/WI-001/status",
        json={
            "to_status": "stage_review",
            "event": "agent_completed",
            "actor_type": "agent",
            "actor_id": "codex",
            "claim_token": token,
        },
        headers=headers,
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "stage_review"
    assert completed.json()["claim"]["worker_id"] is None

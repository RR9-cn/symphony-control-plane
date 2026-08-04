#!/usr/bin/env python3
"""Run one human-in-the-loop scheduling lifecycle against a running API."""

from __future__ import annotations

import argparse
import os
import secrets

import httpx


def require(response: httpx.Response) -> dict | list:
    if response.is_error:
        raise RuntimeError(f"{response.request.method} {response.request.url}: {response.text}")
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--token", default=os.environ.get("CONTROL_PLANE_TOKEN"))
    args = parser.parse_args()
    suffix = secrets.randbelow(9_000_000) + 1_000_000
    feature_id = f"FEATURE-{suffix}"
    item_id = f"WI-{suffix}"

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=10) as client:
        require(
            client.post(
                "/api/features",
                json={
                    "id": feature_id,
                    "title": "API scheduling simulation",
                    "description": "Created by scripts/simulate_api.py",
                },
            )
        )
        item = require(
            client.post(
                "/api/work-items",
                json={
                    "id": item_id,
                    "feature_id": feature_id,
                    "parent_id": None,
                    "title": "Simulated technical analysis",
                    "description": "Exercise claim, heartbeat, human input and completion",
                    "stage": "tech_analysis",
                    "agent_role": "solution_architect",
                    "status": "ready",
                    "priority": 1,
                    "repository": {
                        "url": "git@example.local:simulation.git",
                        "base_branch": "main",
                        "head_branch": None,
                        "commit": None,
                        "pull_request": None,
                    },
                    "dependencies": [],
                    "input_artifacts": [],
                    "output_artifacts": [],
                    "acceptance_criteria": ["Human decision and completion are audited"],
                },
            )
        )
        claim = require(
            client.post(
                f"/api/work-items/{item_id}/claim",
                json={
                    "workerId": "simulation-worker",
                    "expectedVersion": item["version"],
                    "leaseSeconds": 120,
                },
            )
        )
        token = claim["claim_token"]
        require(
            client.post(
                f"/api/work-items/{item_id}/heartbeat",
                json={"claimToken": token, "leaseSeconds": 120},
            )
        )
        require(
            client.post(
                f"/api/work-items/{item_id}/events",
                json={
                    "event_type": "agent_started",
                    "actor_id": "simulation-agent",
                    "claimToken": token,
                },
            )
        )
        decision = require(
            client.post(
                f"/api/work-items/{item_id}/decisions",
                json={
                    "action": "request",
                    "question": "Approve the simulated architecture choice?",
                    "options": ["approve", "reject"],
                    "actor_id": "simulation-agent",
                    "claimToken": token,
                },
            )
        )
        require(
            client.post(
                f"/api/work-items/{item_id}/decisions",
                json={
                    "action": "resolve",
                    "decision_id": decision["id"],
                    "response": "approve",
                    "actor_id": "simulation-human",
                },
            )
        )
        item = require(client.get(f"/api/work-items/{item_id}"))
        claim = require(
            client.post(
                f"/api/work-items/{item_id}/claim",
                json={
                    "worker_id": "simulation-worker",
                    "expected_version": item["version"],
                    "lease_seconds": 120,
                },
            )
        )
        token = claim["claim_token"]
        require(
            client.post(
                f"/api/work-items/{item_id}/artifacts",
                json={
                    "direction": "output",
                    "path": f"orchestration/handoffs/{item_id}.yaml",
                    "revision": "simulation-revision",
                    "claim_token": token,
                },
            )
        )
        require(
            client.post(
                f"/api/work-items/{item_id}/status",
                json={
                    "to_status": "stage_review",
                    "event": "agent_completed",
                    "claim_token": token,
                    "actor_id": "simulation-agent",
                },
            )
        )
        final = require(
            client.post(
                f"/api/work-items/{item_id}/status",
                json={
                    "to_status": "done",
                    "event": "stage_approved",
                    "actor_type": "human",
                    "actor_id": "simulation-human",
                },
            )
        )
        events = require(client.get(f"/api/work-items/{item_id}/events"))

    print(f"simulation passed: {feature_id} / {item_id}")
    print(f"final status: {final['status']}, version: {final['version']}")
    print("events: " + " -> ".join(event["event_type"] for event in events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Exercise one Issue, one Agent, multi-Turn, and final-review lifecycle."""

from __future__ import annotations

import argparse
import os
import secrets

import httpx


def require(response: httpx.Response):
    if response.is_error:
        raise RuntimeError(f"{response.request.method} {response.request.url}: {response.text}")
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--token", default=os.environ.get("CONTROL_PLANE_TOKEN"))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--base-branch", default="main")
    args = parser.parse_args()
    issue_id = f"ISSUE-{secrets.randbelow(900000) + 100000}"
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=10) as client:
        issue = require(client.post("/api/issues", json={
            "id": issue_id,
            "title": "Symphony API simulation",
            "description": "Exercise a complete generic coding-agent lifecycle.",
            "priority": 1,
            "repository": {"url": args.repository, "base_branch": args.base_branch, "commit": args.commit},
            "acceptance_criteria": ["Multi-Turn context and completion are audited"],
        }))
        claim = require(client.post(f"/api/issues/{issue_id}/claim", json={
            "workerId": "simulation-worker",
            "expectedVersion": issue["version"],
            "leaseSeconds": 120,
            "agent": {"config": {"kind": "coding_agent", "max_turns": 20}},
        }))
        token = claim["claim_token"]
        require(client.post(f"/api/issues/{issue_id}/attempt-context", json={
            "claimToken": token, "threadId": "simulation-thread", "turnId": "turn-2", "turnCount": 2,
        }))
        require(client.post(f"/api/issues/{issue_id}/events", json={
            "event_type": "validation_finished", "payload": {"passed": True}, "claimToken": token,
        }))
        final = require(client.post(f"/api/issues/{issue_id}/status", json={
            "toStatus": "reviewing", "event": "agent_completed", "actorType": "agent",
            "actorId": "simulation-agent", "claimToken": token,
        }))
        events = require(client.get(f"/api/issues/{issue_id}/events"))
        attempts = require(client.get(f"/api/issues/{issue_id}/attempts"))
    print(f"simulation passed: {issue_id}")
    print(f"final status: {final['status']}, turns: {attempts[0]['turn_count']}")
    print("events: " + " -> ".join(event["event"] for event in events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

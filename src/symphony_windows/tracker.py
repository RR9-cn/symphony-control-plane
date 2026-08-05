from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx

from symphony_windows.workflow import TrackerConfig


class TrackerError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, error_code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class ClaimConflict(TrackerError):
    pass


@dataclass
class ClaimLease:
    issue: dict[str, Any]
    token: str
    attempt: dict[str, Any]
    resume_thread_id: str | None = None
    resume_decisions: list[dict[str, Any]] = field(default_factory=list)
    continuation_turn_count: int = 0
    active: bool = True

    @property
    def id(self) -> str:
        return str(self.issue["id"])


@dataclass(frozen=True)
class ToolExecution:
    response: dict[str, Any]
    stop_agent: bool = False


class ControlPlaneTracker:
    def __init__(self, config: TrackerConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.endpoint, headers={"Authorization": f"Bearer {config.token}"},
            timeout=config.request_timeout_seconds, transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def candidates(self, limit: int = 100) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/api/issues/candidates", params={"limit": limit})
        if not isinstance(payload, list):
            raise TrackerError("candidate response must be a list")
        return [row for row in payload if isinstance(row, dict)]

    async def get_issue(self, issue_id: str) -> dict[str, Any]:
        payload = await self._request("GET", self._issue_path(issue_id))
        if not isinstance(payload, dict):
            raise TrackerError("issue response must be an object")
        return payload

    async def claim(self, issue: dict[str, Any], agent_snapshot: dict[str, Any]) -> ClaimLease:
        issue_id = str(issue.get("id", ""))
        version = issue.get("version")
        if not issue_id or not isinstance(version, int):
            raise TrackerError("candidate is missing id or version")
        try:
            payload = await self._request(
                "POST", self._issue_path(issue_id) + "/claim",
                json={
                    "workerId": self.config.worker_id, "expectedVersion": version,
                    "leaseSeconds": self.config.lease_seconds, "agent": {"config": agent_snapshot},
                },
            )
        except TrackerError as error:
            if error.status_code == 409:
                raise ClaimConflict(str(error), status_code=409, error_code=error.error_code) from error
            raise
        claimed = payload.get("issue") if isinstance(payload, dict) else None
        token = payload.get("claim_token") if isinstance(payload, dict) else None
        attempt = payload.get("attempt") if isinstance(payload, dict) else None
        if not isinstance(claimed, dict) or not isinstance(token, str) or not isinstance(attempt, dict):
            raise TrackerError("claim response is invalid")
        return ClaimLease(
            issue=claimed, token=token, attempt=attempt,
            resume_thread_id=payload.get("resume_thread_id"),
            resume_decisions=payload.get("resume_decisions", []),
            continuation_turn_count=payload.get("continuation_turn_count", 0),
        )

    async def heartbeat(self, lease: ClaimLease) -> dict[str, Any]:
        self._require_active(lease)
        try:
            payload = await self._request(
                "POST", self._issue_path(lease.id) + "/heartbeat",
                json={"claimToken": lease.token, "leaseSeconds": self.config.lease_seconds},
            )
        except TrackerError as error:
            if error.status_code == 409:
                lease.active = False
                raise ClaimConflict(str(error), status_code=409) from error
            raise
        lease.issue = payload
        return payload

    async def release(self, lease: ClaimLease, reason: str, *, retry_delay_seconds: int, thread_id: str | None = None) -> dict[str, Any]:
        self._require_active(lease)
        body: dict[str, Any] = {"claimToken": lease.token, "reason": reason, "retryDelaySeconds": retry_delay_seconds}
        if thread_id:
            body["threadId"] = thread_id
        payload = await self._request("POST", self._issue_path(lease.id) + "/release", json=body)
        lease.active = False
        lease.issue = payload
        return payload

    async def maintenance_tick(self) -> dict[str, Any]:
        return await self._request("POST", "/api/maintenance/tick", json={})

    async def register_worker(self, *, capacity: int) -> dict[str, Any]:
        return await self._request(
            "POST", "/api/workers/register",
            json={
                "workerId": self.config.worker_id, "hostname": socket.gethostname(),
                "processId": os.getpid(), "version": "0.2.0", "capacity": capacity,
            },
        )

    async def heartbeat_worker(self, *, active_issues: list[str]) -> dict[str, Any]:
        return await self._request(
            "POST", f"/api/workers/{quote(self.config.worker_id, safe='')}/heartbeat",
            json={"state": "running" if active_issues else "idle", "activeIssues": sorted(active_issues)},
        )

    async def worker_stopped(self) -> dict[str, Any]:
        return await self._request("POST", f"/api/workers/{quote(self.config.worker_id, safe='')}/stopped", json={})

    async def update_attempt_context(self, lease: ClaimLease, *, thread_id: str, turn_id: str | None = None, turn_count: int | None = None) -> dict[str, Any]:
        self._require_active(lease)
        body: dict[str, Any] = {"claimToken": lease.token, "threadId": thread_id}
        if turn_id is not None:
            body["turnId"] = turn_id
        if turn_count is not None:
            body["turnCount"] = turn_count
        payload = await self._request("POST", self._issue_path(lease.id) + "/attempt-context", json=body)
        lease.attempt = payload
        return payload

    async def add_attempt_event(self, lease: ClaimLease, event: dict[str, Any]) -> dict[str, Any]:
        self._require_active(lease)
        attempt_id = lease.attempt.get("id")
        return await self._request(
            "POST", self._issue_path(lease.id) + f"/attempts/{quote(str(attempt_id), safe='')}/events",
            json={**event, "claimToken": lease.token},
        )

    def tool_specs(self) -> list[dict[str, Any]]:
        empty = {"type": "object", "additionalProperties": False, "properties": {}}
        return [
            _tool("issue_get", "Read the current Issue.", empty),
            _tool("issue_add_event", "Append an audit event to the current Issue.", _schema(["event_type"], {"event_type": {"type": "string", "minLength": 1}, "payload": {"type": "object", "additionalProperties": True}})),
            _tool("issue_add_artifact", "Register an output artifact for the current Issue.", _schema(["path", "revision"], {"path": {"type": "string", "minLength": 1}, "revision": {"type": "string", "minLength": 1}, "media_type": {"type": ["string", "null"]}, "sha256": {"type": ["string", "null"], "pattern": "^[a-f0-9]{64}$"}})),
            _tool("issue_request_human", "Request a human decision and stop the current run.", _schema(["question"], {"question": {"type": "string", "minLength": 1}, "options": {"type": "array", "items": {"type": "string", "minLength": 1}}})),
            _tool("issue_complete", "Submit the fully implemented and tested Issue for final human review.", empty),
            _tool("issue_block", "Record a blocker and stop the current run.", _schema(["code", "message"], {"code": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"}, "message": {"type": "string", "minLength": 1}})),
        ]

    async def execute_tool(self, lease: ClaimLease, name: str | None, arguments: Any, *, thread_id: str | None = None) -> ToolExecution:
        try:
            body, stop = await self._execute_tool(lease, name, arguments, thread_id=thread_id)
            return ToolExecution(_tool_response(True, body), stop)
        except (TrackerError, ValueError) as error:
            return ToolExecution(_tool_response(False, {"error": str(error)}))

    async def request_runtime_input(self, lease: ClaimLease, question: str) -> None:
        self._require_active(lease)
        await self._request(
            "POST", self._issue_path(lease.id) + "/decisions",
            json={"action": "request", "question": question, "options": [], "actor_id": self.config.worker_id, "claimToken": lease.token},
        )
        lease.active = False

    async def _execute_tool(self, lease: ClaimLease, name: str | None, arguments: Any, *, thread_id: str | None) -> tuple[Any, bool]:
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        if name == "issue_get" and not arguments:
            return await self.get_issue(lease.id), False
        self._require_active(lease)
        if name == "issue_add_event":
            payload = arguments.get("payload", {})
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            return await self._request("POST", self._issue_path(lease.id) + "/events", json={"event_type": _required(arguments, "event_type"), "payload": payload, "claimToken": lease.token}), False
        if name == "issue_add_artifact":
            path = _required(arguments, "path")
            if not _valid_path(path):
                raise ValueError("artifact path must be repository-relative")
            body = {key: value for key, value in arguments.items() if key in {"path", "revision", "media_type", "sha256"}}
            _required(body, "revision")
            body["claimToken"] = lease.token
            return await self._request("POST", self._issue_path(lease.id) + "/artifacts", json=body), False
        if name == "issue_request_human":
            options = arguments.get("options", [])
            if not isinstance(options, list) or not all(isinstance(value, str) and value.strip() for value in options):
                raise ValueError("options must be non-empty strings")
            payload = await self._request(
                "POST", self._issue_path(lease.id) + "/decisions",
                json={"action": "request", "question": _required(arguments, "question"), "options": options, "actor_id": "codex", "claimToken": lease.token, "threadId": thread_id},
            )
            lease.active = False
            return payload, True
        if name == "issue_complete" and not arguments:
            return await self._transition(lease, "reviewing", "agent_completed", _thread({}, thread_id)), True
        if name == "issue_block":
            code = _required(arguments, "code")
            if not re.fullmatch(r"[a-z][a-z0-9_]*", code):
                raise ValueError("blocker code must be snake_case")
            return await self._transition(lease, "blocked", "agent_blocked", _thread({"blocker": {"code": code, "message": _required(arguments, "message")}}, thread_id)), True
        raise ValueError(f"unsupported tool {name!r}")

    async def _transition(self, lease: ClaimLease, status: str, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = await self._request(
            "POST", self._issue_path(lease.id) + "/status",
            json={"to_status": status, "event": event, "actor_type": "worker", "actor_id": self.config.worker_id, "claimToken": lease.token, "payload": payload},
        )
        lease.issue = body
        lease.active = False
        return body

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise TrackerError(f"Control Plane request failed: {error}") from error
        if response.is_success:
            return response.json()
        try:
            payload = response.json().get("error", {})
            message = payload.get("message", response.text)
            code = payload.get("code")
        except (ValueError, AttributeError):
            message, code = response.text, None
        raise TrackerError(f"Control Plane returned {response.status_code}: {message}", status_code=response.status_code, error_code=code)

    @staticmethod
    def _issue_path(issue_id: str) -> str:
        return f"/api/issues/{quote(issue_id, safe='')}"

    @staticmethod
    def _require_active(lease: ClaimLease) -> None:
        if not lease.active:
            raise TrackerError("the current Issue claim is no longer active")


def _tool(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "description": description, "inputSchema": schema}


def _schema(required: list[str], properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": required, "properties": properties}


def _required(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _thread(payload: dict[str, Any], thread_id: str | None) -> dict[str, Any]:
    return payload if thread_id is None else {**payload, "thread_id": thread_id}


def _valid_path(value: str) -> bool:
    if "\\" in value or "\x00" in value or value.startswith("/"):
        return False
    path = PurePosixPath(value)
    return bool(path.parts) and all(part not in {"", ".", ".."} for part in path.parts)


def _tool_response(success: bool, payload: Any) -> dict[str, Any]:
    output = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return {"success": success, "output": output, "contentItems": [{"type": "inputText", "text": output}]}

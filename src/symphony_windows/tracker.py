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
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class ClaimConflict(TrackerError):
    """A candidate changed or was claimed by another worker."""


@dataclass
class ClaimLease:
    item: dict[str, Any]
    token: str
    attempt: dict[str, Any] | None = None
    resume_thread_id: str | None = None
    resume_decisions: list[dict[str, Any]] = field(default_factory=list)
    continuation_turn_count: int = 0
    active: bool = True

    @property
    def id(self) -> str:
        return str(self.item["id"])


@dataclass(frozen=True)
class ToolExecution:
    response: dict[str, Any]
    stop_agent: bool = False


class ControlPlaneTracker:
    def __init__(
        self,
        config: TrackerConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.endpoint,
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=config.request_timeout_seconds,
            transport=transport,
        )

    async def __aenter__(self) -> "ControlPlaneTracker":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def candidates(self, limit: int = 100) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET", "/api/work-items/candidates", params={"limit": limit}
        )
        if not isinstance(payload, list):
            raise TrackerError("candidate response must be a list")
        return [item for item in payload if isinstance(item, dict)]

    async def get_work_item(self, item_id: str) -> dict[str, Any]:
        payload = await self._request("GET", self._item_path(item_id))
        if not isinstance(payload, dict):
            raise TrackerError("work item response must be an object")
        return payload

    async def claim(
        self,
        item: dict[str, Any],
        profile: dict[str, Any] | None = None,
    ) -> ClaimLease:
        item_id = str(item.get("id", ""))
        version = item.get("version")
        if not item_id or not isinstance(version, int):
            raise TrackerError("candidate is missing id or version")
        request_body: dict[str, Any] = {
            "workerId": self.config.worker_id,
            "expectedVersion": version,
            "leaseSeconds": self.config.lease_seconds,
        }
        if profile is not None:
            request_body["profile"] = profile
        try:
            payload = await self._request(
                "POST",
                self._item_path(item_id) + "/claim",
                json=request_body,
            )
        except TrackerError as error:
            if (
                error.status_code == 409
                and error.error_code != "agent_profile_conflict"
            ):
                raise ClaimConflict(
                    str(error), status_code=409, error_code=error.error_code
                ) from error
            raise
        if not isinstance(payload, dict):
            raise TrackerError("claim response must be an object")
        claimed = payload.get("work_item")
        token = payload.get("claim_token")
        attempt = payload.get("attempt")
        resume_thread_id = payload.get("resume_thread_id")
        resume_decisions = payload.get("resume_decisions", [])
        continuation_turn_count = payload.get("continuation_turn_count", 0)
        if (
            not isinstance(claimed, dict)
            or not isinstance(token, str)
            or not token
            or not isinstance(attempt, dict)
            or (resume_thread_id is not None and not isinstance(resume_thread_id, str))
            or not isinstance(resume_decisions, list)
            or not all(isinstance(decision, dict) for decision in resume_decisions)
            or not isinstance(continuation_turn_count, int)
            or continuation_turn_count < 0
        ):
            raise TrackerError(
                "claim response is missing work_item, claim_token, or attempt"
            )
        return ClaimLease(
            item=claimed,
            token=token,
            attempt=attempt,
            resume_thread_id=resume_thread_id,
            resume_decisions=resume_decisions,
            continuation_turn_count=continuation_turn_count,
        )

    async def heartbeat(self, lease: ClaimLease) -> dict[str, Any]:
        self._require_active(lease)
        try:
            item = await self._request(
                "POST",
                self._item_path(lease.id) + "/heartbeat",
                json={
                    "claimToken": lease.token,
                    "leaseSeconds": self.config.lease_seconds,
                },
            )
        except TrackerError as error:
            if error.status_code == 409:
                lease.active = False
                raise ClaimConflict(str(error), status_code=409) from error
            raise
        if not isinstance(item, dict):
            raise TrackerError("heartbeat response must be an object")
        lease.item = item
        return item

    async def release(
        self,
        lease: ClaimLease,
        reason: str,
        *,
        retry_delay_seconds: int,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_active(lease)
        body: dict[str, Any] = {
            "claimToken": lease.token,
            "reason": reason,
            "retryDelaySeconds": retry_delay_seconds,
        }
        if thread_id is not None:
            body["threadId"] = thread_id
        item = await self._request(
            "POST",
            self._item_path(lease.id) + "/release",
            json=body,
        )
        lease.active = False
        if not isinstance(item, dict):
            raise TrackerError("release response must be an object")
        lease.item = item
        return item

    async def maintenance_tick(self) -> dict[str, Any]:
        payload = await self._request("POST", "/api/maintenance/tick", json={})
        if not isinstance(payload, dict):
            raise TrackerError("maintenance response must be an object")
        return payload

    async def register_worker(
        self, *, capacity: int, profiles: list[str]
    ) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            "/api/workers/register",
            json={
                "workerId": self.config.worker_id,
                "hostname": socket.gethostname(),
                "processId": os.getpid(),
                "version": "0.1.0",
                "capacity": capacity,
                "profiles": profiles,
            },
        )
        if not isinstance(payload, dict):
            raise TrackerError("worker registration response must be an object")
        return payload

    async def heartbeat_worker(
        self, *, active_profiles: dict[str, str]
    ) -> dict[str, Any]:
        active_items = sorted(active_profiles)
        payload = await self._request(
            "POST",
            f"/api/workers/{quote(self.config.worker_id, safe='')}/heartbeat",
            json={
                "state": "running" if active_items else "idle",
                "activeWorkItems": active_items,
                "activeProfiles": active_profiles,
            },
        )
        if not isinstance(payload, dict):
            raise TrackerError("worker heartbeat response must be an object")
        return payload

    async def worker_stopped(self) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            f"/api/workers/{quote(self.config.worker_id, safe='')}/stopped",
            json={},
        )
        if not isinstance(payload, dict):
            raise TrackerError("worker stopped response must be an object")
        return payload

    async def update_attempt_context(
        self,
        lease: ClaimLease,
        *,
        thread_id: str,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_active(lease)
        body: dict[str, Any] = {
            "claimToken": lease.token,
            "threadId": thread_id,
        }
        if turn_id is not None:
            body["turnId"] = turn_id
        payload = await self._request(
            "POST",
            self._item_path(lease.id) + "/attempt-context",
            json=body,
        )
        if not isinstance(payload, dict):
            raise TrackerError("attempt context response must be an object")
        lease.attempt = payload
        return payload

    async def add_attempt_event(
        self, lease: ClaimLease, event: dict[str, Any]
    ) -> dict[str, Any]:
        self._require_active(lease)
        attempt_id = lease.attempt.get("id") if lease.attempt is not None else None
        if not isinstance(attempt_id, str) or not attempt_id:
            raise TrackerError("claim is missing an agent attempt id")
        payload = await self._request(
            "POST",
            self._item_path(lease.id)
            + f"/attempts/{quote(attempt_id, safe='')}/events",
            json={**event, "claimToken": lease.token},
        )
        if not isinstance(payload, dict):
            raise TrackerError("attempt event response must be an object")
        return payload

    def tool_specs(self) -> list[dict[str, Any]]:
        empty = {"type": "object", "additionalProperties": False, "properties": {}}
        return [
            _tool("work_item_get", "Read the current Control Plane WorkItem.", empty),
            _tool(
                "work_item_add_event",
                "Append an audit event to the current WorkItem.",
                _object_schema(
                    ["event_type"],
                    {
                        "event_type": {"type": "string", "minLength": 1},
                        "payload": {"type": "object", "additionalProperties": True},
                    },
                ),
            ),
            _tool(
                "work_item_add_artifact",
                "Register an input or output Artifact for the current WorkItem.",
                _object_schema(
                    ["direction", "path", "revision"],
                    {
                        "direction": {"type": "string", "enum": ["input", "output"]},
                        "path": {"type": "string", "minLength": 1},
                        "revision": {"type": "string", "minLength": 1},
                        "media_type": {"type": ["string", "null"]},
                        "sha256": {
                            "type": ["string", "null"],
                            "pattern": "^[a-f0-9]{64}$",
                        },
                    },
                ),
            ),
            _tool(
                "work_item_request_human",
                "Request a human decision for the current WorkItem.",
                _object_schema(
                    ["question"],
                    {
                        "question": {"type": "string", "minLength": 1},
                        "options": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                    },
                ),
            ),
            _tool(
                "work_item_complete",
                "Submit the current WorkItem to StageReview after registering its Handoff.",
                empty,
            ),
            _tool(
                "work_item_block",
                "Record a blocker and release the current WorkItem claim.",
                _object_schema(
                    ["code", "message"],
                    {
                        "code": {
                            "type": "string",
                            "pattern": "^[a-z][a-z0-9_]*$",
                        },
                        "message": {"type": "string", "minLength": 1},
                    },
                ),
            ),
        ]

    async def execute_tool(
        self,
        lease: ClaimLease,
        name: str | None,
        arguments: Any,
        *,
        thread_id: str | None = None,
    ) -> ToolExecution:
        try:
            body, stop = await self._execute_tool(
                lease,
                name,
                arguments,
                thread_id=thread_id,
            )
            return ToolExecution(_tool_response(True, body), stop_agent=stop)
        except (TrackerError, ValueError) as error:
            return ToolExecution(_tool_response(False, {"error": str(error)}))

    async def request_runtime_input(
        self,
        lease: ClaimLease,
        question: str,
    ) -> None:
        self._require_active(lease)
        await self._request(
            "POST",
            self._item_path(lease.id) + "/decisions",
            json={
                "action": "request",
                "question": question,
                "options": [],
                "actor_id": self.config.worker_id,
                "claimToken": lease.token,
            },
        )
        lease.active = False

    async def _execute_tool(
        self,
        lease: ClaimLease,
        name: str | None,
        arguments: Any,
        *,
        thread_id: str | None,
    ) -> tuple[Any, bool]:
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        if name == "work_item_get" and not arguments:
            return await self.get_work_item(lease.id), False
        self._require_active(lease)
        if name == "work_item_add_event":
            event_type = _required_string(arguments, "event_type")
            payload = arguments.get("payload", {})
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            return (
                await self._request(
                    "POST",
                    self._item_path(lease.id) + "/events",
                    json={
                        "event_type": event_type,
                        "actor_type": "agent",
                        "actor_id": "codex",
                        "payload": payload,
                        "claimToken": lease.token,
                    },
                ),
                False,
            )
        if name == "work_item_add_artifact":
            path = _required_string(arguments, "path")
            if not _valid_artifact_path(path):
                raise ValueError(
                    "artifact path must be a safe repository-relative path"
                )
            direction = arguments.get("direction")
            if direction not in {"input", "output"}:
                raise ValueError("artifact direction must be input or output")
            body = {
                key: value
                for key, value in arguments.items()
                if key in {"direction", "path", "revision", "media_type", "sha256"}
            }
            _required_string(body, "revision")
            body["claimToken"] = lease.token
            return (
                await self._request(
                    "POST",
                    self._item_path(lease.id) + "/artifacts",
                    json=body,
                ),
                False,
            )
        if name == "work_item_request_human":
            question = _required_string(arguments, "question")
            options = arguments.get("options", [])
            if not isinstance(options, list) or not all(
                isinstance(option, str) and option.strip() for option in options
            ):
                raise ValueError("options must contain non-empty strings")
            body = await self._request(
                "POST",
                self._item_path(lease.id) + "/decisions",
                json={
                    "action": "request",
                    "question": question,
                    "options": options,
                    "actor_id": "codex",
                    "claimToken": lease.token,
                    "threadId": thread_id,
                },
            )
            lease.active = False
            return body, True
        if name == "work_item_complete" and not arguments:
            body = await self._transition(
                lease,
                "stage_review",
                "agent_completed",
                _thread_payload({}, thread_id),
            )
            return body, True
        if name == "work_item_block":
            code = _required_string(arguments, "code")
            message = _required_string(arguments, "message")
            if not re.fullmatch(r"[a-z][a-z0-9_]*", code):
                raise ValueError("blocker code must be snake_case")
            body = await self._transition(
                lease,
                "blocked",
                "work_item_blocked",
                _thread_payload(
                    {"blocker": {"code": code, "message": message}},
                    thread_id,
                ),
            )
            return body, True
        supported = ", ".join(spec["name"] for spec in self.tool_specs())
        raise ValueError(f"unsupported tool {name!r}; supported tools: {supported}")

    async def _transition(
        self,
        lease: ClaimLease,
        status: str,
        event: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        body = await self._request(
            "POST",
            self._item_path(lease.id) + "/status",
            json={
                "to_status": status,
                "event": event,
                "actor_type": "worker",
                "actor_id": self.config.worker_id,
                "claimToken": lease.token,
                "payload": payload,
            },
        )
        if not isinstance(body, dict):
            raise TrackerError("transition response must be an object")
        lease.item = body
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
            payload = response.json()
            error_payload = payload.get("error", {})
            message = error_payload.get("message", response.text)
            error_code = error_payload.get("code")
        except (ValueError, AttributeError):
            message = response.text
            error_code = None
        raise TrackerError(
            f"Control Plane returned {response.status_code}: {message}",
            status_code=response.status_code,
            error_code=error_code if isinstance(error_code, str) else None,
        )

    def _item_path(self, item_id: str) -> str:
        return f"/api/work-items/{quote(item_id, safe='')}"

    @staticmethod
    def _require_active(lease: ClaimLease) -> None:
        if not lease.active:
            raise TrackerError("the current WorkItem claim is no longer active")


def _tool(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "description": description, "inputSchema": schema}


def _object_schema(required: list[str], properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def _required_string(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _thread_payload(payload: dict[str, Any], thread_id: str | None) -> dict[str, Any]:
    if thread_id is None:
        return payload
    return {**payload, "thread_id": thread_id}


def _valid_artifact_path(value: str) -> bool:
    if "\\" in value or "\x00" in value or value.startswith("/"):
        return False
    path = PurePosixPath(value)
    return bool(path.parts) and all(part not in {"", ".", ".."} for part in path.parts)


def _tool_response(success: bool, payload: Any) -> dict[str, Any]:
    output = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return {
        "success": success,
        "output": output,
        "contentItems": [{"type": "inputText", "text": output}],
    }

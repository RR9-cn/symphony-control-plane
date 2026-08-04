from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx

from symphony_windows.workflow import TrackerConfig


class TrackerError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ClaimConflict(TrackerError):
    """A candidate changed or was claimed by another worker."""


@dataclass
class ClaimLease:
    item: dict[str, Any]
    token: str
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
        payload = await self._request("GET", "/api/work-items/candidates", params={"limit": limit})
        if not isinstance(payload, list):
            raise TrackerError("candidate response must be a list")
        return [item for item in payload if isinstance(item, dict)]

    async def get_work_item(self, item_id: str) -> dict[str, Any]:
        payload = await self._request("GET", self._item_path(item_id))
        if not isinstance(payload, dict):
            raise TrackerError("work item response must be an object")
        return payload

    async def claim(self, item: dict[str, Any]) -> ClaimLease:
        item_id = str(item.get("id", ""))
        version = item.get("version")
        if not item_id or not isinstance(version, int):
            raise TrackerError("candidate is missing id or version")
        try:
            payload = await self._request(
                "POST",
                self._item_path(item_id) + "/claim",
                json={
                    "workerId": self.config.worker_id,
                    "expectedVersion": version,
                    "leaseSeconds": self.config.lease_seconds,
                },
            )
        except TrackerError as error:
            if error.status_code == 409:
                raise ClaimConflict(str(error), status_code=409) from error
            raise
        if not isinstance(payload, dict):
            raise TrackerError("claim response must be an object")
        claimed = payload.get("work_item")
        token = payload.get("claim_token")
        if not isinstance(claimed, dict) or not isinstance(token, str) or not token:
            raise TrackerError("claim response is missing work_item or claim_token")
        return ClaimLease(item=claimed, token=token)

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
    ) -> dict[str, Any]:
        self._require_active(lease)
        item = await self._request(
            "POST",
            self._item_path(lease.id) + "/release",
            json={
                "claimToken": lease.token,
                "reason": reason,
                "retryDelaySeconds": retry_delay_seconds,
            },
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
    ) -> ToolExecution:
        try:
            body, stop = await self._execute_tool(lease, name, arguments)
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
                raise ValueError("artifact path must be a safe repository-relative path")
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
                },
            )
            lease.active = False
            return body, True
        if name == "work_item_complete" and not arguments:
            body = await self._transition(
                lease,
                "stage_review",
                "agent_completed",
                {},
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
                {"blocker": {"code": code, "message": message}},
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
            message = payload.get("error", {}).get("message", response.text)
        except (ValueError, AttributeError):
            message = response.text
        raise TrackerError(
            f"Control Plane returned {response.status_code}: {message}",
            status_code=response.status_code,
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

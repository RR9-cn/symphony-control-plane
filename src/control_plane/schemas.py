from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.models import ISSUE_STATUSES


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True, populate_by_name=True)


class RepositoryData(ApiModel):
    url: str = Field(min_length=1)
    base_branch: str = Field(min_length=1)
    commit: str = Field(pattern=r"^[a-fA-F0-9]{40}$")


class RepositoryHeadRequest(ApiModel):
    path: str = Field(min_length=1)


class RepositoryHeadView(ApiModel):
    path: str
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")


class BlockerRef(ApiModel):
    id: str | None = Field(default=None, max_length=64)
    identifier: str | None = Field(default=None, max_length=128)
    state: str | None = Field(default=None, max_length=64)

    @field_validator("state")
    @classmethod
    def normalize_state(cls, value: str | None) -> str | None:
        return value.strip().lower() if value is not None and value.strip() else None


class IssueCreate(ApiModel):
    id: str = Field(min_length=1, max_length=64)
    identifier: str | None = Field(default=None, min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    priority: int = Field(default=2, ge=0, le=4)
    repository: RepositoryData
    acceptance_criteria: list[str] = Field(min_length=1)
    url: str | None = Field(default=None, max_length=2000)
    assignee_id: str | None = Field(default=None, max_length=255)
    labels: list[str] = Field(default_factory=list)
    blocked_by: list[BlockerRef] = Field(default_factory=list)
    native_ref: dict[str, Any] | None = None
    dispatchable: bool = True
    branch_name: str | None = Field(default=None, max_length=255)

    @field_validator("labels")
    @classmethod
    def normalize_labels(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().lower() for value in values]
        if any(not value for value in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("labels must be non-blank and unique after normalization")
        return normalized

    @model_validator(mode="after")
    def normalize_criteria(self) -> "IssueCreate":
        values = [value.strip() for value in self.acceptance_criteria]
        if any(not value for value in values) or len(set(values)) != len(values):
            raise ValueError("acceptance criteria must be non-blank and unique")
        self.acceptance_criteria = values
        self.identifier = (self.identifier or self.id).strip()
        return self


class ClaimView(ApiModel):
    worker_id: str | None
    expires_at: datetime | None


class ArtifactData(ApiModel):
    path: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    media_type: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ArtifactCreate(ArtifactData):
    claim_token: str | None = Field(default=None, exclude=True, validation_alias=AliasChoices("claim_token", "claimToken"))


class ArtifactView(ArtifactData):
    id: str
    issue_id: str
    created_at: datetime


class IssueView(ApiModel):
    id: str
    identifier: str
    title: str
    description: str
    state: str
    status: str
    priority: int
    version: int
    repository: RepositoryData
    acceptance_criteria: list[str]
    url: str | None
    assignee_id: str | None
    labels: list[str]
    blocked_by: list[BlockerRef]
    native_ref: dict[str, Any] | None
    dispatchable: bool
    branch_name: str | None
    blocker: dict[str, Any] | None
    claim: ClaimView
    retry_at: datetime | None
    head_branch: str | None
    local_commit: str | None
    pull_request: str | None
    merged_at: datetime | None
    artifacts: list[ArtifactView]
    created_at: datetime
    updated_at: datetime


class IssuePatch(ApiModel):
    expected_version: int = Field(validation_alias=AliasChoices("expected_version", "expectedVersion"), ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1)
    priority: int | None = Field(default=None, ge=0, le=4)
    acceptance_criteria: list[str] | None = Field(default=None, min_length=1)
    identifier: str | None = Field(default=None, min_length=1, max_length=128)
    url: str | None = Field(default=None, max_length=2000)
    assignee_id: str | None = Field(default=None, max_length=255)
    labels: list[str] | None = None
    blocked_by: list[BlockerRef] | None = None
    native_ref: dict[str, Any] | None = None
    dispatchable: bool | None = None
    branch_name: str | None = Field(default=None, max_length=255)

    @field_validator("labels")
    @classmethod
    def normalize_labels(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized = [value.strip().lower() for value in values]
        if any(not value for value in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("labels must be non-blank and unique after normalization")
        return normalized


class IssueDeliveryCommand(ApiModel):
    action: Literal["approve_result", "authorize_publish", "confirm_merge"]
    expected_version: int = Field(validation_alias=AliasChoices("expected_version", "expectedVersion"), ge=1)
    authorization: bool = False

    @model_validator(mode="after")
    def require_authorization(self) -> "IssueDeliveryCommand":
        if not self.authorization:
            raise ValueError("explicit delivery authorization is required")
        return self


class AgentConfigSnapshot(ApiModel):
    config: dict[str, Any]

    @model_validator(mode="after")
    def reject_credentials(self) -> "AgentConfigSnapshot":
        forbidden = {"token", "secret", "password", "credential", "api_key"}
        if any(str(key).lower() in forbidden for key in self.config):
            raise ValueError("agent config snapshot must not contain credentials")
        return self


class AgentAttemptView(ApiModel):
    id: str
    issue_id: str
    attempt_number: int
    worker_id: str
    config_snapshot: dict[str, Any]
    status: str
    thread_id: str | None
    turn_id: str | None
    turn_count: int
    started_at: datetime
    completed_at: datetime | None


class AttemptContextUpdate(ApiModel):
    claim_token: str = Field(validation_alias=AliasChoices("claim_token", "claimToken"), min_length=16)
    thread_id: str = Field(validation_alias=AliasChoices("thread_id", "threadId"), min_length=1)
    turn_id: str | None = Field(default=None, validation_alias=AliasChoices("turn_id", "turnId"))
    turn_count: int | None = Field(default=None, validation_alias=AliasChoices("turn_count", "turnCount"), ge=0)


class AgentAttemptEventCreate(ApiModel):
    claim_token: str = Field(validation_alias=AliasChoices("claim_token", "claimToken"), min_length=16)
    event_type: str = Field(min_length=1, max_length=64)
    item_type: str | None = Field(default=None, max_length=100)
    status: str | None = Field(default=None, max_length=64)
    summary: str = Field(min_length=1, max_length=1000)
    detail: str | None = Field(default=None, max_length=16000)
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentAttemptEventView(ApiModel):
    id: str
    attempt_id: str
    issue_id: str
    sequence: int
    event_type: str
    item_type: str | None
    status: str | None
    summary: str
    detail: str | None
    payload: dict[str, Any]
    created_at: datetime


class WorkerRegistration(ApiModel):
    worker_id: str = Field(validation_alias=AliasChoices("worker_id", "workerId"), min_length=1)
    hostname: str = Field(min_length=1)
    process_id: int = Field(validation_alias=AliasChoices("process_id", "processId"), ge=1)
    version: str = Field(min_length=1, max_length=50)
    capacity: int = Field(ge=1, le=128)


class WorkerHeartbeat(ApiModel):
    state: Literal["starting", "idle", "running", "stopping"]
    active_issues: list[str] = Field(default_factory=list, validation_alias=AliasChoices("active_issues", "activeIssues"))

    @model_validator(mode="after")
    def unique_issues(self) -> "WorkerHeartbeat":
        if len(set(self.active_issues)) != len(self.active_issues):
            raise ValueError("active_issues must be unique")
        return self


class WorkerView(ApiModel):
    id: str
    hostname: str
    process_id: int
    version: str
    capacity: int
    active_issues: list[str]
    state: str
    stop_requested: bool
    started_at: datetime
    last_seen_at: datetime
    stopped_at: datetime | None


class AgentRuntimeView(ApiModel):
    issue_id: str
    title: str
    state: str
    worker_id: str | None
    attempt_id: str | None
    attempt_number: int | None
    thread_id: str | None
    turn_id: str | None
    turn_count: int
    started_at: datetime | None
    updated_at: datetime


class RunnerControlView(ApiModel):
    state: Literal["stopped", "starting", "running", "stopping"]
    process_id: int | None
    worker_id: str
    workflow: str
    started_at: datetime | None
    last_exit_code: int | None
    recent_logs: list[str]


class ClaimRequest(ApiModel):
    worker_id: str = Field(validation_alias=AliasChoices("worker_id", "workerId"), min_length=1)
    expected_version: int = Field(validation_alias=AliasChoices("expected_version", "expectedVersion"), ge=1)
    lease_seconds: int = Field(default=300, validation_alias=AliasChoices("lease_seconds", "leaseSeconds"), ge=10, le=3600)
    agent: AgentConfigSnapshot


class DecisionView(ApiModel):
    id: str
    issue_id: str
    question: str
    options: list[str]
    status: str
    response: str | None
    requested_by: str | None
    resolved_by: str | None
    created_at: datetime
    resolved_at: datetime | None


class ClaimResult(ApiModel):
    issue: IssueView
    claim_token: str
    attempt: AgentAttemptView
    resume_thread_id: str | None = None
    resume_decisions: list[DecisionView] = Field(default_factory=list)
    continuation_turn_count: int = Field(default=0, ge=0)


class HeartbeatRequest(ApiModel):
    claim_token: str = Field(validation_alias=AliasChoices("claim_token", "claimToken"), min_length=16)
    lease_seconds: int = Field(default=300, validation_alias=AliasChoices("lease_seconds", "leaseSeconds"), ge=10, le=3600)


class ReleaseRequest(ApiModel):
    claim_token: str = Field(validation_alias=AliasChoices("claim_token", "claimToken"), min_length=16)
    reason: str = Field(default="worker_released", min_length=1)
    retry_delay_seconds: int | None = Field(default=None, validation_alias=AliasChoices("retry_delay_seconds", "retryDelaySeconds"), ge=0, le=86400)
    thread_id: str | None = Field(default=None, validation_alias=AliasChoices("thread_id", "threadId"), min_length=1, max_length=200)


class StatusTransitionRequest(ApiModel):
    to_status: str = Field(validation_alias=AliasChoices("to_status", "toStatus"))
    event: str = Field(min_length=1)
    actor_type: str = Field(default="worker", validation_alias=AliasChoices("actor_type", "actorType"))
    actor_id: str | None = Field(default=None, validation_alias=AliasChoices("actor_id", "actorId"))
    claim_token: str | None = Field(default=None, validation_alias=AliasChoices("claim_token", "claimToken"))
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def known_status(self) -> "StatusTransitionRequest":
        if self.to_status not in ISSUE_STATUSES:
            raise ValueError(f"unknown status: {self.to_status}")
        return self


class EventCreate(ApiModel):
    event_type: str = Field(min_length=1)
    actor_type: str = Field(default="agent", min_length=1)
    actor_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    claim_token: str | None = Field(default=None, exclude=True, validation_alias=AliasChoices("claim_token", "claimToken"))


class EventView(ApiModel):
    id: str
    issue_id: str
    event: str
    actor_type: str
    actor_id: str | None
    from_status: str | None
    to_status: str | None
    payload: dict[str, Any]
    created_at: datetime


class DecisionCommand(ApiModel):
    action: Literal["request", "resolve"]
    decision_id: str | None = None
    question: str | None = None
    options: list[str] = Field(default_factory=list)
    response: str | None = None
    actor_id: str | None = None
    thread_id: str | None = Field(default=None, validation_alias=AliasChoices("thread_id", "threadId"))
    claim_token: str | None = Field(default=None, validation_alias=AliasChoices("claim_token", "claimToken"))

    @model_validator(mode="after")
    def validate_action_fields(self) -> "DecisionCommand":
        if self.action == "request" and not self.question:
            raise ValueError("question is required when action=request")
        if self.action == "resolve" and (not self.decision_id or not self.response):
            raise ValueError("decision_id and response are required when action=resolve")
        return self


class MaintenanceResult(ApiModel):
    expired: int
    readied: int

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from control_plane.protocol import PROTOCOL, ROLE_STAGE


class ApiModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", from_attributes=True, populate_by_name=True
    )


class RepositoryData(ApiModel):
    url: str = Field(min_length=1)
    base_branch: str = Field(min_length=1)
    head_branch: str | None = None
    commit: str | None = None
    pull_request: str | None = None


class ManualIssueRepository(RepositoryData):
    commit: str = Field(pattern=r"^[a-fA-F0-9]{40}$")


class RepositoryHeadRequest(ApiModel):
    path: str = Field(min_length=1)


class RepositoryHeadView(ApiModel):
    path: str
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")


class ArtifactData(ApiModel):
    path: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    media_type: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class FeatureCreate(ApiModel):
    id: str = Field(pattern=r"^FEATURE-[0-9]{3,}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = ""


class FeatureView(FeatureCreate):
    status: Literal["active", "awaiting_publish", "pr_open", "done"]
    version: int
    head_branch: str | None
    local_commit: str | None
    pull_request: str | None
    merged_at: datetime | None
    created_at: datetime
    updated_at: datetime


class FeatureDeliveryCommand(ApiModel):
    action: Literal["prepare_local_commit", "authorize_publish", "confirm_merge"]
    expected_version: int = Field(
        validation_alias=AliasChoices("expected_version", "expectedVersion"), ge=1
    )
    authorization: bool = False

    @model_validator(mode="after")
    def require_authorization(self) -> "FeatureDeliveryCommand":
        if self.action != "prepare_local_commit" and not self.authorization:
            raise ValueError("explicit delivery authorization is required")
        return self


class ManualIssueCreate(ApiModel):
    feature_id: str = Field(pattern=r"^FEATURE-[0-9]{3,}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    priority: int = Field(default=2, ge=0, le=4)
    repository: ManualIssueRepository
    acceptance_criteria: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_acceptance_criteria(self) -> "ManualIssueCreate":
        normalized = [criterion.strip() for criterion in self.acceptance_criteria]
        if any(not criterion for criterion in normalized):
            raise ValueError("acceptance criteria must not be blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("acceptance criteria must be unique")
        self.acceptance_criteria = normalized
        return self


class ClaimView(ApiModel):
    worker_id: str | None
    expires_at: datetime | None


class WorkItemCreate(ApiModel):
    id: str = Field(pattern=r"^WI-[0-9]{3,}$")
    feature_id: str = Field(pattern=r"^FEATURE-[0-9]{3,}$")
    parent_id: str | None = Field(default=None, pattern=r"^WI-[0-9]{3,}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    stage: str
    agent_role: str
    status: Literal["draft", "ready"] = "draft"
    priority: int = Field(default=2, ge=0, le=4)
    repository: RepositoryData
    dependencies: list[str] = Field(default_factory=list)
    input_artifacts: list[ArtifactData] = Field(default_factory=list)
    output_artifacts: list[ArtifactData] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_role_stage(self) -> "WorkItemCreate":
        if self.agent_role not in ROLE_STAGE:
            raise ValueError(f"unknown agent_role: {self.agent_role}")
        if ROLE_STAGE[self.agent_role] != self.stage:
            raise ValueError(
                f"{self.agent_role} requires stage {ROLE_STAGE[self.agent_role]}"
            )
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("dependencies must be unique")
        return self


class WorkItemPatch(ApiModel):
    expected_version: int = Field(
        validation_alias=AliasChoices("expected_version", "expectedVersion"), ge=1
    )
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1)
    priority: int | None = Field(default=None, ge=0, le=4)
    repository: RepositoryData | None = None
    acceptance_criteria: list[str] | None = Field(default=None, min_length=1)
    dependencies: list[str] | None = None


class WorkItemView(ApiModel):
    id: str
    feature_id: str
    parent_id: str | None
    title: str
    description: str
    stage: str
    agent_role: str
    status: str
    priority: int
    version: int
    repository: RepositoryData
    dependencies: list[str]
    blocked_by: list[str]
    input_artifacts: list[ArtifactData]
    output_artifacts: list[ArtifactData]
    acceptance_criteria: list[str]
    blocker: dict[str, Any] | None
    claim: ClaimView
    retry_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ManualIssuePreview(ApiModel):
    template: Literal["five_stage_backend_v1"] = "five_stage_backend_v1"
    feature: FeatureCreate
    work_items: list[WorkItemCreate]


class ManualIssueResult(ApiModel):
    template: Literal["five_stage_backend_v1"] = "five_stage_backend_v1"
    feature: FeatureView
    work_items: list[WorkItemView]


class AgentProfileClaim(ApiModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    version: int = Field(ge=1)
    config: dict[str, Any]

    @model_validator(mode="after")
    def validate_snapshot_identity(self) -> "AgentProfileClaim":
        if self.config.get("profile_name") != self.name:
            raise ValueError("profile config profile_name must match name")
        if self.config.get("profile_version") != self.version:
            raise ValueError("profile config profile_version must match version")
        forbidden = {"token", "secret", "password", "credential", "api_key"}
        if any(str(key).lower() in forbidden for key in self.config):
            raise ValueError("profile config must not contain credentials")
        return self


class AgentProfileView(ApiModel):
    id: str
    name: str
    version: int
    config: dict[str, Any]
    active: bool
    created_at: datetime


class AgentAttemptView(ApiModel):
    id: str
    work_item_id: str
    attempt_number: int
    worker_id: str
    profile_id: str | None
    profile_snapshot: dict[str, Any]
    status: str
    thread_id: str | None
    turn_id: str | None
    started_at: datetime
    completed_at: datetime | None


class AttemptContextUpdate(ApiModel):
    claim_token: str = Field(
        validation_alias=AliasChoices("claim_token", "claimToken"), min_length=16
    )
    thread_id: str = Field(
        validation_alias=AliasChoices("thread_id", "threadId"), min_length=1
    )
    turn_id: str | None = Field(
        default=None, validation_alias=AliasChoices("turn_id", "turnId")
    )


class AgentAttemptEventCreate(ApiModel):
    claim_token: str = Field(
        validation_alias=AliasChoices("claim_token", "claimToken"), min_length=16
    )
    event_type: str = Field(min_length=1, max_length=64)
    item_type: str | None = Field(default=None, max_length=100)
    status: str | None = Field(default=None, max_length=64)
    summary: str = Field(min_length=1, max_length=1000)
    detail: str | None = Field(default=None, max_length=16000)
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentAttemptEventView(ApiModel):
    id: str
    attempt_id: str
    work_item_id: str
    sequence: int
    event_type: str
    item_type: str | None
    status: str | None
    summary: str
    detail: str | None
    payload: dict[str, Any]
    created_at: datetime


class WorkerRegistration(ApiModel):
    worker_id: str = Field(
        validation_alias=AliasChoices("worker_id", "workerId"), min_length=1
    )
    hostname: str = Field(min_length=1)
    process_id: int = Field(
        validation_alias=AliasChoices("process_id", "processId"), ge=1
    )
    version: str = Field(min_length=1, max_length=50)
    capacity: int = Field(ge=1, le=128)
    profiles: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_profiles(self) -> "WorkerRegistration":
        if len(set(self.profiles)) != len(self.profiles):
            raise ValueError("profiles must be unique")
        return self


class WorkerHeartbeat(ApiModel):
    state: Literal["starting", "idle", "running", "stopping"]
    active_work_items: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("active_work_items", "activeWorkItems"),
    )
    active_profiles: dict[str, str] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("active_profiles", "activeProfiles"),
    )

    @model_validator(mode="after")
    def validate_active_work(self) -> "WorkerHeartbeat":
        if len(set(self.active_work_items)) != len(self.active_work_items):
            raise ValueError("active_work_items must be unique")
        if set(self.active_profiles) - set(self.active_work_items):
            raise ValueError("active_profiles keys must be active work items")
        return self


class WorkerView(ApiModel):
    id: str
    hostname: str
    process_id: int
    version: str
    capacity: int
    profiles: list[str]
    active_work_items: list[str]
    active_profiles: dict[str, str]
    state: str
    stop_requested: bool
    started_at: datetime
    last_seen_at: datetime
    stopped_at: datetime | None


class AgentRuntimeView(ApiModel):
    work_item_id: str
    feature_id: str
    title: str
    agent_role: str
    stage: str
    state: str
    worker_id: str | None
    attempt_id: str | None
    attempt_number: int | None
    profile_name: str | None
    profile_version: int | None
    thread_id: str | None
    turn_id: str | None
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
    worker_id: str = Field(
        validation_alias=AliasChoices("worker_id", "workerId"), min_length=1
    )
    expected_version: int = Field(
        validation_alias=AliasChoices("expected_version", "expectedVersion"), ge=1
    )
    lease_seconds: int = Field(
        default=300,
        validation_alias=AliasChoices("lease_seconds", "leaseSeconds"),
        ge=10,
        le=3600,
    )
    profile: AgentProfileClaim | None = None


class DecisionView(ApiModel):
    id: str
    work_item_id: str
    question: str
    options: list[str]
    status: str
    response: str | None
    requested_by: str | None
    resolved_by: str | None
    created_at: datetime
    resolved_at: datetime | None


class ClaimResult(ApiModel):
    work_item: WorkItemView
    claim_token: str
    attempt: AgentAttemptView
    resume_thread_id: str | None = None
    resume_decisions: list[DecisionView] = Field(default_factory=list)
    continuation_turn_count: int = Field(default=0, ge=0)


class HeartbeatRequest(ApiModel):
    claim_token: str = Field(
        validation_alias=AliasChoices("claim_token", "claimToken"), min_length=16
    )
    lease_seconds: int = Field(
        default=300,
        validation_alias=AliasChoices("lease_seconds", "leaseSeconds"),
        ge=10,
        le=3600,
    )


class ReleaseRequest(ApiModel):
    claim_token: str = Field(
        validation_alias=AliasChoices("claim_token", "claimToken"), min_length=16
    )
    reason: str = Field(default="worker_released", min_length=1)
    retry_delay_seconds: int | None = Field(
        default=None,
        validation_alias=AliasChoices("retry_delay_seconds", "retryDelaySeconds"),
        ge=0,
        le=86_400,
    )
    thread_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("thread_id", "threadId"),
        min_length=1,
        max_length=200,
    )


class StatusTransitionRequest(ApiModel):
    to_status: str = Field(validation_alias=AliasChoices("to_status", "toStatus"))
    event: str
    actor_type: str = Field(
        default="worker", validation_alias=AliasChoices("actor_type", "actorType")
    )
    actor_id: str | None = Field(
        default=None, validation_alias=AliasChoices("actor_id", "actorId")
    )
    claim_token: str | None = Field(
        default=None, validation_alias=AliasChoices("claim_token", "claimToken")
    )
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def known_status(self) -> "StatusTransitionRequest":
        if self.to_status not in PROTOCOL.statuses:
            raise ValueError(f"unknown status: {self.to_status}")
        return self


class EventCreate(ApiModel):
    event_type: str = Field(min_length=1)
    actor_type: str = Field(default="agent", min_length=1)
    actor_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    claim_token: str | None = Field(
        default=None,
        exclude=True,
        validation_alias=AliasChoices("claim_token", "claimToken"),
    )


class EventView(EventCreate):
    id: str
    work_item_id: str
    from_status: str | None
    to_status: str | None
    created_at: datetime


class ArtifactCreate(ArtifactData):
    direction: Literal["input", "output"]
    created_by_attempt_id: str | None = None
    claim_token: str | None = Field(
        default=None,
        exclude=True,
        validation_alias=AliasChoices("claim_token", "claimToken"),
    )


class ArtifactView(ArtifactCreate):
    id: str
    work_item_id: str
    created_at: datetime


class DecisionCommand(ApiModel):
    action: Literal["request", "resolve"]
    decision_id: str | None = None
    question: str | None = None
    options: list[str] = Field(default_factory=list)
    response: str | None = None
    actor_id: str | None = None
    thread_id: str | None = Field(
        default=None, validation_alias=AliasChoices("thread_id", "threadId")
    )
    claim_token: str | None = Field(
        default=None, validation_alias=AliasChoices("claim_token", "claimToken")
    )

    @model_validator(mode="after")
    def validate_action_fields(self) -> "DecisionCommand":
        if self.action == "request" and not self.question:
            raise ValueError("question is required when action=request")
        if self.action == "resolve" and (not self.decision_id or not self.response):
            raise ValueError(
                "decision_id and response are required when action=resolve"
            )
        return self


class MaintenanceResult(ApiModel):
    expired: int
    readied: int

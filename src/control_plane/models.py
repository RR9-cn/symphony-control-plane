from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


JSON_VALUE = JSON()
ISSUE_STATUSES = frozenset(
    {
        "ready",
        "running",
        "retry_queued",
        "needs_human",
        "blocked",
        "reviewing",
        "awaiting_publish",
        "pr_open",
        "done",
        "cancelled",
    }
)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("key", name="uq_projects_key"),
        UniqueConstraint("repository_path", name="uq_projects_repository_path"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    key: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    repository_path: Mapped[str] = mapped_column(String(1000))
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    workflow_path: Mapped[str] = mapped_column(String(500), default="WORKFLOW.md")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), default="invalid")
    validation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class ProjectWorkflowSnapshot(Base):
    __tablename__ = "project_workflow_snapshots"
    __table_args__ = (
        UniqueConstraint("project_id", "source_commit", "workflow_revision", "status", name="uq_project_commit_workflow_status"),
        Index("ix_project_snapshots_created", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    source_commit: Mapped[str] = mapped_column(String(40))
    workflow_revision: Mapped[str] = mapped_column(String(64))
    workflow_content: Mapped[str] = mapped_column(Text)
    parsed_config: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    status: Mapped[str] = mapped_column(String(32), default="valid")
    validation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Issue(Base):
    __tablename__ = "issues"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in sorted(ISSUE_STATUSES)) + ")",
            name="ck_issues_status",
        ),
        CheckConstraint("priority BETWEEN 0 AND 4", name="ck_issues_priority"),
        CheckConstraint("version >= 1", name="ck_issues_version"),
        CheckConstraint(
            "(status = 'running' AND claim_worker_id IS NOT NULL "
            "AND claim_token_hash IS NOT NULL AND claim_expires_at IS NOT NULL) OR "
            "(status <> 'running' AND claim_worker_id IS NULL "
            "AND claim_token_hash IS NULL AND claim_expires_at IS NULL)",
            name="ck_issues_claim_state",
        ),
        Index("ix_issues_candidate_order", "project_id", "status", "priority", "created_at"),
        Index("ix_issues_expired_claims", "status", "claim_expires_at"),
        UniqueConstraint("identifier", name="uq_issues_identifier"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), index=True)
    workflow_snapshot_id: Mapped[str] = mapped_column(ForeignKey("project_workflow_snapshots.id", ondelete="RESTRICT"))
    source_commit: Mapped[str] = mapped_column(String(40))
    workspace_path: Mapped[str] = mapped_column(String(1000))
    identifier: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="ready")
    priority: Mapped[int] = mapped_column(Integer, default=2)
    version: Mapped[int] = mapped_column(Integer, default=1)
    acceptance_criteria: Mapped[list[str]] = mapped_column(JSON_VALUE)
    url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    assignee_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    labels: Mapped[list[str]] = mapped_column(JSON_VALUE, default=list)
    blocked_by: Mapped[list[dict[str, Any]]] = mapped_column(JSON_VALUE, default=list)
    native_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE, nullable=True)
    dispatchable: Mapped[bool] = mapped_column(Boolean, default=True)
    branch_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    blocker: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE, nullable=True)
    claim_worker_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    claim_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    head_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    local_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    pull_request: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class IssueArtifact(Base):
    __tablename__ = "issue_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    issue_id: Mapped[str] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(1000))
    revision: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class IssueEvent(Base):
    __tablename__ = "issue_events"
    __table_args__ = (Index("ix_issue_events_issue_created", "issue_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    issue_id: Mapped[str] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), index=True)
    event: Mapped[str] = mapped_column(String(100))
    actor_type: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Worker(Base):
    __tablename__ = "workers"
    __table_args__ = (Index("ix_workers_last_seen", "last_seen_at"),)

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    hostname: Mapped[str] = mapped_column(String(255))
    process_id: Mapped[int] = mapped_column(Integer)
    version: Mapped[str] = mapped_column(String(50))
    capacity: Mapped[int] = mapped_column(Integer)
    active_issues: Mapped[list[str]] = mapped_column(JSON_VALUE, default=list)
    runtime_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict)
    runtime_snapshot_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    state: Mapped[str] = mapped_column(String(32), default="starting")
    stop_requested: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentAttempt(Base):
    __tablename__ = "agent_attempts"
    __table_args__ = (UniqueConstraint("issue_id", "attempt_number", name="uq_issue_attempt_number"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    issue_id: Mapped[str] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str] = mapped_column(String(200))
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="running")
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def session_id(self) -> str | None:
        # Codex resumes a conversation by thread ID; turn IDs identify messages
        # within that stable session and must not become part of its identity.
        return self.thread_id

    @property
    def duration_seconds(self) -> float:
        started_at = self.started_at
        completed_at = self.completed_at or utc_now()
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=timezone.utc)
        return max(0.0, round((completed_at - started_at).total_seconds(), 3))


class AgentAttemptEvent(Base):
    __tablename__ = "agent_attempt_events"
    __table_args__ = (
        UniqueConstraint("attempt_id", "sequence", name="uq_attempt_event_sequence"),
        Index("ix_attempt_events_attempt_sequence", "attempt_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("agent_attempts.id", ondelete="CASCADE"), index=True)
    issue_id: Mapped[str] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    item_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(String(1000))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class HumanDecision(Base):
    __tablename__ = "human_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    issue_id: Mapped[str] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    options: Mapped[list[str]] = mapped_column(JSON_VALUE, default=list)
    status: Mapped[str] = mapped_column(String(16), default="open")
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

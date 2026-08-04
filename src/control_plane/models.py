from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from control_plane.protocol import PROTOCOL, ROLE_STAGE


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


JSON_VALUE = JSON()


def _sql_values(values: set[str] | frozenset[str]) -> str:
    return ", ".join(f"'{value}'" for value in sorted(values))


class Base(DeclarativeBase):
    pass


class Feature(Base):
    __tablename__ = "features"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class WorkItem(Base):
    __tablename__ = "work_items"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_sql_values(PROTOCOL.statuses)})", name="ck_work_items_status"
        ),
        CheckConstraint(
            f"stage IN ({_sql_values(set(ROLE_STAGE.values()))})",
            name="ck_work_items_stage",
        ),
        CheckConstraint(
            f"agent_role IN ({_sql_values(set(ROLE_STAGE))})",
            name="ck_work_items_agent_role",
        ),
        CheckConstraint("priority BETWEEN 0 AND 4", name="ck_work_items_priority"),
        CheckConstraint("version >= 1", name="ck_work_items_version"),
        CheckConstraint(
            "(status = 'running' AND claim_worker_id IS NOT NULL "
            "AND claim_token_hash IS NOT NULL AND claim_expires_at IS NOT NULL) OR "
            "(status <> 'running' AND claim_worker_id IS NULL "
            "AND claim_token_hash IS NULL AND claim_expires_at IS NULL)",
            name="ck_work_items_claim_state",
        ),
        Index("ix_work_items_candidate_order", "status", "priority", "created_at"),
        Index("ix_work_items_expired_claims", "status", "claim_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    feature_id: Mapped[str] = mapped_column(
        ForeignKey("features.id", ondelete="CASCADE"), index=True
    )
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("work_items.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(String(32))
    agent_role: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="draft")
    priority: Mapped[int] = mapped_column(Integer, default=2)
    version: Mapped[int] = mapped_column(Integer, default=1)
    repository: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    acceptance_criteria: Mapped[list[str]] = mapped_column(JSON_VALUE)
    blocker: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE, nullable=True)
    claim_worker_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    claim_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class WorkItemDependency(Base):
    __tablename__ = "work_item_dependencies"
    __table_args__ = (
        CheckConstraint("work_item_id <> depends_on_id", name="ck_dependency_not_self"),
    )

    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), primary_key=True
    )
    depends_on_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="RESTRICT"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkItemArtifact(Base):
    __tablename__ = "work_item_artifacts"
    __table_args__ = (
        CheckConstraint("direction IN ('input', 'output')", name="ck_artifact_direction"),
        UniqueConstraint(
            "work_item_id", "direction", "path", "revision", name="uq_artifact_identity"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(String(1000))
    revision: Mapped[str] = mapped_column(String(200))
    media_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_attempts.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkItemEvent(Base):
    __tablename__ = "work_item_events"
    __table_args__ = (Index("ix_events_work_item_created", "work_item_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(100))
    actor_type: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AgentProfile(Base):
    __tablename__ = "agent_profiles"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_profile_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AgentAttempt(Base):
    __tablename__ = "agent_attempts"
    __table_args__ = (
        UniqueConstraint("work_item_id", "attempt_number", name="uq_attempt_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str] = mapped_column(String(200))
    profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_profiles.id", ondelete="SET NULL"), nullable=True
    )
    profile_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="running")
    thread_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class HumanDecision(Base):
    __tablename__ = "human_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), index=True
    )
    question: Mapped[str] = mapped_column(Text)
    options: Mapped[list[str]] = mapped_column(JSON_VALUE, default=list)
    status: Mapped[str] = mapped_column(String(16), default="open")
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

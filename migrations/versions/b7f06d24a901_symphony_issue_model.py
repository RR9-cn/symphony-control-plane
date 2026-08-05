"""Symphony single-issue control-plane model.

Revision ID: b7f06d24a901
Revises:
Create Date: 2026-08-05
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b7f06d24a901"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "issues",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("repository", sa.JSON(), nullable=False),
        sa.Column("acceptance_criteria", sa.JSON(), nullable=False),
        sa.Column("blocker", sa.JSON(), nullable=True),
        sa.Column("claim_worker_id", sa.String(200), nullable=True),
        sa.Column("claim_token_hash", sa.String(64), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("head_branch", sa.String(255), nullable=True),
        sa.Column("local_commit", sa.String(40), nullable=True),
        sa.Column("pull_request", sa.String(2000), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("priority BETWEEN 0 AND 4", name="ck_issues_priority"),
        sa.CheckConstraint("version >= 1", name="ck_issues_version"),
        sa.CheckConstraint("status IN ('awaiting_publish','blocked','cancelled','done','needs_human','pr_open','ready','retry_queued','reviewing','running')", name="ck_issues_status"),
        sa.CheckConstraint("(status = 'running' AND claim_worker_id IS NOT NULL AND claim_token_hash IS NOT NULL AND claim_expires_at IS NOT NULL) OR (status <> 'running' AND claim_worker_id IS NULL AND claim_token_hash IS NULL AND claim_expires_at IS NULL)", name="ck_issues_claim_state"),
    )
    op.create_index("ix_issues_candidate_order", "issues", ["status", "priority", "created_at"])
    op.create_index("ix_issues_expired_claims", "issues", ["status", "claim_expires_at"])

    op.create_table(
        "issue_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("issue_id", sa.String(64), sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.String(1000), nullable=False),
        sa.Column("revision", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(255), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_issue_artifacts_issue_id", "issue_artifacts", ["issue_id"])

    op.create_table(
        "issue_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("issue_id", sa.String(64), sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event", sa.String(100), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(200), nullable=True),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_issue_events_issue_id", "issue_events", ["issue_id"])
    op.create_index("ix_issue_events_issue_created", "issue_events", ["issue_id", "created_at"])

    op.create_table(
        "workers",
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("process_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("active_issues", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("stop_requested", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_workers_last_seen", "workers", ["last_seen_at"])

    op.create_table(
        "agent_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("issue_id", sa.String(64), sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(200), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("thread_id", sa.String(200), nullable=True),
        sa.Column("turn_id", sa.String(200), nullable=True),
        sa.Column("turn_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("issue_id", "attempt_number", name="uq_issue_attempt_number"),
    )
    op.create_index("ix_agent_attempts_issue_id", "agent_attempts", ["issue_id"])

    op.create_table(
        "agent_attempt_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("attempt_id", sa.String(36), sa.ForeignKey("agent_attempts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("issue_id", sa.String(64), sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("item_type", sa.String(100), nullable=True),
        sa.Column("status", sa.String(64), nullable=True),
        sa.Column("summary", sa.String(1000), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("attempt_id", "sequence", name="uq_attempt_event_sequence"),
    )
    op.create_index("ix_agent_attempt_events_attempt_id", "agent_attempt_events", ["attempt_id"])
    op.create_index("ix_agent_attempt_events_issue_id", "agent_attempt_events", ["issue_id"])
    op.create_index("ix_attempt_events_attempt_sequence", "agent_attempt_events", ["attempt_id", "sequence"])

    op.create_table(
        "human_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("issue_id", sa.String(64), sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.String(200), nullable=True),
        sa.Column("resolved_by", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_human_decisions_issue_id", "human_decisions", ["issue_id"])


def downgrade() -> None:
    op.drop_table("human_decisions")
    op.drop_table("agent_attempt_events")
    op.drop_table("agent_attempts")
    op.drop_table("workers")
    op.drop_table("issue_events")
    op.drop_table("issue_artifacts")
    op.drop_table("issues")

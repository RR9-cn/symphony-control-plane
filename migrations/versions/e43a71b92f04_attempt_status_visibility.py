"""Persist the reason associated with an Attempt terminal status.

Revision ID: e43a71b92f04
Revises: d29f84a17c33
Create Date: 2026-08-05
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e43a71b92f04"
down_revision: str | None = "d29f84a17c33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("agent_attempts")
    }
    if "status_reason" not in columns:
        with op.batch_alter_table("agent_attempts") as batch:
            batch.add_column(sa.Column("status_reason", sa.Text(), nullable=True))

    # Existing retry reasons live in Issue audit events. Associate each ended
    # retry Attempt with the first retry_scheduled event emitted after it began.
    op.execute(
        """
        UPDATE agent_attempts AS attempt
        SET status_reason = (
            SELECT json_extract(event.payload, '$.reason')
            FROM issue_events AS event
            WHERE event.issue_id = attempt.issue_id
              AND event.event = 'retry_scheduled'
              AND event.created_at >= attempt.started_at
            ORDER BY event.created_at ASC
            LIMIT 1
        )
        WHERE attempt.status = 'retry_queued'
        """
    )


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("agent_attempts")
    }
    if "status_reason" in columns:
        with op.batch_alter_table("agent_attempts") as batch:
            batch.drop_column("status_reason")

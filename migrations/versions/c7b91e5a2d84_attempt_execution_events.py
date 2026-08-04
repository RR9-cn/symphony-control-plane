"""attempt execution events

Revision ID: c7b91e5a2d84
Revises: 8a4c1d2e3f40
Create Date: 2026-08-04 18:45:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7b91e5a2d84"
down_revision: Union[str, Sequence[str], None] = "8a4c1d2e3f40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_attempt_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("work_item_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("item_type", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.String(length=1000), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["agent_attempts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["work_item_id"], ["work_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "attempt_id", "sequence", name="uq_attempt_event_sequence"
        ),
    )
    with op.batch_alter_table("agent_attempt_events", schema=None) as batch_op:
        batch_op.create_index(
            "ix_agent_attempt_events_attempt_id", ["attempt_id"], unique=False
        )
        batch_op.create_index(
            "ix_agent_attempt_events_work_item_id", ["work_item_id"], unique=False
        )
        batch_op.create_index(
            "ix_attempt_events_attempt_sequence",
            ["attempt_id", "sequence"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_attempt_events", schema=None) as batch_op:
        batch_op.drop_index("ix_attempt_events_attempt_sequence")
        batch_op.drop_index("ix_agent_attempt_events_work_item_id")
        batch_op.drop_index("ix_agent_attempt_events_attempt_id")
    op.drop_table("agent_attempt_events")

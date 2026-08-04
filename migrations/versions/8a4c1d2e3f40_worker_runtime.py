"""worker runtime and attempt context

Revision ID: 8a4c1d2e3f40
Revises: ee63ed53e1cc
Create Date: 2026-08-04 18:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8a4c1d2e3f40"
down_revision: Union[str, Sequence[str], None] = "ee63ed53e1cc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workers",
        sa.Column("id", sa.String(length=200), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("process_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("profiles", sa.JSON(), nullable=False),
        sa.Column("active_work_items", sa.JSON(), nullable=False),
        sa.Column("active_profiles", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("stop_requested", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("workers", schema=None) as batch_op:
        batch_op.create_index("ix_workers_last_seen", ["last_seen_at"], unique=False)
    with op.batch_alter_table("agent_attempts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("turn_id", sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_attempts", schema=None) as batch_op:
        batch_op.drop_column("turn_id")
    with op.batch_alter_table("workers", schema=None) as batch_op:
        batch_op.drop_index("ix_workers_last_seen")
    op.drop_table("workers")

"""Cache authoritative Worker runtime snapshots.

Revision ID: f54b82ca3015
Revises: e43a71b92f04
Create Date: 2026-08-05
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "f54b82ca3015"
down_revision: str | None = "e43a71b92f04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("workers")
    }
    with op.batch_alter_table("workers") as batch:
        if "runtime_snapshot" not in columns:
            batch.add_column(
                sa.Column(
                    "runtime_snapshot",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'{}'"),
                )
            )
        if "runtime_snapshot_at" not in columns:
            batch.add_column(
                sa.Column("runtime_snapshot_at", sa.DateTime(timezone=True), nullable=True)
            )


def downgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("workers")
    }
    with op.batch_alter_table("workers") as batch:
        if "runtime_snapshot_at" in columns:
            batch.drop_column("runtime_snapshot_at")
        if "runtime_snapshot" in columns:
            batch.drop_column("runtime_snapshot")

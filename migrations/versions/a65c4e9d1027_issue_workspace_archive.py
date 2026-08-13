"""Track force-archived Issue workspaces.

Revision ID: a65c4e9d1027
Revises: f54b82ca3015
Create Date: 2026-08-07
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a65c4e9d1027"
down_revision: str | None = "f54b82ca3015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("issues")
    }
    if "archived_at" not in columns:
        with op.batch_alter_table("issues") as batch:
            batch.add_column(
                sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
            )


def downgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("issues")
    }
    if "archived_at" in columns:
        with op.batch_alter_table("issues") as batch:
            batch.drop_column("archived_at")

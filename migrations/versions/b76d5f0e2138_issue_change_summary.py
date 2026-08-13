"""Persist Issue change summaries.

Revision ID: b76d5f0e2138
Revises: a65c4e9d1027
Create Date: 2026-08-07
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b76d5f0e2138"
down_revision: str | None = "a65c4e9d1027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("issues")
    }
    if "change_summary" not in columns:
        with op.batch_alter_table("issues") as batch:
            batch.add_column(
                sa.Column(
                    "change_summary",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'{}'"),
                )
            )


def downgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("issues")
    }
    if "change_summary" in columns:
        with op.batch_alter_table("issues") as batch:
            batch.drop_column("change_summary")

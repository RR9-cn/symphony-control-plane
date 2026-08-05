"""Add Symphony normalized Issue dispatch fields.

Revision ID: c18d4b7e2a10
Revises: b7f06d24a901
Create Date: 2026-08-05
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c18d4b7e2a10"
down_revision: str | None = "b7f06d24a901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("issues") as batch:
        batch.add_column(sa.Column("identifier", sa.String(128), nullable=True))
        batch.add_column(sa.Column("url", sa.String(2000), nullable=True))
        batch.add_column(sa.Column("assignee_id", sa.String(255), nullable=True))
        batch.add_column(
            sa.Column("labels", sa.JSON(), nullable=False, server_default="[]")
        )
        batch.add_column(
            sa.Column("blocked_by", sa.JSON(), nullable=False, server_default="[]")
        )
        batch.add_column(sa.Column("native_ref", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column(
                "dispatchable", sa.Boolean(), nullable=False, server_default=sa.true()
            )
        )
        batch.add_column(sa.Column("branch_name", sa.String(255), nullable=True))
    op.execute("UPDATE issues SET identifier = id WHERE identifier IS NULL")
    with op.batch_alter_table("issues") as batch:
        batch.alter_column("identifier", existing_type=sa.String(128), nullable=False)
        batch.create_unique_constraint("uq_issues_identifier", ["identifier"])


def downgrade() -> None:
    with op.batch_alter_table("issues") as batch:
        batch.drop_constraint("uq_issues_identifier", type_="unique")
        batch.drop_column("branch_name")
        batch.drop_column("dispatchable")
        batch.drop_column("native_ref")
        batch.drop_column("blocked_by")
        batch.drop_column("labels")
        batch.drop_column("assignee_id")
        batch.drop_column("url")
        batch.drop_column("identifier")

"""feature delivery lifecycle

Revision ID: d93f617c0a21
Revises: c7b91e5a2d84
Create Date: 2026-08-04 21:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d93f617c0a21"
down_revision: Union[str, Sequence[str], None] = "c7b91e5a2d84"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("features", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("status", sa.String(length=32), nullable=False, server_default="active")
        )
        batch_op.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(sa.Column("head_branch", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("local_commit", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("pull_request", sa.String(length=2000), nullable=True))
        batch_op.add_column(sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_check_constraint(
            "ck_features_status",
            "status IN ('active', 'awaiting_publish', 'pr_open', 'done')",
        )
        batch_op.create_check_constraint("ck_features_version", "version >= 1")


def downgrade() -> None:
    with op.batch_alter_table("features", schema=None) as batch_op:
        batch_op.drop_constraint("ck_features_version", type_="check")
        batch_op.drop_constraint("ck_features_status", type_="check")
        batch_op.drop_column("merged_at")
        batch_op.drop_column("pull_request")
        batch_op.drop_column("local_commit")
        batch_op.drop_column("head_branch")
        batch_op.drop_column("version")
        batch_op.drop_column("status")

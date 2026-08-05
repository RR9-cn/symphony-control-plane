"""Replace V1 data with project-repository V2.

Revision ID: d29f84a17c33
Revises: c18d4b7e2a10
Create Date: 2026-08-05

This migration is intentionally destructive. V1 Issues cannot be assigned a
trustworthy repository-owned WORKFLOW snapshot, so all operational data is
discarded instead of being guessed or silently made compatible.
"""

from collections.abc import Sequence

from alembic import op

from control_plane.models import Base


revision: str = "d29f84a17c33"
down_revision: str | None = "c18d4b7e2a10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    Base.metadata.drop_all(bind=connection)
    Base.metadata.create_all(bind=connection)


def downgrade() -> None:
    raise RuntimeError("project-repository V2 is an intentionally irreversible reset")

"""live workflow progress columns

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analyses", sa.Column("current_step", sa.String(length=60), nullable=True))
    op.add_column("analyses", sa.Column("worker_id", sa.String(length=200), nullable=True))
    op.add_column("analyses", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_analyses_status", "analyses", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_analyses_status", table_name="analyses")
    op.drop_column("analyses", "heartbeat_at")
    op.drop_column("analyses", "worker_id")
    op.drop_column("analyses", "current_step")

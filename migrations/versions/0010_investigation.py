"""bounded historical investigation results on analyses

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analyses", sa.Column("investigation", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("analyses", "investigation")

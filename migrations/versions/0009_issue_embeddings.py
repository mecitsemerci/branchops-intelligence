"""optional semantic retrieval: issue embeddings (REAL[], no pgvector)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "issue_embeddings",
        sa.Column("incident_key", sa.String(length=200), primary_key=True),
        sa.Column("model", sa.String(length=120), primary_key=True),
        sa.Column("vector", postgresql.ARRAY(postgresql.REAL()), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
    )
    op.create_index("ix_issue_embeddings_model_updated", "issue_embeddings", ["model", "updated_at"])


def downgrade() -> None:
    op.drop_table("issue_embeddings")

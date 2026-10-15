"""in-app reports: draft tokens, attachments, masked app context

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incident_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("token_sha256", sa.String(length=64), nullable=False, unique=True),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("incident_key", sa.String(length=200), nullable=True),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("incident_key", sa.String(length=200), nullable=True, index=True),
        sa.Column(
            "draft_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("incident_drafts.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column("filename", sa.String(length=200), nullable=False),
        sa.Column("content_type", sa.String(length=60), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage", sa.String(length=20), nullable=False),
        sa.Column("storage_ref", sa.String(length=300), nullable=False),
        sa.Column("data", postgresql.BYTEA(), nullable=True),
        sa.Column("blurred_regions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
            index=True,
        ),
    )
    op.create_table(
        "incident_app_context",
        sa.Column("incident_key", sa.String(length=200), primary_key=True),
        sa.Column("context", postgresql.JSONB(), nullable=False),
        sa.Column(
            "security_flags", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("incident_app_context")
    op.drop_table("attachments")
    op.drop_table("incident_drafts")

"""model recovery: recoverable analyses, recovery state machine, inbox retention index

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "analyses", sa.Column("recoverable", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.create_table(
        "analysis_recovery",
        sa.Column("incident_key", sa.String(length=200), primary_key=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("analysis_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("inbox_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_analysis_recovery_due", "analysis_recovery", ["state", "next_attempt_at"])
    # Inbox retention (purge_finished) filters finished events by age.
    op.create_index("ix_inbox_finished", "webhook_inbox", ["status", "processed_at"])


def downgrade() -> None:
    op.drop_index("ix_inbox_finished", table_name="webhook_inbox")
    op.drop_table("analysis_recovery")
    op.drop_column("analyses", "recoverable")

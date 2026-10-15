"""routing decisions of reported incidents and the notification outbox

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _outbox_columns() -> list[sa.Column[object]]:
    """Same columns as ``ticketing_outbox`` (``OutboxMixin``); revisions are kept self-contained."""
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=300), nullable=False, unique=True),
        sa.Column("incident_key", sa.String(length=200), nullable=True, index=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="8"),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("lease_owner", sa.String(length=120), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "incident_routing",
        sa.Column("incident_key", sa.String(length=200), primary_key=True),
        sa.Column("owner_team_id", sa.Integer(), nullable=True, index=True),
        sa.Column("to_team_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("cc_team_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("channels", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("matched_level", sa.String(length=40), nullable=False),
        sa.Column("unowned_screen", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("screen_unregistered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("application_key", sa.String(length=64), nullable=True),
        sa.Column("screen_key", sa.String(length=64), nullable=True),
        sa.Column(
            "matched_rule_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("reasons", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("decided_by", sa.String(length=200), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("first_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "notification_outbox",
        *_outbox_columns(),
        sa.Column("event", sa.String(length=40), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=True),
        sa.Column("role", sa.String(length=10), nullable=False, server_default="to"),
    )
    op.create_index("ix_notification_outbox_claim", "notification_outbox", ["status", "available_at"])


def downgrade() -> None:
    op.drop_table("notification_outbox")
    op.drop_table("incident_routing")

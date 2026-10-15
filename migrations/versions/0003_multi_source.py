"""multi-source incidents: incident_key identity, inbox source, pull-source checkpoints

Existing data is Jira data: every key is backfilled as ``jira:<issue_key>``. ``issues.issue_key`` is kept
as a non-null legacy column (equal to ``external_key``) for one release.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, constraint, column) of every foreign key pointing at issues.issue_key in revision 0002.
_OLD_FKS = (
    ("analyses", "analyses_issue_key_fkey", "issue_key"),
    ("issue_comments", "issue_comments_issue_key_fkey", "issue_key"),
    ("issue_relationships", "issue_relationships_source_issue_key_fkey", "source_issue_key"),
    ("issue_relationships", "issue_relationships_target_issue_key_fkey", "target_issue_key"),
)
_NEW_FKS = (
    ("analyses", "analyses_incident_key_fkey", "incident_key"),
    ("issue_comments", "issue_comments_incident_key_fkey", "incident_key"),
    ("issue_relationships", "issue_relationships_source_incident_key_fkey", "source_incident_key"),
    ("issue_relationships", "issue_relationships_target_incident_key_fkey", "target_incident_key"),
)
_LEGACY_EVENT_TYPES = {
    "ISSUE_CREATED": "RECORD_CREATED",
    "ISSUE_UPDATED": "RECORD_UPDATED",
    "ISSUE_DELETED": "RECORD_DELETED",
}


def upgrade() -> None:
    # --- inbox ------------------------------------------------------------------------------------
    op.add_column(
        "webhook_inbox", sa.Column("source", sa.String(length=40), server_default="jira", nullable=False)
    )
    op.alter_column("webhook_inbox", "issue_key", type_=sa.String(length=160))
    op.alter_column("webhook_inbox", "project_key", new_column_name="group_key", type_=sa.String(length=120))
    op.drop_index("ix_inbox_issue", table_name="webhook_inbox")
    op.create_index("ix_inbox_incident", "webhook_inbox", ["source", "issue_key", "received_at"])
    op.create_index("ix_inbox_source_claim", "webhook_inbox", ["source", "status", "available_at"])
    for old, new in _LEGACY_EVENT_TYPES.items():
        op.execute(
            sa.text("UPDATE webhook_inbox SET event_type = :new WHERE event_type = :old").bindparams(
                old=old, new=new
            )
        )

    # --- issues: new identity ---------------------------------------------------------------------
    op.add_column("issues", sa.Column("source", sa.String(length=40), server_default="jira", nullable=False))
    op.add_column("issues", sa.Column("external_key", sa.String(length=160), nullable=True))
    op.add_column("issues", sa.Column("incident_key", sa.String(length=200), nullable=True))
    op.execute("UPDATE issues SET external_key = issue_key, incident_key = 'jira:' || issue_key")
    op.alter_column("issues", "external_key", nullable=False)
    op.alter_column("issues", "incident_key", nullable=False)
    op.alter_column("issues", "issue_key", type_=sa.String(length=160))
    op.alter_column("issues", "issue_id", new_column_name="external_id")
    op.alter_column("issues", "project_key", new_column_name="group_key", type_=sa.String(length=120))
    op.execute("ALTER INDEX ix_issues_project_key RENAME TO ix_issues_group_key")
    # Stored snapshots: rename the Jira-era keys (the model also accepts the old shape).
    op.execute(
        """
        UPDATE issues SET snapshot =
            (snapshot - 'issue_key' - 'issue_id' - 'project_key')
            || jsonb_build_object(
                'source', 'jira',
                'external_key', issue_key,
                'external_id', snapshot -> 'issue_id',
                'group_key', snapshot -> 'project_key',
                'field_versions',
                    (coalesce(snapshot -> 'field_versions', '{}'::jsonb) - 'issue_id' - 'project_key')
                    || jsonb_strip_nulls(jsonb_build_object(
                        'external_id', snapshot -> 'field_versions' -> 'issue_id',
                        'group_key', snapshot -> 'field_versions' -> 'project_key')))
        """
    )

    for table, name, _ in _OLD_FKS:
        op.drop_constraint(name, table, type_="foreignkey")
    op.drop_constraint("issues_pkey", "issues", type_="primary")
    op.create_primary_key("issues_pkey", "issues", ["incident_key"])
    op.create_unique_constraint("uq_issues_source_external", "issues", ["source", "external_key"])
    op.create_index("ix_issues_issue_key", "issues", ["issue_key"])
    op.create_index("ix_issues_source", "issues", ["source"])

    # --- dependent tables -------------------------------------------------------------------------
    op.alter_column("analyses", "issue_key", new_column_name="incident_key", type_=sa.String(length=200))
    op.execute("UPDATE analyses SET incident_key = 'jira:' || incident_key")
    op.execute("ALTER INDEX ix_analyses_issue_started RENAME TO ix_analyses_incident_started")
    # Persisted analysis documents reference issues by key; give them the new identity as well.
    op.execute(
        """
        UPDATE analyses SET
            canonical_issue = CASE WHEN canonical_issue ? 'issue_key'
                THEN (canonical_issue - 'issue_key')
                     || jsonb_build_object('incident_key', 'jira:' || (canonical_issue ->> 'issue_key'))
                ELSE canonical_issue END,
            similar_issues = (
                SELECT coalesce(jsonb_agg(
                    CASE WHEN s ? 'issue_key'
                        THEN (s - 'issue_key') || jsonb_build_object(
                            'incident_key', 'jira:' || (s ->> 'issue_key'), 'source', 'jira')
                        ELSE s END), '[]'::jsonb)
                FROM jsonb_array_elements(similar_issues) AS s)
        WHERE similar_issues IS NOT NULL OR canonical_issue IS NOT NULL
        """
    )
    op.alter_column(
        "issue_comments", "issue_key", new_column_name="incident_key", type_=sa.String(length=200)
    )
    op.execute("UPDATE issue_comments SET incident_key = 'jira:' || incident_key")
    op.alter_column(
        "issue_relationships",
        "source_issue_key",
        new_column_name="source_incident_key",
        type_=sa.String(length=200),
    )
    op.alter_column(
        "issue_relationships",
        "target_issue_key",
        new_column_name="target_incident_key",
        type_=sa.String(length=200),
    )
    op.execute(
        "UPDATE issue_relationships SET source_incident_key = 'jira:' || source_incident_key, "
        "target_incident_key = 'jira:' || target_incident_key"
    )
    for table, name, column in _NEW_FKS:
        op.create_foreign_key(name, table, "issues", [column], ["incident_key"], ondelete="CASCADE")

    # --- pull-source checkpoints ------------------------------------------------------------------
    op.create_table(
        "source_checkpoints",
        sa.Column("source_id", sa.String(length=40), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("source_id"),
    )


def downgrade() -> None:
    """Only possible while every record is a Jira record (the old schema cannot represent other sources)."""
    bind = op.get_bind()
    others = bind.execute(sa.text("SELECT count(*) FROM issues WHERE source <> 'jira'")).scalar_one()
    pending = bind.execute(
        sa.text(
            "SELECT count(*) FROM webhook_inbox WHERE source <> 'jira' AND status IN ('PENDING', 'PROCESSING')"
        )
    ).scalar_one()
    if others or pending:
        raise RuntimeError(
            f"cannot downgrade: {others} non-Jira issues / {pending} pending non-Jira inbox events exist"
        )
    op.drop_table("source_checkpoints")

    for table, name, _ in _NEW_FKS:
        op.drop_constraint(name, table, type_="foreignkey")
    op.execute(
        "UPDATE issue_relationships SET source_incident_key = substr(source_incident_key, 6), "
        "target_incident_key = substr(target_incident_key, 6)"
    )
    op.alter_column(
        "issue_relationships",
        "target_incident_key",
        new_column_name="target_issue_key",
        type_=sa.String(length=64),
    )
    op.alter_column(
        "issue_relationships",
        "source_incident_key",
        new_column_name="source_issue_key",
        type_=sa.String(length=64),
    )
    op.execute("UPDATE issue_comments SET incident_key = substr(incident_key, 6)")
    op.alter_column("issue_comments", "incident_key", new_column_name="issue_key", type_=sa.String(length=64))
    op.execute(
        """
        UPDATE analyses SET
            canonical_issue = CASE WHEN canonical_issue ? 'incident_key'
                THEN (canonical_issue - 'incident_key')
                     || jsonb_build_object('issue_key', substr(canonical_issue ->> 'incident_key', 6))
                ELSE canonical_issue END,
            similar_issues = (
                SELECT coalesce(jsonb_agg(
                    CASE WHEN s ? 'incident_key'
                        THEN (s - 'incident_key' - 'source') || jsonb_build_object(
                            'issue_key', substr(s ->> 'incident_key', 6))
                        ELSE s END), '[]'::jsonb)
                FROM jsonb_array_elements(similar_issues) AS s)
        WHERE similar_issues IS NOT NULL OR canonical_issue IS NOT NULL
        """
    )
    op.execute("ALTER INDEX ix_analyses_incident_started RENAME TO ix_analyses_issue_started")
    op.execute("UPDATE analyses SET incident_key = substr(incident_key, 6)")
    op.alter_column("analyses", "incident_key", new_column_name="issue_key", type_=sa.String(length=64))

    op.drop_index("ix_issues_source", table_name="issues")
    op.drop_index("ix_issues_issue_key", table_name="issues")
    op.drop_constraint("uq_issues_source_external", "issues", type_="unique")
    op.drop_constraint("issues_pkey", "issues", type_="primary")
    op.create_primary_key("issues_pkey", "issues", ["issue_key"])
    for table, name, column in _OLD_FKS:
        op.create_foreign_key(name, table, "issues", [column], ["issue_key"], ondelete="CASCADE")
    op.execute(
        """
        UPDATE issues SET snapshot =
            (snapshot - 'source' - 'external_key' - 'external_id' - 'group_key')
            || jsonb_build_object(
                'issue_key', external_key,
                'issue_id', snapshot -> 'external_id',
                'project_key', snapshot -> 'group_key',
                'field_versions',
                    (coalesce(snapshot -> 'field_versions', '{}'::jsonb) - 'external_id' - 'group_key')
                    || jsonb_strip_nulls(jsonb_build_object(
                        'issue_id', snapshot -> 'field_versions' -> 'external_id',
                        'project_key', snapshot -> 'field_versions' -> 'group_key')))
        """
    )
    op.execute("ALTER INDEX ix_issues_group_key RENAME TO ix_issues_project_key")
    op.alter_column("issues", "group_key", new_column_name="project_key", type_=sa.String(length=64))
    op.alter_column("issues", "external_id", new_column_name="issue_id")
    op.alter_column("issues", "issue_key", type_=sa.String(length=64))
    op.drop_column("issues", "incident_key")
    op.drop_column("issues", "external_key")
    op.drop_column("issues", "source")

    for old, new in _LEGACY_EVENT_TYPES.items():
        op.execute(
            sa.text("UPDATE webhook_inbox SET event_type = :old WHERE event_type = :new").bindparams(
                old=old, new=new
            )
        )
    op.drop_index("ix_inbox_source_claim", table_name="webhook_inbox")
    op.drop_index("ix_inbox_incident", table_name="webhook_inbox")
    op.create_index("ix_inbox_issue", "webhook_inbox", ["issue_key", "received_at"])
    op.alter_column("webhook_inbox", "group_key", new_column_name="project_key", type_=sa.String(length=64))
    op.alter_column("webhook_inbox", "issue_key", type_=sa.String(length=64))
    op.drop_column("webhook_inbox", "source")

"""Migration 0003 on existing (Jira-only) data: upgrade backfills incident keys, downgrade restores them."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from branchops.domain.entities.incident import IncidentSnapshot
from branchops.infrastructure.database.session import Database

pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[2]


def alembic_config(db: Database) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = db.engine.url.render_as_string(hide_password=False)
    return cfg


LEGACY_SNAPSHOT = {
    "issue_key": "OPS-1",
    "issue_id": "10001",
    "project_key": "OPS",
    "summary": "EFT hatası",
    "field_versions": {"project_key": "2026-10-14T09:00:00+00:00", "summary": "2026-10-14T09:00:00+00:00"},
}


def seed_legacy(db: Database) -> uuid.UUID:
    analysis_id = uuid.uuid4()
    with db.engine.begin() as c:
        for key in ("OPS-1", "OPS-2"):
            snap = {**LEGACY_SNAPSHOT, "issue_key": key}
            c.execute(
                text(
                    "INSERT INTO issues (issue_key, issue_id, project_key, summary, snapshot, is_deleted) "
                    "VALUES (:k, '10001', 'OPS', 'EFT hatası', CAST(:s AS jsonb), false)"
                ),
                {"k": key, "s": json.dumps(snap)},
            )
        c.execute(
            text(
                "INSERT INTO analyses (id, issue_key, correlation_id, status, input_hash, recurring, "
                "recurring_count, prompt_tokens, completion_tokens, started_at, canonical_issue, similar_issues) "
                "VALUES (:id, 'OPS-1', 'c', 'COMPLETED', 'h', false, 0, 0, 0, now(), "
                "CAST(:canonical AS jsonb), CAST(:similar AS jsonb))"
            ),
            {
                "id": analysis_id,
                "canonical": json.dumps({"issue_key": "OPS-1", "project": "OPS", "fields": {}}),
                "similar": json.dumps([{"issue_key": "OPS-2", "relationship": "RELATED"}]),
            },
        )
        c.execute(
            text("UPDATE issues SET latest_analysis_id = :id WHERE issue_key = 'OPS-1'"), {"id": analysis_id}
        )
        c.execute(
            text(
                "INSERT INTO issue_comments (issue_key, comment_id, body, is_deleted) VALUES ('OPS-1', '7', 'not', false)"
            )
        )
        c.execute(
            text(
                "INSERT INTO issue_relationships (source_issue_key, target_issue_key, relationship_type, text_rank, "
                "signals, evidence) VALUES ('OPS-1', 'OPS-2', 'RELATED', 0.5, '[]', '[]')"
            )
        )
        c.execute(
            text(
                "INSERT INTO webhook_inbox (id, idempotency_key, correlation_id, event_type, raw_event_name, "
                "issue_key, project_key, payload, payload_sha256, payload_redacted, status, attempts, max_attempts) "
                "VALUES (:id, 'sha256:x', 'c', 'ISSUE_CREATED', 'jira:issue_created', 'OPS-1', 'OPS', '{}', 'x', "
                "false, 'PENDING', 0, 5)"
            ),
            {"id": uuid.uuid4()},
        )
    return analysis_id


def test_upgrade_and_downgrade_with_existing_jira_data(db: Database) -> None:
    cfg = alembic_config(db)
    command.downgrade(cfg, "0002")
    try:
        analysis_id = seed_legacy(db)
        command.upgrade(cfg, "0003")
        with db.engine.connect() as c:
            issues = c.execute(
                text(
                    "SELECT incident_key, source, external_key, issue_key, group_key, snapshot FROM issues ORDER BY 1"
                )
            ).all()
            assert [(r.incident_key, r.source, r.external_key, r.issue_key, r.group_key) for r in issues] == [
                ("jira:OPS-1", "jira", "OPS-1", "OPS-1", "OPS"),
                ("jira:OPS-2", "jira", "OPS-2", "OPS-2", "OPS"),
            ]
            snap = IncidentSnapshot.model_validate(issues[0].snapshot)
            assert (
                snap.incident_key == "jira:OPS-1" and snap.external_id == "10001" and snap.group_key == "OPS"
            )
            assert "issue_key" not in issues[0].snapshot
            assert set(snap.field_versions) == {"group_key", "summary"}
            analysis = c.execute(
                text("SELECT incident_key, canonical_issue, similar_issues FROM analyses WHERE id = :id"),
                {"id": analysis_id},
            ).one()
            assert analysis.incident_key == "jira:OPS-1"
            assert analysis.canonical_issue["incident_key"] == "jira:OPS-1"
            assert analysis.similar_issues == [
                {"incident_key": "jira:OPS-2", "source": "jira", "relationship": "RELATED"}
            ]
            assert c.execute(text("SELECT incident_key FROM issue_comments")).scalar_one() == "jira:OPS-1"
            rel = c.execute(
                text("SELECT source_incident_key, target_incident_key FROM issue_relationships")
            ).one()
            assert tuple(rel) == ("jira:OPS-1", "jira:OPS-2")
            inbox = c.execute(
                text("SELECT source, event_type, issue_key, group_key FROM webhook_inbox")
            ).one()
            assert tuple(inbox) == ("jira", "RECORD_CREATED", "OPS-1", "OPS")
        # Foreign keys point at the new primary key (cascade still works).
        with db.engine.begin() as c:
            c.execute(text("DELETE FROM issues WHERE incident_key = 'jira:OPS-2'"))
            assert c.execute(text("SELECT count(*) FROM issue_relationships")).scalar_one() == 0

        command.downgrade(cfg, "0002")
        with db.engine.connect() as c:
            row = c.execute(text("SELECT issue_key, project_key, snapshot FROM issues")).one()
            assert (row.issue_key, row.project_key) == ("OPS-1", "OPS")
            assert row.snapshot["issue_key"] == "OPS-1" and row.snapshot["project_key"] == "OPS"
            assert c.execute(text("SELECT issue_key FROM analyses")).scalar_one() == "OPS-1"
            assert c.execute(text("SELECT event_type FROM webhook_inbox")).scalar_one() == "ISSUE_CREATED"
    finally:
        command.upgrade(cfg, "head")
        with db.engine.begin() as c:
            c.execute(
                text(
                    "TRUNCATE agent_steps, analyses, issue_relationships, issue_comments, issues, webhook_inbox CASCADE"
                )
            )


def test_downgrade_refuses_when_other_sources_exist(db: Database) -> None:
    cfg = alembic_config(db)
    with db.engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO issues (incident_key, source, external_key, issue_key, snapshot, is_deleted) "
                "VALUES ('csv:IMP-1', 'csv', 'IMP-1', 'IMP-1', '{}', false)"
            )
        )
    with pytest.raises(RuntimeError, match="non-Jira"):
        command.downgrade(cfg, "0002")
    with db.engine.connect() as c:  # still at head
        assert c.execute(text("SELECT count(*) FROM source_checkpoints")).scalar_one() == 0

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from branchops.infrastructure.database.session import Database, create_db_engine
from branchops.templates.registry import TemplateRegistry
from tests.helpers import TEMPLATES, make_settings

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
_TABLES = (
    "agent_steps, analyses, issue_relationships, issue_comments, issues, webhook_inbox, "
    "template_versions, audit_log, source_checkpoints, ticketing_outbox, "
    "ownership_changes, routing_rules, screens, applications, teams, attachments, incident_drafts, "
    "incident_app_context, incident_routing, notification_outbox"
)


@pytest.fixture(scope="session")
def registry() -> TemplateRegistry:
    return TemplateRegistry.from_directory(TEMPLATES)


@pytest.fixture(scope="session")
def _migrated_db() -> Iterator[str]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set; skipping PostgreSQL tests")
    from alembic import command
    from alembic.config import Config

    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    engine.dispose()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    command.upgrade(cfg, "head")
    yield TEST_DATABASE_URL


@pytest.fixture
def db(_migrated_db: str) -> Iterator[Database]:
    settings = make_settings(database={"url": _migrated_db})
    database = Database(create_db_engine(settings.database))
    with database.engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE"))
    yield database
    database.engine.dispose()

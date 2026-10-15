"""Migrations 0004-0007 (reporting, ownership, attachments, notifications) upgrade and downgrade cleanly."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import inspect

from branchops.infrastructure.database.session import Database
from tests.integration.test_migration_0003 import alembic_config

pytestmark = pytest.mark.postgres
NEW_TABLES = {
    "ticketing_outbox",
    "teams",
    "applications",
    "screens",
    "routing_rules",
    "ownership_changes",
    "incident_drafts",
    "attachments",
    "incident_app_context",
    "incident_routing",
    "notification_outbox",
}


def test_round_trip(db: Database) -> None:
    cfg = alembic_config(db)
    try:
        command.downgrade(cfg, "0003")
        assert not NEW_TABLES & set(inspect(db.engine).get_table_names())
    finally:
        command.upgrade(cfg, "head")
    assert set(inspect(db.engine).get_table_names()) >= NEW_TABLES

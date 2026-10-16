"""Investigation step on PostgreSQL: parameterised reader queries, persisted evidence-checked results."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select

from branchops.application.commands.ingest_webhook import IngestIncidentEvent
from branchops.application.handlers.process_event import EventProcessor
from branchops.config.settings import Settings
from branchops.container import Container
from branchops.infrastructure.database.models import Analysis
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.persistence.investigation_repository import DatabaseInvestigationReader
from branchops.workers.dispatcher import Dispatcher
from branchops.workers.processor import InboxItemRunner
from tests.helpers import ScriptedProvider, load_sample, make_settings
from tests.integration.test_end_to_end import drain

pytestmark = pytest.mark.postgres


@pytest.fixture
def settings(db: Database) -> Settings:
    return make_settings(
        database={"url": str(db.engine.url.render_as_string(hide_password=False))},
        worker={"concurrency": 1, "batch_size": 1},
        workflow={"investigation": {"enabled": True}},
    )


@pytest.fixture
def container(settings: Settings) -> Iterator[Container]:
    c = Container.build(settings)
    yield c
    c.close()


def test_investigation_runs_and_is_persisted(container: Container, settings: Settings, db: Database) -> None:
    command = IngestIncidentEvent(container.db, settings, container.sources)
    for sample in ("complete_incident", "duplicate_issue"):
        command.execute("jira", load_sample(sample), correlation_id=uuid.uuid4().hex[:16])
    processor = EventProcessor(
        db=container.db,
        settings=settings,
        registry=container.registry,
        workflow=container.build_workflow(ScriptedProvider()),
    )
    drain(
        Dispatcher(
            container.db, InboxItemRunner(container.db, processor, settings.worker, "w"), settings.worker, "w"
        ),
        db,
    )
    with db.session() as s:
        rows = {a.incident_key: a.investigation for a in s.execute(select(Analysis)).scalars()}
    assert rows["jira:BOPS-101"] is None  # nothing in history yet: step skipped
    second = rows["jira:BOPS-104"]
    assert second is not None and second["status"] == "COMPLETED"
    assert [c["tool"] for c in second["tool_calls"]] == ["get_issue_summary"]
    kinds = [f["kind"] for f in second["findings"]]
    assert kinds == ["OBSERVATION", "HYPOTHESIS"]
    assert second["findings"][0]["evidence"][0]["verified"] is True

    reader = DatabaseInvestigationReader(container.db)
    found = reader.search_history("EFT gönderimi zaman aşımı", 5, exclude="jira:BOPS-104")
    assert [i.incident_key for i in found] == ["jira:BOPS-101"]
    summary = reader.issue_summary("jira:BOPS-104")
    assert summary is not None and "jira:BOPS-101" in summary.related
    assert reader.issue_summary("jira:NOPE-1") is None

"""Status history and operational analytics (PostgreSQL): reopen, recurrence after resolution, UNKNOWN."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from branchops.application.commands.ingest_webhook import IngestIncidentEvent
from branchops.application.handlers.process_event import EventProcessor
from branchops.application.queries.operations import operational_intelligence
from branchops.config.settings import Settings
from branchops.container import Container
from branchops.infrastructure.database.models import Issue, IssueStatusEvent
from branchops.infrastructure.database.session import Database
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
    )


@pytest.fixture
def container(settings: Settings) -> Iterator[Container]:
    c = Container.build(settings)
    yield c
    c.close()


def update(key: str, status: str, when: str, *, created: str | None = None) -> dict[str, Any]:
    payload = copy.deepcopy(load_sample("complete_incident"))
    payload["webhookEvent"] = "jira:issue_updated" if created is None else "jira:issue_created"
    payload["issue"]["key"] = key
    payload["issue"]["id"] = key
    fields = payload["issue"]["fields"]
    fields["status"] = {"name": status}
    fields["updated"] = when
    if created is not None:
        fields["created"] = created
    return payload


def process(container: Container, settings: Settings, db: Database, *payloads: dict[str, Any]) -> None:
    command = IngestIncidentEvent(container.db, settings, container.sources)
    processor = EventProcessor(
        db=container.db,
        settings=settings,
        registry=container.registry,
        workflow=container.build_workflow(ScriptedProvider()),
    )
    runner = InboxItemRunner(container.db, processor, settings.worker, "w")
    for payload in payloads:  # one at a time: delivery order matters for this test
        command.execute("jira", payload, correlation_id=uuid.uuid4().hex[:16])
        drain(Dispatcher(container.db, runner, settings.worker, "w"), db)


def test_without_status_history_metrics_are_unknown(
    container: Container, settings: Settings, db: Database
) -> None:
    process(container, settings, db, load_sample("complete_incident"))
    with db.session() as s:
        ops = operational_intelligence(s, days=3650)
    rr = ops["reopen_and_recurrence"]
    assert rr["reopened_records"] == "UNKNOWN" and rr["recurrence_after_resolution"] == "UNKNOWN"
    assert rr["coverage"] == {"records_with_status_history": 1, "records_resolved": 0}
    assert ops["application_trend"] and ops["missing_by_category"]
    assert "iş etkisi göstergesi değildir" in ops["note"]


def test_reopen_and_recurrence_after_resolution(
    container: Container, settings: Settings, db: Database
) -> None:
    process(
        container,
        settings,
        db,
        load_sample("complete_incident"),  # BOPS-101, "Açık"
        update("BOPS-101", "Çözüldü", "2026-10-14T12:00:00.000+0300"),
        update("BOPS-101", "Kapatıldı", "2026-10-14T11:00:00.000+0300"),  # older: rejected by the merge
        update("BOPS-101", "Yeniden Açıldı", "2026-10-14T15:00:00.000+0300"),
        update("BOPS-101", "Çözüldü", "2026-10-14T16:00:00.000+0300"),
        # Same error signature, created after the resolution: a recurrence.
        update("BOPS-150", "Açık", "2026-10-15T09:00:00.000+0300", created="2026-10-15T09:00:00.000+0300"),
    )
    with db.session() as s:
        events = [
            (e.from_status, e.to_status, e.resolved)
            for e in s.execute(
                select(IssueStatusEvent)
                .where(IssueStatusEvent.incident_key == "jira:BOPS-101")
                .order_by(IssueStatusEvent.id)
            ).scalars()
        ]
        issue = s.get(Issue, "jira:BOPS-101")
        assert issue is not None and issue.resolved_at is not None
        ops = operational_intelligence(s, days=3650)
    assert events == [
        (None, "Açık", False),
        ("Açık", "Çözüldü", True),
        ("Çözüldü", "Yeniden Açıldı", False),
        ("Yeniden Açıldı", "Çözüldü", True),
    ]
    rr = ops["reopen_and_recurrence"]
    assert rr["reopened_records"] == 1 and rr["reopen_rate"] == 1.0
    assert [r["new_records_within_window"] for r in rr["recurrence_after_resolution"]] == [1]

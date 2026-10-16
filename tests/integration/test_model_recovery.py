"""Model outage → degraded analysis → bounded, idempotent recovery once the model is back (PostgreSQL)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select, text

from branchops.application.commands.ingest_webhook import IngestIncidentEvent
from branchops.application.handlers.process_event import EventProcessor
from branchops.application.interfaces.model_provider import (
    CompletionRequest,
    CompletionResult,
    ModelHealth,
    ModelUnavailableError,
)
from branchops.config.settings import ModelCapabilities, Settings
from branchops.container import Container
from branchops.domain.entities.analysis import AnalysisResult
from branchops.domain.enums import AnalysisStatus
from branchops.infrastructure.database.models import Analysis, AnalysisRecovery, Issue
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.llm.mock_provider import MockModelProvider
from branchops.infrastructure.persistence.recovery_repository import RecoveryRepository
from branchops.workers.dispatcher import Dispatcher
from branchops.workers.processor import InboxItemRunner
from branchops.workers.recovery import ModelHealthProbe, RecoveryScheduler
from tests.helpers import load_sample, make_settings
from tests.integration.test_end_to_end import drain

pytestmark = pytest.mark.postgres


class SwitchableModel:
    """Mock model that can be taken down (every call and the health probe fail while ``down``)."""

    def __init__(self) -> None:
        self.down = False
        self._mock = MockModelProvider()

    @property
    def model_id(self) -> str:
        return "switchable"

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(structured_outputs=True, json_mode=True)

    def health(self) -> ModelHealth:
        return ModelHealth(not self.down, self.model_id, "down" if self.down else "ok")

    def complete(self, request: CompletionRequest, *, timeout: float | None = None) -> CompletionResult:
        if self.down:
            raise ModelUnavailableError("connection refused")
        return self._mock.complete(request)


@pytest.fixture
def settings(db: Database) -> Settings:
    return make_settings(
        database={"url": str(db.engine.url.render_as_string(hide_password=False))},
        worker={"concurrency": 1, "batch_size": 1, "max_attempts": 2},
        recovery={"base_delay_seconds": 1, "max_attempts": 3},
    )


@pytest.fixture
def container(settings: Settings) -> Iterator[Container]:
    c = Container.build(settings)
    yield c
    c.close()


def run_worker(container: Container, settings: Settings, model: SwitchableModel, db: Database) -> None:
    processor = EventProcessor(
        db=container.db,
        settings=settings,
        registry=container.registry,
        workflow=container.build_workflow(model),
    )
    runner = InboxItemRunner(container.db, processor, settings.worker, "w")
    drain(Dispatcher(container.db, runner, settings.worker, "w"), db)


def ingest(container: Container, settings: Settings, sample: str, **changes: Any) -> None:
    payload = load_sample(sample)
    payload["issue"]["fields"].update(changes)
    IngestIncidentEvent(container.db, settings, container.sources).execute(
        "jira", payload, correlation_id=uuid.uuid4().hex[:16]
    )


def recovery_row(db: Database, key: str = "jira:BOPS-101") -> AnalysisRecovery:
    with db.session() as s:
        row = s.get(AnalysisRecovery, key)
        assert row is not None
        s.expunge(row)
        return row


def make_due(db: Database) -> None:
    with db.session() as s:
        s.execute(text("UPDATE analysis_recovery SET next_attempt_at = now() - interval '1 second'"))


def latest(db: Database, key: str = "jira:BOPS-101") -> Analysis:
    with db.session() as s:
        issue = s.get(Issue, key)
        assert issue is not None and issue.latest_analysis_id is not None
        analysis = s.get(Analysis, issue.latest_analysis_id)
        assert analysis is not None
        s.expunge(analysis)
        return analysis


def test_outage_is_recovered_when_the_model_returns(
    container: Container, settings: Settings, db: Database
) -> None:
    model = SwitchableModel()
    scheduler = RecoveryScheduler(container.db, settings, ModelHealthProbe(model, ttl_seconds=0))
    ingest(container, settings, "complete_incident")
    model.down = True
    run_worker(container, settings, model, db)  # retries, then the last attempt stores a degraded result

    degraded = latest(db)
    assert degraded.status == AnalysisStatus.NEEDS_REVIEW.value and degraded.recoverable
    assert recovery_row(db).state == "RECOVERY_PENDING"

    # Model still down: nothing is enqueued and no attempt is consumed.
    make_due(db)
    summary = scheduler.tick()
    assert "enqueued" not in summary and "model unavailable" in summary["skipped"]
    assert recovery_row(db).attempts == 0

    model.down = False
    summary = scheduler.tick()
    assert summary["enqueued"] == 1 and recovery_row(db).state == "RUNNING"
    assert scheduler.tick().get("enqueued", 0) == 0  # no duplicate active execution
    run_worker(container, settings, model, db)

    recovered = latest(db)
    assert recovered.id != degraded.id
    assert recovered.status == AnalysisStatus.COMPLETED.value and not recovered.recoverable
    row = recovery_row(db)
    assert row.state == "COMPLETED" and row.reason == "recovered" and row.analysis_id == recovered.id
    with db.session() as s:  # history preserved
        assert (
            s.execute(text("SELECT count(*) FROM analyses WHERE incident_key='jira:BOPS-101'")).scalar_one()
            == 2
        )


def test_redelivery_reanalyses_a_provisional_result(
    container: Container, settings: Settings, db: Database
) -> None:
    """The input hash of a degraded result must not suppress a later redelivery of unchanged content."""
    model = SwitchableModel()
    ingest(container, settings, "complete_incident")
    model.down = True
    run_worker(container, settings, model, db)
    model.down = False
    IngestIncidentEvent(container.db, settings, container.sources).execute(
        "jira",
        load_sample("complete_incident"),
        correlation_id="redelivery",
        headers={"X-Atlassian-Webhook-Identifier": "second-delivery"},
    )
    run_worker(container, settings, model, db)
    assert latest(db).status == AnalysisStatus.COMPLETED.value
    assert recovery_row(db).state == "COMPLETED"


def test_provisional_result_never_replaces_a_complete_one(
    container: Container, settings: Settings, db: Database
) -> None:
    model = SwitchableModel()
    ingest(container, settings, "complete_incident")
    run_worker(container, settings, model, db)
    good = latest(db)
    assert good.status == AnalysisStatus.COMPLETED.value

    model.down = True
    ingest(
        container,
        settings,
        "complete_incident",
        description="Güncel açıklama: EFT-4012 hata devam ediyor.",
        updated="2026-10-14T12:00:00.000+0300",
    )
    run_worker(container, settings, model, db)
    with db.session() as s:
        statuses = [a.status for a in s.execute(select(Analysis).order_by(Analysis.started_at)).scalars()]
        issue = s.get(Issue, "jira:BOPS-101")
        assert issue is not None
        category = issue.category
    assert statuses[-1] == AnalysisStatus.NEEDS_REVIEW.value  # stored for history ...
    assert latest(db).id == good.id  # ... but the complete analysis stays current
    assert category == "TRANSACTION"  # denormalised columns were not nulled
    assert recovery_row(db).state == "RECOVERY_PENDING"


def _result(key: str, *, recoverable: bool, started: datetime) -> AnalysisResult:
    return AnalysisResult(
        analysis_id=str(uuid.uuid4()),
        incident_key=key,
        correlation_id="c",
        input_hash="h",
        prompt_version="p",
        started_at=started,
        status=AnalysisStatus.NEEDS_REVIEW if recoverable else AnalysisStatus.COMPLETED,
        recoverable=recoverable,
        errors=["classify: connection refused"] if recoverable else [],
    )


def test_recovery_is_bounded(settings: Settings, db: Database) -> None:
    repo = RecoveryRepository(settings.recovery)
    now = datetime.now(UTC)
    with db.session() as s:
        assert (
            repo.record_analysis(s, _result("jira:X-1", recoverable=True, started=now)) == "RECOVERY_PENDING"
        )
    for attempt in range(1, settings.recovery.max_attempts + 1):
        with db.session() as s:
            repo.mark_running(s, "jira:X-1", uuid.uuid4())
            state = repo.record_analysis(s, _result("jira:X-1", recoverable=True, started=now))
        expected = "FAILED_PERMANENT" if attempt == settings.recovery.max_attempts else "RETRY_WAIT"
        assert state == expected
    row = recovery_row(db, "jira:X-1")
    assert row.attempts == settings.recovery.max_attempts and row.reason == "retry limit reached"
    # A later degraded analysis (new content) starts a new cycle; a complete one ends it.
    with db.session() as s:
        assert (
            repo.record_analysis(s, _result("jira:X-1", recoverable=True, started=now)) == "RECOVERY_PENDING"
        )
        assert repo.record_analysis(s, _result("jira:X-1", recoverable=False, started=now)) == "COMPLETED"


def test_dead_lettered_recovery_event_is_retried_later(settings: Settings, db: Database) -> None:
    repo = RecoveryRepository(settings.recovery)
    with db.session() as s:
        repo.record_analysis(s, _result("jira:X-2", recoverable=True, started=datetime.now(UTC)))
        repo.mark_running(s, "jira:X-2", uuid.uuid4())  # event id that does not exist (purged / lost)
    with db.session() as s:
        assert repo.resolve_running(s) == {"retry": 1, "completed": 0}
    row = recovery_row(db, "jira:X-2")
    assert row.state == "RETRY_WAIT" and row.attempts == 1 and row.next_attempt_at > datetime.now(UTC)


def test_stale_running_analysis_is_closed(container: Container, settings: Settings, db: Database) -> None:
    ingest(container, settings, "complete_incident")
    run_worker(container, settings, SwitchableModel(), db)
    with db.session() as s:  # simulate a worker that died mid-run two hours ago
        s.execute(text("UPDATE analyses SET status='RUNNING', heartbeat_at = now() - interval '2 hours'"))
    assert RecoveryScheduler(container.db, settings, None).close_stale_runs() == 1
    with db.session() as s:
        assert s.execute(text("SELECT status FROM analyses")).scalar_one() == "FAILED"

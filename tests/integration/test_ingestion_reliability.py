"""Ingestion and inbox reliability (PostgreSQL): authentication, replay protection, poison messages, retry
exhaustion, lease crash recovery, per-issue exclusivity, graceful shutdown, redaction and retention."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from branchops.api.app import create_app
from branchops.application.handlers.process_event import EventProcessor, ProcessOutcome
from branchops.config.settings import Settings
from branchops.container import Container
from branchops.domain.enums import InboxStatus
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.persistence.inbox_repository import InboxItem, InboxRepository
from branchops.infrastructure.security.webhook_auth import sign_body
from branchops.workers.dispatcher import Dispatcher
from branchops.workers.processor import InboxItemRunner
from tests.helpers import ScriptedProvider, load_sample, make_settings
from tests.integration.test_end_to_end import drain, inbox_rows

pytestmark = pytest.mark.postgres
SECRET = "reliability-secret"
AGE_SQL = (
    "UPDATE webhook_inbox SET status='{status}', processed_at = now() - interval '40 days' WHERE id = :i"
)
PII = ("10000000146", "4111 1111 1111 1111", "0532 123 45 67")


@pytest.fixture
def settings(db: Database, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("JIRA_WEBHOOK_SECRETS", SECRET)
    return make_settings(
        database={"url": str(db.engine.url.render_as_string(hide_password=False))},
        worker={"concurrency": 1, "batch_size": 1, "max_attempts": 3},
    )


@pytest.fixture
def container(settings: Settings) -> Iterator[Container]:
    c = Container.build(settings)
    yield c
    c.close()


@pytest.fixture
def client(settings: Settings, container: Container) -> Iterator[TestClient]:
    with TestClient(create_app(settings, container)) as c:
        yield c


def post(
    client: TestClient, payload: Any, *, sign: bool = True, headers: dict[str, str] | None = None
) -> Any:
    body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json", **(headers or {})}
    if sign:
        h["X-Hub-Signature"] = sign_body(body, SECRET)
    return client.post("/api/ingest/jira", content=body, headers=h)


def inbox_count(db: Database) -> int:
    with db.session() as s:
        return int(s.execute(text("SELECT count(*) FROM webhook_inbox")).scalar_one())


def runner_for(container: Container, settings: Settings, processor: Any = None) -> InboxItemRunner:
    processor = processor or EventProcessor(
        db=container.db,
        settings=settings,
        registry=container.registry,
        workflow=container.build_workflow(ScriptedProvider()),
    )
    return InboxItemRunner(container.db, processor, settings.worker, "w")


class _Exploding(EventProcessor):
    """Fails with an unexpected error whose message carries incident text (as DB driver errors do)."""

    def _process_locked(self, item: InboxItem, incident_key: str) -> ProcessOutcome:
        raise RuntimeError(
            "insert failed: TCKN 10000000146 kart 4111 1111 1111 1111"
            "\n[SQL: INSERT ...]\n[parameters: {'x': 1}]"
        )


def _claim(db: Database, owner: str = "w") -> InboxItem:
    with db.session() as s:
        s.execute(text("UPDATE webhook_inbox SET available_at = now() WHERE status = 'PENDING'"))
        items = InboxRepository().claim(s, owner, limit=1, lease_seconds=60)
    assert items, "nothing to claim"
    return items[0]


class TestAuthentication:
    def test_rejected_requests_persist_nothing(self, client: TestClient, db: Database) -> None:
        payload = load_sample("pii_incident")
        assert post(client, payload, sign=False).status_code == 401
        assert (
            post(client, payload, sign=False, headers={"X-Hub-Signature": "sha256=" + "0" * 64}).status_code
            == 401
        )
        assert inbox_count(db) == 0
        with db.session() as s:
            assert s.execute(text("SELECT count(*) FROM audit_log")).scalar_one() == 0

    def test_signed_delivery_replayed_with_a_new_delivery_id_is_a_duplicate(
        self, client: TestClient, db: Database
    ) -> None:
        """ING-2: the delivery-id header is not signed, so it must not decide idempotency of signed bodies."""
        payload = load_sample("complete_incident")
        first = post(client, payload, headers={"X-Atlassian-Webhook-Identifier": "delivery-1"})
        replayed = post(client, payload, headers={"X-Atlassian-Webhook-Identifier": "attacker-chosen"})
        assert first.status_code == 202 and replayed.json()["status"] == "duplicate"
        assert inbox_count(db) == 1

    def test_malformed_but_authentic_payloads_are_400_not_500(self, client: TestClient, db: Database) -> None:
        for depth in (5_000, 200_000):  # parsed (ignored event) or refused, never a server error
            nested = b'{"a":' * depth + b"1" + b"}" * depth
            assert post(client, nested).status_code in (200, 400, 413)
        nul = json.dumps({"webhookEvent": "jira:issue_created", "x": "a\u0000b"}).encode()
        assert post(client, nul).status_code == 400
        # ING-1: a non-object changelog is treated as absent, the event is accepted.
        payload = {**load_sample("partial_update"), "changelog": ["not", "an", "object"]}
        assert post(client, payload).status_code == 202
        assert inbox_count(db) == 1


class TestReplayWindow:
    def test_signed_events_older_than_the_window_are_ignored(
        self, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A captured delivery cannot be re-submitted once its idempotency key may have been purged."""
        monkeypatch.setenv("JIRA_WEBHOOK_SECRETS", SECRET)
        settings = make_settings(
            database={"url": str(db.engine.url.render_as_string(hide_password=False))},
            webhook={"max_event_age_days": 7},
            retention={"inbox_days": 30},
        )
        container = Container.build(settings)
        try:
            with TestClient(create_app(settings, container)) as c:
                old = {**load_sample("complete_incident"), "timestamp": 1_600_000_000_000}  # 2020
                r = post(c, old)
                assert r.status_code == 200 and r.json()["status"] == "ignored"
                assert "replay protection" in r.json()["reason"]
                # Fail-closed: no time asserted by the body (receive time is never used).
                undated = load_sample("complete_incident")
                del undated["timestamp"]
                del undated["issue"]["fields"]["updated"]
                r = post(c, undated)
                assert r.status_code == 200 and r.json()["status"] == "ignored"
                fresh = {**load_sample("complete_incident"), "timestamp": int(time.time() * 1000)}
                assert post(c, fresh).status_code == 202
        finally:
            container.close()
        assert inbox_count(db) == 1
        with pytest.raises(ValueError, match="max_event_age_days"):
            make_settings(webhook={"max_event_age_days": 30}, retention={"inbox_days": 30})


class TestPoisonAndRetries:
    def test_poison_message_is_dead_lettered_immediately_and_redacted(
        self, client: TestClient, container: Container, settings: Settings, db: Database
    ) -> None:
        assert post(client, load_sample("pii_incident")).status_code == 202
        with db.session() as s:  # corrupt the stored payload: key no longer matches the inbox row
            s.execute(
                text("UPDATE webhook_inbox SET payload = jsonb_set(payload, '{issue,key}', '\"OTHER-1\"')")
            )
        status = runner_for(container, settings).run(_claim(db))
        assert status is InboxStatus.DEAD_LETTER
        row = inbox_rows(db)[0]
        assert row.attempts == 1 and row.processed_at is not None and row.payload_redacted
        assert not any(p in json.dumps(row.payload, ensure_ascii=False) for p in PII)

    def test_retry_exhaustion_through_the_runner(
        self, client: TestClient, container: Container, settings: Settings, db: Database
    ) -> None:
        assert post(client, load_sample("pii_incident")).status_code == 202
        processor = _Exploding(
            db=container.db,
            settings=settings,
            registry=container.registry,
            workflow=None,  # type: ignore[arg-type]
        )
        runner = runner_for(container, settings, processor)
        statuses = [runner.run(_claim(db)) for _ in range(settings.worker.max_attempts)]
        assert statuses == [InboxStatus.PENDING, InboxStatus.PENDING, InboxStatus.DEAD_LETTER]
        row = inbox_rows(db)[0]
        assert row.status == "DEAD_LETTER" and row.processed_at is not None
        # INB-4: stored error text is masked and the SQL parameter dump is cut.
        assert "10000000146" not in row.last_error and "[parameters:" not in row.last_error
        assert "RuntimeError" in row.last_error
        # INB-1: the dead letter keeps only the masked payload, and can still be replayed.
        assert row.payload_redacted and not any(p in json.dumps(row.payload, ensure_ascii=False) for p in PII)
        with db.session() as s:
            assert InboxRepository().replay(s, row.id)
        replayed = inbox_rows(db)[0]
        assert replayed.status == "PENDING" and replayed.attempts == 0 and replayed.outcome is None
        assert replayed.last_error  # kept for investigation


class TestLeases:
    def test_reaper_dead_letters_at_max_attempts_and_redacts(
        self, client: TestClient, container: Container, settings: Settings, db: Database
    ) -> None:
        assert post(client, load_sample("pii_incident")).status_code == 202
        with db.session() as s:
            s.execute(text("UPDATE webhook_inbox SET attempts = max_attempts - 1"))
        _claim(db, "crashed")  # last attempt, then the worker "crashes"
        with db.session() as s:
            s.execute(text("UPDATE webhook_inbox SET lease_expires_at = now() - interval '1 second'"))
        dispatcher = Dispatcher(container.db, runner_for(container, settings), settings.worker, "healthy")
        dispatcher.run_once()
        row = inbox_rows(db)[0]
        assert row.status == "DEAD_LETTER" and row.processed_at is not None
        assert "lease expired" in row.last_error
        assert row.payload_redacted and not any(p in json.dumps(row.payload, ensure_ascii=False) for p in PII)

    def test_crashed_worker_event_is_completed_by_another_dispatcher(
        self, client: TestClient, container: Container, settings: Settings, db: Database
    ) -> None:
        assert post(client, load_sample("complete_incident")).status_code == 202
        _claim(db, "crashed")
        with db.session() as s:
            s.execute(text("UPDATE webhook_inbox SET lease_expires_at = now() - interval '1 second'"))
        drain(Dispatcher(container.db, runner_for(container, settings), settings.worker, "healthy"), db)
        row = inbox_rows(db)[0]
        assert row.status == "COMPLETED" and row.attempts == 2

    def test_issue_locked_by_another_worker_is_released_without_consuming_an_attempt(
        self, client: TestClient, container: Container, settings: Settings, db: Database
    ) -> None:
        assert post(client, load_sample("complete_incident")).status_code == 202
        with db.engine.connect() as other_worker:
            lock_id = other_worker.execute(select(func.hashtextextended("jira:BOPS-101", 0))).scalar_one()
            assert other_worker.execute(select(func.pg_try_advisory_lock(lock_id))).scalar_one()
            other_worker.commit()
            status = runner_for(container, settings).run(_claim(db))
            other_worker.execute(select(func.pg_advisory_unlock(lock_id)))
            other_worker.commit()
        assert status is InboxStatus.PENDING
        row = inbox_rows(db)[0]
        assert row.status == "PENDING" and row.attempts == 0

    def test_graceful_shutdown_hands_unfinished_events_back(
        self, client: TestClient, container: Container, settings: Settings, db: Database
    ) -> None:
        assert post(client, load_sample("complete_incident")).status_code == 202
        started, release = threading.Event(), threading.Event()

        class _Slow(EventProcessor):
            def _process_locked(self, item: InboxItem, incident_key: str) -> ProcessOutcome:
                started.set()
                release.wait(10)
                return ProcessOutcome(InboxStatus.COMPLETED, "late")

        runner = runner_for(
            container,
            settings,
            _Slow(db=container.db, settings=settings, registry=container.registry, workflow=None),  # type: ignore[arg-type]
        )
        dispatcher = Dispatcher(container.db, runner, settings.worker, "stopping")
        assert dispatcher.run_once() == 1
        assert started.wait(5)
        assert dispatcher.drain(grace_seconds=0.2) == 1
        row = inbox_rows(db)[0]
        assert row.status == "PENDING" and row.attempts == 0 and row.lease_owner is None
        release.set()  # the abandoned thread finishes late: its result must not be recorded
        dispatcher._pool.shutdown(wait=True)
        assert inbox_rows(db)[0].status == "PENDING"


class TestRetention:
    def test_finished_events_and_checkpoints_are_purged(self, client: TestClient, db: Database) -> None:
        for name in ("complete_incident", "duplicate_issue", "incomplete_incident"):
            assert post(client, load_sample(name)).status_code == 202
        with db.session() as s:
            ids = [r[0] for r in s.execute(text("SELECT id FROM webhook_inbox ORDER BY received_at")).all()]
            s.execute(
                text(AGE_SQL.format(status="COMPLETED")),
                {"i": ids[0]},
            )
            s.execute(
                text(AGE_SQL.format(status="DEAD_LETTER")),
                {"i": ids[1]},
            )
            deleted = InboxRepository().purge_finished(s, completed_days=30, dead_letter_days=90)
        assert deleted == 1
        with db.session() as s:
            remaining = {r[0] for r in s.execute(text("SELECT id FROM webhook_inbox")).all()}
        assert remaining == {ids[1], ids[2]}  # dead letter kept longer; pending untouched
        with db.session() as s:
            assert InboxRepository().purge_finished(s, completed_days=30, dead_letter_days=30) == 1
        assert uuid.UUID(str(ids[2])) in {uuid.UUID(str(i)) for i in remaining}

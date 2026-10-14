from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import text

from branchops.application.commands.ingest_webhook import IngestJiraWebhook, IngestOutcome
from branchops.domain.enums import InboxStatus
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.jira.adapter import JiraWebhookAdapter
from branchops.infrastructure.persistence.inbox_repository import InboxRepository
from tests.helpers import load_sample, make_settings

pytestmark = pytest.mark.postgres
SETTINGS = make_settings()


def ingest(db: Database, sample: str, delivery_id: str | None = None) -> tuple[IngestOutcome, str | None]:
    cmd = IngestJiraWebhook(db, SETTINGS, JiraWebhookAdapter(SETTINGS.jira.field_mapping))
    result = cmd.execute(load_sample(sample), correlation_id=uuid.uuid4().hex, delivery_id=delivery_id)
    return result.outcome, result.event_id


def test_duplicate_webhook_is_idempotent(db: Database) -> None:
    first = ingest(db, "complete_incident")
    second = ingest(db, "complete_incident")
    assert first[0] is IngestOutcome.ACCEPTED
    assert second == (IngestOutcome.DUPLICATE, first[1])
    third = ingest(db, "complete_incident", delivery_id="abc")
    fourth = ingest(db, "complete_incident", delivery_id="abc")
    assert third[0] is IngestOutcome.ACCEPTED
    assert fourth[0] is IngestOutcome.DUPLICATE
    with db.session() as s:
        assert s.execute(text("SELECT count(*) FROM webhook_inbox")).scalar_one() == 2


def test_project_allowlist_and_unsupported(db: Database) -> None:
    restricted = make_settings(jira={"allowed_projects": ["OTHER"]})
    cmd = IngestJiraWebhook(db, restricted, JiraWebhookAdapter())
    assert (
        cmd.execute(load_sample("complete_incident"), correlation_id="c" * 8).outcome is IngestOutcome.IGNORED
    )
    cmd = IngestJiraWebhook(db, SETTINGS, JiraWebhookAdapter())
    assert (
        cmd.execute({"webhookEvent": "board_created"}, correlation_id="c" * 8).outcome
        is IngestOutcome.IGNORED
    )


def test_concurrent_claims_never_overlap(db: Database) -> None:
    repo = InboxRepository()
    for name in (
        "complete_incident",
        "incomplete_incident",
        "generic_description",
        "duplicate_issue",
        "related_different_issue",
        "missing_branch_code",
        "missing_application",
        "prompt_injection",
    ):
        ingest(db, name)
    claimed: list[list[uuid.UUID]] = []
    barrier = threading.Barrier(4)

    def worker(i: int) -> None:
        barrier.wait()
        with db.session() as s:
            items = repo.claim(s, f"w{i}", limit=3, lease_seconds=60)
        claimed.append([it.id for it in items])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    all_ids = [i for batch in claimed for i in batch]
    assert len(all_ids) == len(set(all_ids)) == 8


def test_same_issue_not_claimed_while_processing(db: Database) -> None:
    repo = InboxRepository()
    ingest(db, "incomplete_incident")
    ingest(db, "partial_update")
    with db.session() as s:
        first = repo.claim(s, "w1", limit=10, lease_seconds=60)  # one event per issue per claim
    with db.session() as s:
        second = repo.claim(s, "w2", limit=10, lease_seconds=60)
    assert len(first) == 1
    assert second == []  # the other event of BOPS-102 waits until the first completes
    with db.session() as s:
        repo.complete(s, first[0].id, first[0].lease_owner, outcome="ok")
    with db.session() as s:
        assert len(repo.claim(s, "w2", limit=10, lease_seconds=60)) == 1


def test_retry_backoff_and_dead_letter(db: Database) -> None:
    repo = InboxRepository()
    _, event_id = ingest(db, "complete_incident")
    for attempt in range(1, 4):
        with db.session() as s:
            s.execute(text("UPDATE webhook_inbox SET available_at = now()"))
            items = repo.claim(s, "w", limit=1, lease_seconds=60)
        assert items[0].attempts == attempt
        retry = 5.0 if attempt < items[0].max_attempts else None
        with db.session() as s:
            status = repo.fail(s, items[0].id, items[0].lease_owner, error="boom", retry_in_seconds=retry)
        if retry is not None:
            assert status is InboxStatus.PENDING
            with db.session() as s:
                assert repo.claim(s, "w", limit=1, lease_seconds=60) == []  # backoff in effect
    assert status is InboxStatus.DEAD_LETTER
    with db.session() as s:
        assert repo.replay(s, uuid.UUID(event_id))  # type: ignore[arg-type]
        assert not repo.replay(s, uuid.UUID(event_id))  # type: ignore[arg-type]
        assert repo.depth_by_status(s) == {"PENDING": 1}


def test_expired_lease_recovered_after_crash(db: Database) -> None:
    repo = InboxRepository()
    ingest(db, "complete_incident")
    with db.session() as s:
        items = repo.claim(s, "crashed-worker", limit=1, lease_seconds=60)
        s.execute(text("UPDATE webhook_inbox SET lease_expires_at = now() - interval '1 second'"))
    with db.session() as s:
        assert repo.reap_expired_leases(s) == 1
        again = repo.claim(s, "healthy-worker", limit=1, lease_seconds=60)
    assert again[0].id == items[0].id
    assert again[0].attempts == 2
    with db.session() as s:
        # The crashed worker can no longer complete the event it lost, even from the same host/pid prefix.
        assert items[0].lease_owner != again[0].lease_owner
        assert not repo.complete(s, items[0].id, items[0].lease_owner, outcome="late")
        assert repo.complete(s, items[0].id, again[0].lease_owner, outcome="ok")
        assert not repo.complete(s, items[0].id, again[0].lease_owner, outcome="twice")


def test_lease_renewal(db: Database) -> None:
    repo = InboxRepository()
    ingest(db, "complete_incident")
    with db.session() as s:
        item = repo.claim(s, "w", limit=1, lease_seconds=30)[0]
    with db.session() as s:
        assert repo.renew_lease(s, item.id, item.lease_owner, 300)
        assert not repo.renew_lease(s, item.id, "other", 300)
        remaining = s.execute(
            text("SELECT extract(epoch FROM lease_expires_at - now()) FROM webhook_inbox")
        ).scalar_one()
    assert remaining > 200

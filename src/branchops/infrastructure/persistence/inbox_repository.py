"""Durable PostgreSQL inbox with leases, retries, dead-lettering and crash recovery."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from branchops.domain.entities.incident import incident_key
from branchops.domain.enums import InboxStatus
from branchops.infrastructure.database.models import WebhookInbox


@dataclass(frozen=True)
class InboxItem:
    id: uuid.UUID
    correlation_id: str
    event_type: str
    raw_event_name: str
    # The source's record key (``external_key``).
    issue_key: str | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    received_at: datetime
    lease_owner: str
    source: str = "jira"

    @property
    def is_last_attempt(self) -> bool:
        return self.attempts >= self.max_attempts

    @property
    def incident_key(self) -> str | None:
        return incident_key(self.source, self.issue_key) if self.issue_key else None


@dataclass(frozen=True)
class NewInboxEvent:
    idempotency_key: str
    correlation_id: str
    event_type: str
    raw_event_name: str
    issue_key: str | None
    group_key: str | None
    event_timestamp: datetime | None
    payload: dict[str, Any]
    payload_sha256: str
    max_attempts: int
    source: str = "jira"


_CLAIM_SQL = text(
    """
    WITH candidates AS (
        SELECT i.id
        FROM webhook_inbox i
        WHERE i.status = 'PENDING'
          AND i.available_at <= now()
          AND NOT EXISTS (
              SELECT 1 FROM webhook_inbox p
              WHERE p.source = i.source AND p.issue_key = i.issue_key
                AND p.status = 'PROCESSING'
                AND p.lease_expires_at > now()
          )
          -- one event per incident per claim: only the oldest pending event of each incident is eligible
          AND NOT EXISTS (
              SELECT 1 FROM webhook_inbox o
              WHERE o.source = i.source AND o.issue_key = i.issue_key
                AND o.status = 'PENDING'
                AND o.id <> i.id
                AND (o.received_at, o.id) < (i.received_at, i.id)
          )
        ORDER BY i.available_at, i.received_at
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE webhook_inbox w
    SET status = 'PROCESSING',
        attempts = w.attempts + 1,
        lease_owner = :owner,
        lease_expires_at = now() + make_interval(secs => :lease)
    FROM candidates c
    WHERE w.id = c.id
    RETURNING w.id, w.correlation_id, w.event_type, w.raw_event_name, w.source, w.issue_key, w.payload,
              w.attempts, w.max_attempts, w.received_at, w.lease_owner
    """
)

_REAP_SQL = text(
    """
    UPDATE webhook_inbox
    SET status = CASE WHEN attempts >= max_attempts THEN 'DEAD_LETTER' ELSE 'PENDING' END,
        last_error = coalesce(last_error || E'\n', '') || 'lease expired (worker crash or timeout)',
        lease_owner = NULL,
        lease_expires_at = NULL,
        available_at = now()
    WHERE status = 'PROCESSING' AND lease_expires_at < now()
    RETURNING id
    """
)


def _seconds(value: float) -> Any:
    return func.make_interval(0, 0, 0, 0, 0, 0, float(value))


class InboxRepository:
    def insert(self, session: Session, event: NewInboxEvent) -> tuple[uuid.UUID | None, bool]:
        """Insert idempotently. Returns ``(id, created)``; ``created`` is False for duplicates."""
        stmt = (
            insert(WebhookInbox)
            .values(
                id=uuid.uuid4(),
                idempotency_key=event.idempotency_key,
                correlation_id=event.correlation_id,
                event_type=event.event_type,
                raw_event_name=event.raw_event_name,
                source=event.source,
                issue_key=event.issue_key,
                group_key=event.group_key,
                event_timestamp=event.event_timestamp,
                payload=event.payload,
                payload_sha256=event.payload_sha256,
                status=InboxStatus.PENDING.value,
                attempts=0,
                max_attempts=event.max_attempts,
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(WebhookInbox.id)
        )
        new_id = session.execute(stmt).scalar_one_or_none()
        if new_id is not None:
            return new_id, True
        existing = session.execute(
            select(WebhookInbox.id).where(WebhookInbox.idempotency_key == event.idempotency_key)
        ).scalar_one_or_none()
        return existing, False

    def claim(self, session: Session, owner: str, limit: int, lease_seconds: int) -> list[InboxItem]:
        """Claim up to ``limit`` events. Each claim gets a unique lease token (``owner:<uuid>``) so a
        stale run can never complete or fail an event that was re-claimed in the meantime."""
        token = f"{owner}:{uuid.uuid4().hex[:12]}"
        rows = session.execute(_CLAIM_SQL, {"owner": token, "limit": limit, "lease": lease_seconds}).all()
        return [
            InboxItem(
                id=r.id,
                correlation_id=r.correlation_id,
                event_type=r.event_type,
                raw_event_name=r.raw_event_name,
                source=r.source,
                issue_key=r.issue_key,
                payload=r.payload,
                attempts=r.attempts,
                max_attempts=r.max_attempts,
                received_at=r.received_at,
                lease_owner=r.lease_owner,
            )
            for r in sorted(rows, key=lambda r: r.received_at)
        ]

    def renew_lease(self, session: Session, event_id: uuid.UUID, owner: str, lease_seconds: int) -> bool:
        result = session.execute(
            update(WebhookInbox)
            .where(
                WebhookInbox.id == event_id,
                WebhookInbox.lease_owner == owner,
                WebhookInbox.status == InboxStatus.PROCESSING.value,
            )
            .values(lease_expires_at=func.now() + _seconds(lease_seconds))
            .execution_options(synchronize_session=False)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    def complete(
        self,
        session: Session,
        event_id: uuid.UUID,
        owner: str,
        *,
        status: InboxStatus = InboxStatus.COMPLETED,
        outcome: str,
        redacted_payload: dict[str, Any] | None = None,
    ) -> bool:
        values: dict[str, Any] = {
            "status": status.value,
            "outcome": outcome[:200],
            "processed_at": func.now(),
            "lease_owner": None,
            "lease_expires_at": None,
        }
        if redacted_payload is not None:
            values["payload"] = redacted_payload
            values["payload_redacted"] = True
        result = session.execute(
            update(WebhookInbox)
            .where(
                WebhookInbox.id == event_id,
                WebhookInbox.lease_owner == owner,
                WebhookInbox.status == InboxStatus.PROCESSING.value,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    def fail(
        self,
        session: Session,
        event_id: uuid.UUID,
        owner: str,
        *,
        error: str,
        retry_in_seconds: float | None,
    ) -> InboxStatus:
        """Schedule a retry (``retry_in_seconds``) or dead-letter the event (``None``)."""
        status = InboxStatus.PENDING if retry_in_seconds is not None else InboxStatus.DEAD_LETTER
        values: dict[str, Any] = {
            "status": status.value,
            "last_error": error[:4000],
            "lease_owner": None,
            "lease_expires_at": None,
        }
        if retry_in_seconds is not None:
            values["available_at"] = func.now() + _seconds(retry_in_seconds)
        else:
            values["processed_at"] = func.now()
        session.execute(
            update(WebhookInbox)
            .where(
                WebhookInbox.id == event_id,
                WebhookInbox.lease_owner == owner,
                WebhookInbox.status == InboxStatus.PROCESSING.value,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return status

    def release(self, session: Session, event_id: uuid.UUID, owner: str, delay_seconds: float) -> None:
        """Give an event back without consuming an attempt (e.g. issue locked by another worker)."""
        session.execute(
            update(WebhookInbox)
            .where(
                WebhookInbox.id == event_id,
                WebhookInbox.lease_owner == owner,
                WebhookInbox.status == InboxStatus.PROCESSING.value,
            )
            .values(
                status=InboxStatus.PENDING.value,
                attempts=WebhookInbox.attempts - 1,
                lease_owner=None,
                lease_expires_at=None,
                available_at=func.now() + _seconds(delay_seconds),
            )
            .execution_options(synchronize_session=False)
        )

    def reap_expired_leases(self, session: Session) -> int:
        return len(session.execute(_REAP_SQL).all())

    def replay(self, session: Session, event_id: uuid.UUID) -> bool:
        result = session.execute(
            update(WebhookInbox)
            .where(
                WebhookInbox.id == event_id,
                WebhookInbox.status.in_([InboxStatus.DEAD_LETTER.value, InboxStatus.SKIPPED.value]),
            )
            .values(
                status=InboxStatus.PENDING.value,
                attempts=0,
                available_at=func.now(),
                processed_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    def depth_by_status(self, session: Session) -> dict[str, int]:
        rows = session.execute(select(WebhookInbox.status, func.count()).group_by(WebhookInbox.status)).all()
        return {status: int(count) for status, count in rows}

    def oldest_pending_age_seconds(self, session: Session) -> float:
        value = session.execute(
            text(
                "SELECT coalesce(extract(epoch FROM now() - min(received_at)), 0) "
                "FROM webhook_inbox WHERE status = 'PENDING'"
            )
        ).scalar_one()
        return float(value)

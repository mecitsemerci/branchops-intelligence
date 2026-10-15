"""Transactional outboxes: rows written in the same transaction as the business change, delivered later.

Same delivery guarantees as the inbox: claim with ``FOR UPDATE SKIP LOCKED`` under a lease, retries with
backoff, dead letter after ``max_attempts``, reaping of expired leases. Idempotency is a unique key per row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from branchops.infrastructure.database.models import OutboxMixin


@dataclass(frozen=True)
class OutboxItem:
    id: uuid.UUID
    idempotency_key: str
    incident_key: str | None
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    lease_owner: str
    extra: dict[str, Any]

    @property
    def is_last_attempt(self) -> bool:
        return self.attempts >= self.max_attempts


def _seconds(value: float) -> Any:
    return func.make_interval(0, 0, 0, 0, 0, 0, value)


class OutboxRepository:
    """Repository over one outbox table (``model`` is an ``OutboxMixin`` ORM class)."""

    def __init__(self, model: type[OutboxMixin], extra_columns: tuple[str, ...] = ()) -> None:
        self.model = model
        self._extra = extra_columns

    def enqueue(
        self,
        session: Session,
        *,
        idempotency_key: str,
        kind: str,
        payload: dict[str, Any],
        incident_key: str | None = None,
        max_attempts: int = 8,
        delay_seconds: float = 0.0,
        **extra: Any,
    ) -> uuid.UUID | None:
        """Insert unless a row with the same idempotency key exists; returns the new id or ``None``."""
        table = self.model.__table__  # type: ignore[attr-defined]
        values: dict[str, Any] = {
            "id": uuid.uuid4(),
            "idempotency_key": idempotency_key[:300],
            "incident_key": incident_key,
            "kind": kind,
            "payload": payload,
            "status": "PENDING",
            "attempts": 0,
            "max_attempts": max_attempts,
            "available_at": func.now() + _seconds(delay_seconds),
            **extra,
        }
        stmt = (
            insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(table.c.id)
        )
        new_id: uuid.UUID | None = session.execute(stmt).scalar_one_or_none()
        return new_id

    def claim(self, session: Session, owner: str, limit: int, lease_seconds: int) -> list[OutboxItem]:
        m = self.model
        rows = (
            session.execute(
                select(m)
                .where(m.status == "PENDING", m.available_at <= func.now())
                .order_by(m.available_at, m.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        items: list[OutboxItem] = []
        for row in rows:
            row.status = "PROCESSING"
            row.attempts += 1
            row.lease_owner = owner
            row.lease_expires_at = session.execute(select(func.now() + _seconds(lease_seconds))).scalar_one()
            items.append(
                OutboxItem(
                    id=row.id,
                    idempotency_key=row.idempotency_key,
                    incident_key=row.incident_key,
                    kind=row.kind,
                    payload=dict(row.payload),
                    attempts=row.attempts,
                    max_attempts=row.max_attempts,
                    lease_owner=owner,
                    extra={c: getattr(row, c) for c in self._extra},
                )
            )
        session.flush()
        return items

    def _owned(self, item_id: uuid.UUID, owner: str) -> Any:
        m = self.model
        return update(m).where(m.id == item_id, m.status == "PROCESSING", m.lease_owner == owner)

    def complete(
        self, session: Session, item_id: uuid.UUID, owner: str, result: dict[str, Any] | None
    ) -> bool:
        done = session.execute(
            self._owned(item_id, owner).values(
                status="DONE",
                result=result,
                completed_at=func.now(),
                lease_owner=None,
                lease_expires_at=None,
                last_error=None,
            )
        )
        return bool(done.rowcount)  # type: ignore[attr-defined]

    def fail(self, session: Session, item: OutboxItem, error: str, *, retry_in: float | None) -> str:
        """``retry_in=None`` or the last attempt -> DEAD_LETTER; otherwise back to PENDING after the delay."""
        status = "DEAD_LETTER" if retry_in is None or item.is_last_attempt else "PENDING"
        session.execute(
            self._owned(item.id, item.lease_owner).values(
                status=status,
                last_error=error[:2000],
                lease_owner=None,
                lease_expires_at=None,
                available_at=func.now() + _seconds(retry_in or 0.0),
            )
        )
        return status

    def defer(self, session: Session, item: OutboxItem, delay_seconds: float, reason: str) -> None:
        """Not ready yet (e.g. the ticket does not exist yet): retry later without consuming an attempt."""
        m = self.model
        session.execute(
            self._owned(item.id, item.lease_owner).values(
                status="PENDING",
                attempts=m.attempts - 1,
                last_error=reason[:2000],
                lease_owner=None,
                lease_expires_at=None,
                available_at=func.now() + _seconds(delay_seconds),
            )
        )

    def reap_expired_leases(self, session: Session) -> int:
        m = self.model
        res = session.execute(
            update(m)
            .where(m.status == "PROCESSING", m.lease_expires_at < func.now())
            .values(status="PENDING", lease_owner=None, lease_expires_at=None)
        )
        return int(res.rowcount)  # type: ignore[attr-defined]

    def replay(self, session: Session, item_id: uuid.UUID) -> bool:
        m = self.model
        res = session.execute(
            update(m)
            .where(m.id == item_id, m.status.in_(["DEAD_LETTER", "DONE"]))
            .values(status="PENDING", attempts=0, available_at=func.now(), last_error=None)
        )
        return bool(res.rowcount)  # type: ignore[attr-defined]

    def result_of(self, session: Session, idempotency_key: str) -> dict[str, Any] | None:
        """``result`` of a DONE row (e.g. the created ticket key), else ``None``."""
        m = self.model
        row = session.execute(
            select(m.result).where(m.idempotency_key == idempotency_key, m.status == "DONE")
        ).scalar_one_or_none()
        return dict(row) if row else None

    def depth_by_status(self, session: Session) -> dict[str, int]:
        m = self.model
        return {
            k: int(v) for k, v in session.execute(select(m.status, func.count()).group_by(m.status)).all()
        }

    def for_incident(self, session: Session, incident_key: str) -> list[dict[str, Any]]:
        m = self.model
        rows = session.execute(
            select(m).where(m.incident_key == incident_key).order_by(m.created_at)
        ).scalars()
        return [
            {
                "id": str(r.id),
                "kind": r.kind,
                "status": r.status,
                "attempts": r.attempts,
                "last_error": (r.last_error or "")[:500] or None,
                "result": r.result,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                **{c: getattr(r, c) for c in self._extra},
            }
            for r in rows
        ]

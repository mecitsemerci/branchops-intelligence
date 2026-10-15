"""Delivers ``ticketing_outbox`` rows to the configured :class:`TicketingSink`."""

from __future__ import annotations

from typing import Any

from branchops.application.interfaces.delivery import PermanentDeliveryError, RetryLaterError
from branchops.application.interfaces.ticketing import TicketingSink, TicketRequest
from branchops.infrastructure.database.models import TicketingOutbox
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.persistence.outbox_repository import OutboxItem, OutboxRepository

TICKETING_OUTBOX = OutboxRepository(TicketingOutbox)


def create_key(incident_key: str) -> str:
    """Idempotency key of the create_ticket row of an incident (other rows wait for its result)."""
    return f"ticket:{incident_key}:create"


class TicketingHandler:
    def __init__(self, db: Database, sink: TicketingSink) -> None:
        self._db = db
        self._sink = sink

    def _ticket_key(self, incident_key: str) -> str:
        with self._db.session() as s:
            result = TICKETING_OUTBOX.result_of(s, create_key(incident_key))
        if not result or not result.get("ticket_key"):
            raise RetryLaterError("ticket not created yet")
        return str(result["ticket_key"])

    def __call__(self, item: OutboxItem) -> dict[str, Any] | None:
        p = item.payload
        if item.kind == "create_ticket":
            ref = self._sink.create_ticket(
                TicketRequest(
                    project_key=str(p["project_key"]),
                    summary=str(p["summary"]),
                    description=str(p["description"]),
                    labels=[str(x) for x in p.get("labels", [])],
                )
            )
            return {"ticket_key": ref.key, "url": ref.url, "sink": self._sink.name}
        if item.incident_key is None:
            raise PermanentDeliveryError("ticketing row without incident key")
        if item.kind == "add_comment":
            key = self._ticket_key(item.incident_key)
            self._sink.add_comment(key, str(p["body"]))
            return {"ticket_key": key}
        raise PermanentDeliveryError(f"unknown ticketing operation {item.kind!r}")

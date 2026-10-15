"""Delivers ``notification_outbox`` rows through the configured channels."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update

from branchops.application.interfaces.delivery import PermanentDeliveryError
from branchops.application.interfaces.notifications import NotificationChannel, NotificationMessage
from branchops.infrastructure.database.models import IncidentRouting
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.persistence.outbox_repository import OutboxItem


class NotificationHandler:
    def __init__(self, db: Database, channels: dict[str, NotificationChannel]) -> None:
        self._db = db
        self._channels = channels

    def __call__(self, item: OutboxItem) -> dict[str, Any] | None:
        message = NotificationMessage.from_payload(item.payload)
        channel = self._channels.get(message.channel)
        if channel is None:
            raise PermanentDeliveryError(f"channel {message.channel!r} is not configured")
        result = channel.send(message)
        if item.incident_key and message.event == "incident_reported":
            # Time to first notification (analytics): only the first successful delivery counts.
            with self._db.session() as s:
                s.execute(
                    update(IncidentRouting)
                    .where(
                        IncidentRouting.incident_key == item.incident_key,
                        IncidentRouting.first_notified_at.is_(None),
                    )
                    .values(first_notified_at=datetime.now(UTC))
                )
        return {"channel": channel.name, **(result or {})}

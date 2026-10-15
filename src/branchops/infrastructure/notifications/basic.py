"""Development / CI channels: log the envelope (never the body) or drop the message."""

from __future__ import annotations

import logging
from typing import Any

from branchops.application.interfaces.notifications import NotificationMessage

logger = logging.getLogger("branchops.notifications")


class LogChannel:
    def __init__(self, name: str) -> None:
        self.name = name

    def send(self, message: NotificationMessage) -> dict[str, Any] | None:
        logger.info(
            "notification (log channel)",
            extra={
                "channel": self.name,
                "event": message.event,
                "incident_key": message.incident_key,
                "recipient": message.recipient,
                "subject": message.subject,
            },
        )
        return {"delivered": "log"}


class NullChannel:
    def __init__(self, name: str) -> None:
        self.name = name

    def send(self, message: NotificationMessage) -> dict[str, Any] | None:
        return {"delivered": "null"}

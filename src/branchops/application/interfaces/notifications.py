"""Port: outbound notification channels (e-mail, Zoom Team Chat …), used only by the outbox drain loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class NotificationMessage:
    event: str
    channel: str
    # Delivery address on this channel: an e-mail address or a Zoom channel id.
    recipient: str
    subject: str
    text: str
    html: str | None = None
    card: dict[str, Any] | None = None
    # Display headers (e-mail): the decision's To / Cc addresses; delivery is to ``recipient`` only.
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    incident_key: str | None = None
    # Name of the environment variable holding the channel token (Zoom), never the token itself.
    secret_ref: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> NotificationMessage:
        return cls(**{k: payload.get(k) for k in cls.__dataclass_fields__ if k in payload})  # type: ignore[arg-type]


class NotificationChannel(Protocol):
    name: str

    def send(self, message: NotificationMessage) -> dict[str, Any] | None: ...

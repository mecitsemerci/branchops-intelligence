"""Port: external ticketing system for reported incidents (written only through ``ticketing_outbox``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class TicketRequest:
    project_key: str
    summary: str
    description: str
    labels: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TicketRef:
    key: str
    url: str | None = None


class TicketingSink(Protocol):
    name: str

    def create_ticket(self, request: TicketRequest) -> TicketRef: ...

    def add_comment(self, ticket_key: str, body: str) -> None: ...

    def add_attachment(self, ticket_key: str, filename: str, content_type: str, data: bytes) -> None: ...

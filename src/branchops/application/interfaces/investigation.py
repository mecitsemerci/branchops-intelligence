"""Read-only port for the bounded historical investigation (typed, parameterised; never SQL from a model)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class HistoryItem:
    """Approved, already-masked summary of a historical incident."""

    incident_key: str
    summary: str | None
    description_excerpt: str | None
    category: str | None
    application: str | None
    branch_code: str | None
    error_signature: str | None
    created_at: datetime | None
    source_status: str | None = None
    resolved_at: datetime | None = None
    quality_score: float | None = None
    related: list[str] = field(default_factory=list)

    def text(self) -> str:
        return " ".join(filter(None, [self.summary, self.description_excerpt]))


class InvestigationReader(Protocol):
    def search_history(self, query: str, limit: int, exclude: str) -> list[HistoryItem]: ...

    def issue_summary(self, incident_key: str) -> HistoryItem | None: ...

    def same_signature(self, error_signature: str, limit: int, exclude: str) -> list[HistoryItem]: ...

    def known_resolutions(self, error_signature: str, limit: int, exclude: str) -> list[HistoryItem]:
        """Incidents with the signature whose source status is resolved/closed (no resolution text is
        captured by the sources; the status and resolution time are what is known)."""
        ...

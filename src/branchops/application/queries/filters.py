"""Issue explorer filter, sort field and pagination result types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

SortField = Literal["updated", "quality", "completeness", "incident_key", "issue_key"]


@dataclass
class IssueFilter:
    search: str | None = None
    source: str | None = None
    category: str | None = None
    branch_code: str | None = None
    application: str | None = None
    status: str | None = None
    low_quality_only: bool = False
    # Only incidents created or updated in the last N days (by source timestamps); None = no limit.
    since_days: int | None = None
    sort: SortField = "updated"
    descending: bool = True
    page: int = 1
    page_size: int = 25


@dataclass
class Page:
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int

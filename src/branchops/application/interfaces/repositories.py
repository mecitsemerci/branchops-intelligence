"""Ports used by the workflow for read access to historical data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from branchops.domain.entities.analysis import AnalysisResult


@dataclass(frozen=True)
class CandidateIssue:
    issue_key: str
    summary: str | None
    description_excerpt: str | None
    category: str | None
    subcategory: str | None
    branch_code: str | None
    application: str | None
    error_signature: str | None
    created_at: datetime | None
    text_rank: float


class HistoricalIssueReader(Protocol):
    def find_candidates(
        self,
        issue_key: str,
        query_text: str,
        error_signature: str | None,
        lookback_days: int,
        limit: int,
    ) -> list[CandidateIssue]: ...

    def count_signature_occurrences(self, issue_key: str, error_signature: str, window_days: int) -> int: ...


class AnalysisSink(Protocol):
    def save(self, result: AnalysisResult) -> None: ...

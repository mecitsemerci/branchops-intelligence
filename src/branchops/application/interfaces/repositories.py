"""Ports used by the workflow for read access to historical data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from branchops.domain.entities.analysis import AnalysisResult, StepRecord


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


@dataclass(frozen=True)
class RunInfo:
    analysis_id: str
    issue_key: str
    correlation_id: str
    inbox_event_id: str | None
    input_hash: str
    prompt_version: str
    started_at: datetime
    worker_id: str | None
    model_id: str | None
    resumed: bool = False


class ProgressReporter(Protocol):
    """Receives live workflow progress (used by the dashboard's real-time agent view)."""

    def run_started(self, info: RunInfo) -> None: ...

    def step_started(self, analysis_id: str, step: str) -> None: ...

    def step_finished(self, analysis_id: str, record: StepRecord) -> None: ...

    def run_failed(self, analysis_id: str, error: str) -> None: ...


class NullProgressReporter:
    def run_started(self, info: RunInfo) -> None:
        return None

    def step_started(self, analysis_id: str, step: str) -> None:
        return None

    def step_finished(self, analysis_id: str, record: StepRecord) -> None:
        return None

    def run_failed(self, analysis_id: str, error: str) -> None:
        return None

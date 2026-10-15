"""ReadModels facade over the per-concern read-side query modules (SQL only, no model calls)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from branchops.application.queries.analytics import quality_analytics
from branchops.application.queries.executions import execution_steps, executions, inbox_summary
from branchops.application.queries.filters import IssueFilter, Page, SortField
from branchops.application.queries.issues import issue_detail, list_issues
from branchops.application.queries.live import live_activity
from branchops.application.queries.overview import Overview, overview
from branchops.application.queries.workflow_nodes import WORKFLOW_NODES, node_states, step_node

__all__ = [
    "WORKFLOW_NODES",
    "IssueFilter",
    "Overview",
    "Page",
    "ReadModels",
    "SortField",
    "live_activity",
    "node_states",
    "step_node",
]


class ReadModels:
    def __init__(self, low_quality_threshold: float) -> None:
        self.low_quality_threshold = low_quality_threshold

    def overview(self, s: Session, days: int = 30) -> Overview:
        return overview(s, days=days, low_quality_threshold=self.low_quality_threshold)

    def list_issues(self, s: Session, f: IssueFilter) -> Page:
        return list_issues(s, f, low_quality_threshold=self.low_quality_threshold)

    def issue_detail(self, s: Session, issue_key: str) -> dict[str, Any] | None:
        return issue_detail(s, issue_key)

    def quality_analytics(self, s: Session, days: int = 90) -> dict[str, Any]:
        return quality_analytics(s, days=days, low_quality_threshold=self.low_quality_threshold)

    def executions(
        self, s: Session, *, status: str | None = None, page: int = 1, page_size: int = 25
    ) -> Page:
        return executions(s, status=status, page=page, page_size=page_size)

    def execution_steps(self, s: Session, analysis_id: str) -> list[dict[str, Any]]:
        return execution_steps(s, analysis_id)

    def inbox_summary(self, s: Session) -> dict[str, Any]:
        return inbox_summary(s)

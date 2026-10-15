"""ReadModels facade over the per-concern read-side query modules (SQL only, no model calls)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from branchops.application.queries.analytics import quality_analytics, recurring_errors
from branchops.application.queries.executions import execution_steps, executions, inbox_health, inbox_summary
from branchops.application.queries.filters import IssueFilter, Page, SortField
from branchops.application.queries.issues import issue_detail, list_issues
from branchops.application.queries.live import live_activity
from branchops.application.queries.overview import Overview, overview
from branchops.application.queries.scope import UNRESTRICTED, QueryScope, ScopeGrant
from branchops.application.queries.similarity import similar_incidents, similar_to_text
from branchops.application.queries.workflow_nodes import WORKFLOW_NODES, node_states, step_node

__all__ = [
    "WORKFLOW_NODES",
    "IssueFilter",
    "Overview",
    "Page",
    "QueryScope",
    "ReadModels",
    "ScopeGrant",
    "SortField",
    "live_activity",
    "node_states",
    "step_node",
]


class ReadModels:
    def __init__(self, low_quality_threshold: float) -> None:
        self.low_quality_threshold = low_quality_threshold

    def overview(self, s: Session, days: int = 30, *, scope: QueryScope = UNRESTRICTED) -> Overview:
        return overview(s, days=days, low_quality_threshold=self.low_quality_threshold, scope=scope)

    def list_issues(self, s: Session, f: IssueFilter, *, scope: QueryScope = UNRESTRICTED) -> Page:
        return list_issues(s, f, low_quality_threshold=self.low_quality_threshold, scope=scope)

    def issue_detail(
        self, s: Session, incident_key: str, *, scope: QueryScope = UNRESTRICTED
    ) -> dict[str, Any] | None:
        return issue_detail(s, incident_key, scope=scope)

    def quality_analytics(
        self, s: Session, days: int = 90, *, scope: QueryScope = UNRESTRICTED
    ) -> dict[str, Any]:
        return quality_analytics(s, days=days, low_quality_threshold=self.low_quality_threshold, scope=scope)

    def recurring_errors(
        self,
        s: Session,
        *,
        days: int = 30,
        min_count: int = 3,
        branch_code: str | None = None,
        application: str | None = None,
        limit: int = 50,
        scope: QueryScope = UNRESTRICTED,
    ) -> list[dict[str, Any]]:
        return recurring_errors(
            s,
            days=days,
            min_count=min_count,
            branch_code=branch_code,
            application=application,
            limit=limit,
            scope=scope,
        )

    def similar_incidents(
        self, s: Session, incident_key: str, *, limit: int = 10, scope: QueryScope = UNRESTRICTED
    ) -> list[dict[str, Any]] | None:
        return similar_incidents(s, incident_key, limit=limit, scope=scope)

    def similar_to_text(
        self, s: Session, text: str, *, limit: int = 10, scope: QueryScope = UNRESTRICTED
    ) -> list[dict[str, Any]]:
        return similar_to_text(s, text, limit=limit, scope=scope)

    def executions(
        self, s: Session, *, status: str | None = None, page: int = 1, page_size: int = 25
    ) -> Page:
        return executions(s, status=status, page=page, page_size=page_size)

    def execution_steps(self, s: Session, analysis_id: str) -> list[dict[str, Any]]:
        return execution_steps(s, analysis_id)

    def inbox_summary(self, s: Session) -> dict[str, Any]:
        return inbox_summary(s)

    def inbox_health(self, s: Session) -> dict[str, Any]:
        return inbox_health(s)

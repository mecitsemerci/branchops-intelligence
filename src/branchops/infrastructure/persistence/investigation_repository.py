"""``InvestigationReader`` over PostgreSQL: fixed, parameterised queries returning masked summaries."""

from __future__ import annotations

from sqlalchemy import or_, select

from branchops.application.interfaces.investigation import HistoryItem
from branchops.domain.services.lexicon import expand_query
from branchops.infrastructure.database.models import Issue, IssueRelationship
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.persistence.issue_repository import IssueRepository

# Source statuses that mean "resolved" in Jira (Turkish/English workflows) and ServiceNow.
RESOLVED_STATUSES = ("çözüldü", "kapatıldı", "kapalı", "resolved", "closed", "done", "tamamlandı")


def _item(issue: Issue, related: list[str] | None = None) -> HistoryItem:
    return HistoryItem(
        incident_key=issue.incident_key,
        summary=issue.summary,
        description_excerpt=(issue.description or "")[:400] or None,
        category=issue.category,
        application=issue.application,
        branch_code=issue.branch_code,
        error_signature=issue.error_signature,
        created_at=issue.source_created_at or issue.created_at,
        source_status=issue.status,
        resolved_at=getattr(issue, "resolved_at", None),
        quality_score=issue.latest_quality,
        related=related or [],
    )


class DatabaseInvestigationReader:
    def __init__(self, db: Database, lookback_days: int = 365) -> None:
        self._db = db
        self._issues = IssueRepository()
        self._lookback_days = lookback_days

    def search_history(self, query: str, limit: int, exclude: str) -> list[HistoryItem]:
        with self._db.session() as s:
            candidates = self._issues.find_candidates(
                s, exclude, expand_query(query[:200]), None, self._lookback_days, min(limit, 10)
            )
            keys = [c.incident_key for c in candidates]
            issues = {
                i.incident_key: i
                for i in s.execute(select(Issue).where(Issue.incident_key.in_(keys))).scalars()
            }
            return [_item(issues[k]) for k in keys if k in issues]

    def issue_summary(self, incident_key: str) -> HistoryItem | None:
        with self._db.session() as s:
            issue = s.get(Issue, incident_key)
            if issue is None or issue.is_deleted:
                return None
            related = list(
                s.execute(
                    select(IssueRelationship.target_incident_key)
                    .where(IssueRelationship.source_incident_key == incident_key)
                    .limit(20)
                ).scalars()
            )
            return _item(issue, related)

    def same_signature(self, error_signature: str, limit: int, exclude: str) -> list[HistoryItem]:
        with self._db.session() as s:
            rows = s.execute(
                select(Issue)
                .where(
                    Issue.error_signature == error_signature,
                    Issue.incident_key != exclude,
                    Issue.is_deleted.is_(False),
                )
                .order_by(Issue.source_created_at.desc().nulls_last())
                .limit(min(limit, 10))
            ).scalars()
            return [_item(i) for i in rows]

    def known_resolutions(self, error_signature: str, limit: int, exclude: str) -> list[HistoryItem]:
        with self._db.session() as s:
            status = Issue.status
            rows = s.execute(
                select(Issue)
                .where(
                    Issue.error_signature == error_signature,
                    Issue.incident_key != exclude,
                    Issue.is_deleted.is_(False),
                    or_(*[status.ilike(f"%{name}%") for name in RESOLVED_STATUSES]),
                )
                .order_by(Issue.source_created_at.desc().nulls_last())
                .limit(min(limit, 10))
            ).scalars()
            return [_item(i) for i in rows]

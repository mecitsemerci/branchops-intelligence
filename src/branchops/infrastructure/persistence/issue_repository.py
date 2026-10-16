"""Incident snapshot persistence, retention and historical retrieval."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from branchops.application.interfaces.repositories import CandidateIssue
from branchops.domain.entities.incident import IncidentSnapshot, parse_incident_key
from branchops.domain.services.lexicon import expand_query
from branchops.infrastructure.database.models import (
    Analysis,
    Issue,
    IssueComment,
    IssueEmbedding,
    IssueRelationship,
)
from branchops.infrastructure.database.session import Database

_CANDIDATES_SQL = text(
    """
    WITH q AS (
        SELECT CASE WHEN plainto_tsquery('turkish', :query)::text = '' THEN NULL
                    ELSE replace(plainto_tsquery('turkish', :query)::text, '&', '|')::tsquery END AS tsq
    )
    SELECT i.incident_key, i.source, i.summary, left(i.description, 400) AS description_excerpt,
           i.category, i.subcategory, i.branch_code, i.application, i.error_signature,
           coalesce(i.source_created_at, i.created_at) AS created_at,
           CASE WHEN q.tsq IS NULL THEN 0 ELSE ts_rank_cd(i.search_vector, q.tsq) END AS rank
    FROM issues i CROSS JOIN q
    WHERE i.incident_key <> :incident_key
      AND NOT i.is_deleted
      AND coalesce(i.source_created_at, i.created_at) >= now() - make_interval(days => :days)
      AND ((q.tsq IS NOT NULL AND i.search_vector @@ q.tsq)
           OR (CAST(:signature AS text) IS NOT NULL AND i.error_signature = CAST(:signature AS text)))
    ORDER BY rank DESC, created_at DESC
    LIMIT :limit
    """
)


class IssueRepository:
    def lock_snapshot(self, session: Session, incident_key: str) -> IncidentSnapshot | None:
        row = session.execute(
            select(Issue.snapshot).where(Issue.incident_key == incident_key).with_for_update()
        ).scalar_one_or_none()
        return IncidentSnapshot.model_validate(row) if row is not None else None

    def get_snapshot(self, session: Session, incident_key: str) -> IncidentSnapshot | None:
        row = session.execute(
            select(Issue.snapshot).where(Issue.incident_key == incident_key)
        ).scalar_one_or_none()
        return IncidentSnapshot.model_validate(row) if row is not None else None

    def exists(self, session: Session, incident_key: str) -> bool:
        found = session.execute(
            select(Issue.incident_key).where(Issue.incident_key == incident_key)
        ).scalar_one_or_none()
        return found is not None

    def save_snapshot(self, session: Session, snapshot: IncidentSnapshot) -> None:
        values = {
            "incident_key": snapshot.incident_key,
            "source": snapshot.source,
            "external_key": snapshot.external_key,
            "issue_key": snapshot.external_key,
            "external_id": snapshot.external_id,
            "group_key": snapshot.group_key,
            "summary": snapshot.summary,
            "description": snapshot.description,
            "status": snapshot.status,
            "snapshot": snapshot.model_dump(mode="json"),
            "source_created_at": snapshot.created_at,
            "source_updated_at": snapshot.updated_at,
            "is_deleted": snapshot.deleted,
        }
        stmt = insert(Issue).values(**values)
        update_cols: dict[str, Any] = {
            k: stmt.excluded[k] for k in values if k not in ("incident_key", "source", "external_key")
        }
        update_cols["updated_at"] = func.now()
        session.execute(stmt.on_conflict_do_update(index_elements=["incident_key"], set_=update_cols))
        for c in snapshot.comments:
            cstmt = insert(IssueComment).values(
                incident_key=snapshot.incident_key,
                comment_id=c.comment_id,
                body=c.body,
                author=c.author,
                created_at=c.created_at,
                updated_at=c.updated_at,
                is_deleted=c.deleted,
            )
            session.execute(
                cstmt.on_conflict_do_update(
                    index_elements=["incident_key", "comment_id"],
                    set_={
                        "body": cstmt.excluded.body,
                        "updated_at": cstmt.excluded.updated_at,
                        "is_deleted": cstmt.excluded.is_deleted,
                    },
                )
            )

    def mark_deleted(self, session: Session, incident_key: str) -> None:
        session.execute(
            update(Issue)
            .where(Issue.incident_key == incident_key)
            .values(is_deleted=True, deleted_at=datetime.now(UTC))
        )
        session.execute(
            delete(IssueRelationship).where(
                (IssueRelationship.source_incident_key == incident_key)
                | (IssueRelationship.target_incident_key == incident_key)
            )
        )

    def purge(self, session: Session, incident_key: str) -> None:
        """Erase all content and derived data (retention policy ``purge``).

        A content-free tombstone row is kept so that late, out-of-order events cannot re-create the issue.
        """
        session.execute(delete(Analysis).where(Analysis.incident_key == incident_key))
        session.execute(delete(IssueComment).where(IssueComment.incident_key == incident_key))
        session.execute(delete(IssueEmbedding).where(IssueEmbedding.incident_key == incident_key))
        session.execute(
            delete(IssueRelationship).where(
                (IssueRelationship.source_incident_key == incident_key)
                | (IssueRelationship.target_incident_key == incident_key)
            )
        )
        source, external_key = parse_incident_key(incident_key)
        tombstone = IncidentSnapshot(source=source, external_key=external_key, deleted=True)
        session.execute(
            update(Issue)
            .where(Issue.incident_key == incident_key)
            .values(
                summary=None,
                description=None,
                status=None,
                snapshot=tombstone.model_dump(mode="json"),
                is_deleted=True,
                deleted_at=datetime.now(UTC),
                category=None,
                subcategory=None,
                branch_code=None,
                application=None,
                error_signature=None,
                latest_analysis_id=None,
                latest_completeness=None,
                latest_quality=None,
                latest_status=None,
            )
        )

    def latest_input_hash(self, session: Session, incident_key: str) -> str | None:
        return session.execute(
            select(Analysis.input_hash)
            # RUNNING/FAILED rows are in-flight or aborted attempts and must not suppress a retry; a result
            # degraded by a model outage is provisional, so an unchanged redelivery re-analyses it.
            .where(
                Analysis.incident_key == incident_key,
                Analysis.status.in_(["COMPLETED", "PARTIAL", "NEEDS_REVIEW"]),
                Analysis.recoverable.is_(False),
            )
            .order_by(Analysis.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def find_candidates(
        self,
        session: Session,
        incident_key: str,
        query_text: str,
        error_signature: str | None,
        lookback_days: int,
        limit: int,
    ) -> list[CandidateIssue]:
        rows = session.execute(
            _CANDIDATES_SQL,
            {
                "incident_key": incident_key,
                # Domain synonyms widen recall ("para sayma makinesi" ~ "banknot sayım cihazı").
                "query": expand_query(query_text[:1500])[:2000],
                "signature": error_signature,
                "days": lookback_days,
                "limit": limit,
            },
        ).all()
        return [
            CandidateIssue(
                incident_key=r.incident_key,
                source=r.source,
                summary=r.summary,
                description_excerpt=r.description_excerpt,
                category=r.category,
                subcategory=r.subcategory,
                branch_code=r.branch_code,
                application=r.application,
                error_signature=r.error_signature,
                created_at=r.created_at,
                text_rank=float(r.rank or 0.0),
            )
            for r in rows
        ]

    def count_signature_occurrences(
        self, session: Session, incident_key: str, error_signature: str, window_days: int
    ) -> int:
        """Number of non-deleted issues (including this one) sharing the signature within the window."""
        others = session.execute(
            select(func.count())
            .select_from(Issue)
            .where(
                Issue.error_signature == error_signature,
                Issue.incident_key != incident_key,
                Issue.is_deleted.is_(False),
                func.coalesce(Issue.source_created_at, Issue.created_at)
                >= func.now() - func.make_interval(0, 0, 0, window_days),
            )
        ).scalar_one()
        return int(others) + 1


class SessionBoundHistoryReader:
    """Adapts ``IssueRepository`` to the ``HistoricalIssueReader`` port using a session factory."""

    def __init__(self, db: Database, repo: IssueRepository) -> None:
        self._db = db
        self._repo = repo

    def find_candidates(
        self,
        incident_key: str,
        query_text: str,
        error_signature: str | None,
        lookback_days: int,
        limit: int,
    ) -> list[CandidateIssue]:
        with self._db.session() as s:
            return self._repo.find_candidates(
                s, incident_key, query_text, error_signature, lookback_days, limit
            )

    def count_signature_occurrences(self, incident_key: str, error_signature: str, window_days: int) -> int:
        with self._db.session() as s:
            return self._repo.count_signature_occurrences(s, incident_key, error_signature, window_days)

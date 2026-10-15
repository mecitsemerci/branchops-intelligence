"""Similar / duplicate / recurring incidents for one incident or for free text (SQL full-text search only)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from branchops.application.queries.scope import UNRESTRICTED, QueryScope
from branchops.domain.entities.incident import incident_key as make_incident_key
from branchops.domain.entities.incident import parse_incident_key
from branchops.infrastructure.database.models import Issue, IssueRelationship


def _visible(scope: QueryScope, model: Any = Issue) -> list[Any]:
    predicate = scope.predicate(model)
    return [model.is_deleted.is_(False), *([predicate] if predicate is not None else [])]


def similar_incidents(
    s: Session, incident_key: str, *, limit: int = 10, scope: QueryScope = UNRESTRICTED
) -> list[dict[str, Any]] | None:
    """Verified relationships of an incident plus incidents sharing its error signature.

    Returns ``None`` when the incident does not exist or is outside ``scope``; related incidents outside the
    scope are never returned.
    """
    key = make_incident_key(*parse_incident_key(incident_key))
    base = s.execute(select(Issue).where(Issue.incident_key == key, *_visible(scope))).scalar_one_or_none()
    if base is None:
        return None
    limit = max(1, min(limit, 50))
    other = aliased(Issue)
    other_key = func.coalesce(
        func.nullif(IssueRelationship.target_incident_key, key), IssueRelationship.source_incident_key
    )
    rels = s.execute(
        select(IssueRelationship, other)
        .join(other, other.incident_key == other_key)
        .where(
            or_(IssueRelationship.source_incident_key == key, IssueRelationship.target_incident_key == key),
            *_visible(scope, other),
        )
        .order_by(IssueRelationship.text_rank.desc(), other.incident_key)
        .limit(limit)
    ).all()
    out: list[dict[str, Any]] = [
        {
            "incident_key": o.incident_key,
            "source": o.source,
            "group_key": o.group_key,
            "summary": o.summary,
            "branch_code": o.branch_code,
            "relationship": rel.relationship_type,
            "text_rank": round(float(rel.text_rank or 0.0), 4),
            "reasons": [*(rel.signals or []), *([rel.rationale] if rel.rationale else [])],
        }
        for rel, o in rels
    ]
    seen = {key, *(item["incident_key"] for item in out)}
    if base.error_signature and len(out) < limit:
        recurring = s.execute(
            select(Issue)
            .where(
                Issue.error_signature == base.error_signature,
                Issue.incident_key.not_in(seen),
                *_visible(scope),
            )
            .order_by(func.coalesce(Issue.source_created_at, Issue.created_at).desc())
            .limit(limit - len(out))
        ).scalars()
        out.extend(
            {
                "incident_key": o.incident_key,
                "source": o.source,
                "group_key": o.group_key,
                "summary": o.summary,
                "branch_code": o.branch_code,
                "relationship": "RECURRING",
                "text_rank": 0.0,
                "reasons": [f"same error signature: {base.error_signature}"],
            }
            for o in recurring
        )
    return out


def similar_to_text(
    s: Session, text: str, *, limit: int = 10, days: int = 365, scope: QueryScope = UNRESTRICTED
) -> list[dict[str, Any]]:
    """Incidents whose summary/description match ``text`` (PostgreSQL Turkish full-text search)."""
    query = func.websearch_to_tsquery("turkish", text.strip()[:500])
    rank = func.ts_rank_cd(Issue.search_vector, query)
    rows = s.execute(
        select(Issue, rank.label("rank"))
        .where(
            and_(
                Issue.search_vector.op("@@")(query),
                func.coalesce(Issue.source_created_at, Issue.created_at)
                >= func.now() - func.make_interval(0, 0, 0, max(1, days)),
                *_visible(scope),
            )
        )
        .order_by(rank.desc(), Issue.incident_key)
        .limit(max(1, min(limit, 50)))
    ).all()
    return [
        {
            "incident_key": o.incident_key,
            "source": o.source,
            "group_key": o.group_key,
            "summary": o.summary,
            "branch_code": o.branch_code,
            "relationship": "TEXT_MATCH",
            "text_rank": round(float(r or 0.0), 4),
            "reasons": ["full-text match on summary/description"]
            + ([f"error signature: {o.error_signature}"] if o.error_signature else []),
        }
        for o, r in rows
    ]

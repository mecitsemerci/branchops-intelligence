"""Issue explorer listing and single-issue detail."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from branchops.application.queries._common import _analysis_dict, _r
from branchops.application.queries.filters import IssueFilter, Page
from branchops.infrastructure.database.models import Analysis, Issue, IssueComment, IssueRelationship


def list_issues(s: Session, f: IssueFilter, *, low_quality_threshold: float) -> Page:
    conditions: list[Any] = [Issue.is_deleted.is_(False)]
    if f.search:
        term = f.search.strip()[:200]
        conditions.append(
            or_(
                Issue.issue_key.ilike(f"%{term}%"),
                Issue.search_vector.op("@@")(func.websearch_to_tsquery("turkish", term)),
            )
        )
    if f.category:
        conditions.append(Issue.category == f.category)
    if f.branch_code:
        conditions.append(Issue.branch_code == f.branch_code)
    if f.application:
        conditions.append(Issue.application.ilike(f"%{f.application[:100]}%"))
    if f.status:
        conditions.append(Issue.latest_status == f.status)
    if f.low_quality_only:
        conditions.append(Issue.latest_quality < low_quality_threshold)
    sort_col = {
        "updated": func.coalesce(Issue.source_updated_at, Issue.updated_at),
        "quality": Issue.latest_quality,
        "completeness": Issue.latest_completeness,
        "issue_key": Issue.issue_key,
    }[f.sort]
    order = sort_col.desc().nulls_last() if f.descending else sort_col.asc().nulls_last()
    total = s.execute(select(func.count()).select_from(Issue).where(and_(*conditions))).scalar_one()
    page_size = max(1, min(f.page_size, 200))
    page = max(1, f.page)
    rows = s.execute(
        select(
            Issue.issue_key,
            Issue.summary,
            Issue.status,
            Issue.category,
            Issue.subcategory,
            Issue.branch_code,
            Issue.application,
            Issue.latest_completeness,
            Issue.latest_quality,
            Issue.latest_status,
            Issue.source_updated_at,
        )
        .where(and_(*conditions))
        .order_by(order, Issue.issue_key)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items = [
        {
            "issue_key": r.issue_key,
            "summary": r.summary,
            "jira_status": r.status,
            "category": r.category,
            "subcategory": r.subcategory,
            "branch_code": r.branch_code,
            "application": r.application,
            "completeness": _r(r.latest_completeness),
            "quality": _r(r.latest_quality),
            "analysis_status": r.latest_status,
            "updated_at": r.source_updated_at.isoformat() if r.source_updated_at else None,
        }
        for r in rows
    ]
    return Page(items=items, total=int(total), page=page, page_size=page_size)


def issue_detail(s: Session, issue_key: str) -> dict[str, Any] | None:
    issue = s.get(Issue, issue_key)
    if issue is None or issue.is_deleted:
        return None
    latest = s.get(Analysis, issue.latest_analysis_id) if issue.latest_analysis_id else None
    history = s.execute(
        select(
            Analysis.id,
            Analysis.started_at,
            Analysis.status,
            Analysis.completeness_score,
            Analysis.quality_score,
            Analysis.template_id,
            Analysis.template_version,
            Analysis.scoring_policy_version,
            Analysis.model_id,
        )
        .where(Analysis.issue_key == issue_key)
        .order_by(Analysis.started_at)
    ).all()
    relationships = s.execute(
        select(IssueRelationship, Issue.summary)
        .join(
            Issue,
            Issue.issue_key
            == func.coalesce(
                func.nullif(IssueRelationship.target_issue_key, issue_key),
                IssueRelationship.source_issue_key,
            ),
        )
        .where(
            or_(
                IssueRelationship.source_issue_key == issue_key,
                IssueRelationship.target_issue_key == issue_key,
            ),
            Issue.is_deleted.is_(False),
        )
        .order_by(IssueRelationship.text_rank.desc())
    ).all()
    comments = s.execute(
        select(IssueComment)
        .where(IssueComment.issue_key == issue_key, IssueComment.is_deleted.is_(False))
        .order_by(IssueComment.created_at)
    ).scalars()
    return {
        "issue": {
            "issue_key": issue.issue_key,
            "project_key": issue.project_key,
            "summary": issue.summary,
            "description": issue.description,
            "jira_status": issue.status,
            "category": issue.category,
            "subcategory": issue.subcategory,
            "branch_code": issue.branch_code,
            "application": issue.application,
            "source_created_at": issue.source_created_at.isoformat() if issue.source_created_at else None,
            "source_updated_at": issue.source_updated_at.isoformat() if issue.source_updated_at else None,
        },
        "comments": [
            {
                "id": c.comment_id,
                "author": c.author,
                "body": c.body,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in comments
        ],
        "latest_analysis": _analysis_dict(latest) if latest else None,
        "score_history": [
            {
                "analysis_id": str(h.id),
                "started_at": h.started_at.isoformat(),
                "status": h.status,
                "completeness": _r(h.completeness_score),
                "quality": _r(h.quality_score),
                "template": f"{h.template_id}@{h.template_version}" if h.template_id else None,
                "scoring_policy_version": h.scoring_policy_version,
                "model_id": h.model_id,
            }
            for h in history
        ],
        "relationships": [
            {
                "issue_key": rel.target_issue_key
                if rel.source_issue_key == issue_key
                else rel.source_issue_key,
                "summary": summary,
                "relationship": rel.relationship_type,
                "text_rank": rel.text_rank,
                "signals": rel.signals,
                "rationale": rel.rationale,
                "direction": "outgoing" if rel.source_issue_key == issue_key else "incoming",
            }
            for rel, summary in relationships
        ],
    }

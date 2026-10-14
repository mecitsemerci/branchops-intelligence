"""Read API for issues, analyses, analytics and executions; analyst/admin actions."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from branchops.agents.reporting import ReportingAgent
from branchops.api.dependencies import AppServices, db_session, require, services
from branchops.application.commands.ingest_webhook import RequestReanalysis
from branchops.application.queries.read_models import IssueFilter
from branchops.domain.enums import Role
from branchops.infrastructure.jira.adapter import ISSUE_KEY_RE
from branchops.infrastructure.persistence.audit import audit
from branchops.infrastructure.persistence.inbox_repository import InboxRepository
from branchops.infrastructure.security.auth import Principal

router = APIRouter(prefix="/api", tags=["issues"])


@router.get("/issues")
def list_issues(
    search: str | None = Query(default=None, max_length=200),
    category: str | None = None,
    branch_code: str | None = None,
    application: str | None = None,
    analysis_status: str | None = None,
    low_quality_only: bool = False,
    sort: Literal["updated", "quality", "completeness", "issue_key"] = "updated",
    descending: bool = True,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    svc: AppServices = Depends(services),
    session: Session = Depends(db_session),
    _: Principal = Depends(require(Role.VIEWER)),
) -> dict[str, Any]:
    result = svc.read_models.list_issues(
        session,
        IssueFilter(
            search=search,
            category=category,
            branch_code=branch_code,
            application=application,
            status=analysis_status,
            low_quality_only=low_quality_only,
            sort=sort,
            descending=descending,
            page=page,
            page_size=page_size,
        ),
    )
    return {"items": result.items, "total": result.total, "page": result.page, "page_size": result.page_size}


@router.get("/issues/{issue_key}")
def issue_detail(
    issue_key: str,
    svc: AppServices = Depends(services),
    session: Session = Depends(db_session),
    _: Principal = Depends(require(Role.VIEWER)),
) -> dict[str, Any]:
    if not ISSUE_KEY_RE.match(issue_key):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid issue key")
    detail = svc.read_models.issue_detail(session, issue_key)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "issue not found")
    return detail


@router.post("/issues/{issue_key}/reanalyze", status_code=status.HTTP_202_ACCEPTED)
def reanalyze(
    issue_key: str,
    request: Request,
    svc: AppServices = Depends(services),
    principal: Principal = Depends(require(Role.ANALYST)),
) -> dict[str, Any]:
    if not ISSUE_KEY_RE.match(issue_key):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid issue key")
    with svc.db.session() as s:
        if svc.read_models.issue_detail(s, issue_key) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "issue not found")
        audit(
            s,
            actor=principal.username,
            action="reanalysis_requested",
            resource_type="issue",
            resource_id=issue_key,
        )
    event_id = RequestReanalysis(svc.db, svc.settings).execute(
        issue_key, requested_by=principal.username, correlation_id=request.state.correlation_id
    )
    return {"status": "queued", "event_id": event_id}


@router.get("/analytics/overview")
def overview(
    days: int = Query(default=30, ge=1, le=365),
    svc: AppServices = Depends(services),
    session: Session = Depends(db_session),
    _: Principal = Depends(require(Role.VIEWER)),
) -> dict[str, Any]:
    return svc.read_models.overview(session, days).__dict__


@router.get("/analytics/quality")
def quality(
    days: int = Query(default=90, ge=1, le=730),
    svc: AppServices = Depends(services),
    session: Session = Depends(db_session),
    _: Principal = Depends(require(Role.VIEWER)),
) -> dict[str, Any]:
    return svc.read_models.quality_analytics(session, days)


@router.get("/reports/summary")
def report_summary(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    svc: AppServices = Depends(services),
    session: Session = Depends(db_session),
    _: Principal = Depends(require(Role.VIEWER)),
) -> dict[str, Any]:
    metrics = {
        "period_days": days,
        "overview": {
            k: v
            for k, v in svc.read_models.overview(session, days).__dict__.items()
            if k not in ("daily_trend",)
        },
        "most_missing_fields": svc.read_models.quality_analytics(session, days)["most_missing_fields"][:5],
    }
    # The API process never calls the model; narrative generation by the model is a worker/CLI concern.
    report, source = ReportingAgent(None).run(metrics, request.state.correlation_id)
    return {"report": report.model_dump(), "source": source, "metrics": metrics}


@router.get("/executions")
def executions(
    analysis_status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    svc: AppServices = Depends(services),
    session: Session = Depends(db_session),
    _: Principal = Depends(require(Role.VIEWER)),
) -> dict[str, Any]:
    result = svc.read_models.executions(session, status=analysis_status, page=page, page_size=page_size)
    return {"items": result.items, "total": result.total, "page": result.page, "page_size": result.page_size}


@router.get("/executions/{analysis_id}/steps")
def execution_steps(
    analysis_id: str,
    svc: AppServices = Depends(services),
    session: Session = Depends(db_session),
    _: Principal = Depends(require(Role.VIEWER)),
) -> list[dict[str, Any]]:
    return svc.read_models.execution_steps(session, analysis_id)


@router.get("/admin/inbox")
def inbox_summary(
    svc: AppServices = Depends(services),
    session: Session = Depends(db_session),
    _: Principal = Depends(require(Role.ADMIN)),
) -> dict[str, Any]:
    return svc.read_models.inbox_summary(session)


@router.post("/admin/inbox/{event_id}/replay")
def replay(
    event_id: uuid.UUID,
    svc: AppServices = Depends(services),
    principal: Principal = Depends(require(Role.ADMIN)),
) -> dict[str, Any]:
    with svc.db.session() as s:
        ok = InboxRepository().replay(s, event_id)
        if not ok:
            raise HTTPException(status.HTTP_409_CONFLICT, "event is not in DEAD_LETTER/SKIPPED state")
        audit(
            s,
            actor=principal.username,
            action="inbox_replay",
            resource_type="inbox_event",
            resource_id=str(event_id),
        )
    return {"status": "requeued"}


@router.get("/templates")
def templates(
    svc: AppServices = Depends(services),
    _: Principal = Depends(require(Role.VIEWER)),
) -> list[dict[str, Any]]:
    return [
        {
            "template_id": t.template_id,
            "version": t.version,
            "name": t.name,
            "category": t.category,
            "subcategories": t.subcategories,
            "is_fallback": t.is_fallback,
            "fields": [
                {"name": f.name, "label": f.label, "weight": f.weight, "required": f.required}
                for f in t.fields
            ],
        }
        for t in svc.registry.latest_published()
    ]

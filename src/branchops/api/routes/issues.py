"""Read API for incidents ("issues"), analyses, analytics and executions; analyst/admin actions.

Incidents are addressed by ``incident_key`` (``jira:OPS-123``, ``snow:INC0012345``,
``csv:2026-10-import/row-17``); a bare legacy Jira key (``OPS-123``) is still accepted.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from branchops.agents.reporting import ReportingAgent
from branchops.api.dependencies import AppServices, db_session, require, services
from branchops.application.commands.attachments import AttachmentService
from branchops.application.commands.ingest_webhook import RequestReanalysis
from branchops.application.queries.read_models import IssueFilter
from branchops.domain.entities.incident import incident_key as make_incident_key
from branchops.domain.entities.incident import is_valid_incident_key, parse_incident_key
from branchops.domain.enums import Role
from branchops.infrastructure.attachments import build_attachment_store
from branchops.infrastructure.persistence.audit import audit
from branchops.infrastructure.persistence.inbox_repository import InboxRepository
from branchops.infrastructure.security.auth import Principal

router = APIRouter(prefix="/api", tags=["issues"])


def _incident_key(value: str) -> str:
    if not is_valid_incident_key(value):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid incident key")
    return make_incident_key(*parse_incident_key(value))


@router.get("/issues")
def list_issues(
    search: str | None = Query(default=None, max_length=200),
    source: str | None = Query(default=None, max_length=40),
    category: str | None = None,
    branch_code: str | None = None,
    application: str | None = None,
    analysis_status: str | None = None,
    low_quality_only: bool = False,
    sort: Literal["updated", "quality", "completeness", "incident_key", "issue_key"] = "updated",
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
            source=source,
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


@router.post("/issues/{incident_key:path}/reanalyze", status_code=status.HTTP_202_ACCEPTED)
def reanalyze(
    incident_key: str,
    request: Request,
    svc: AppServices = Depends(services),
    principal: Principal = Depends(require(Role.ANALYST)),
) -> dict[str, Any]:
    key = _incident_key(incident_key)
    with svc.db.session() as s:
        if svc.read_models.issue_detail(s, key) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "issue not found")
        audit(
            s,
            actor=principal.username,
            action="reanalysis_requested",
            resource_type="issue",
            resource_id=key,
        )
    event_id = RequestReanalysis(svc.db, svc.settings).execute(
        key, requested_by=principal.username, correlation_id=request.state.correlation_id
    )
    return {"status": "queued", "event_id": event_id}


def _attachments(svc: AppServices) -> AttachmentService:
    return AttachmentService(svc.settings, build_attachment_store(svc.settings))


@router.get("/issues/{incident_key:path}/attachments")
def list_attachments(
    incident_key: str,
    svc: AppServices = Depends(services),
    session: Session = Depends(db_session),
    _: Principal = Depends(require(Role.VIEWER)),
) -> list[dict[str, Any]]:
    """Attachment metadata of an incident (content via ``/api/attachments/{id}``)."""
    return _attachments(svc).list_for_incident(session, _incident_key(incident_key))


@router.get("/attachments/{attachment_id}")
def download_attachment(
    attachment_id: str,
    svc: AppServices = Depends(services),
    session: Session = Depends(db_session),
    principal: Principal = Depends(require(Role.VIEWER)),
) -> Response:
    """Attachment content (always as a download); every access is audited."""
    loaded = _attachments(svc).load(session, attachment_id)
    if loaded is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "attachment not found")
    meta, data = loaded
    audit(
        session,
        actor=principal.username,
        action="attachment_downloaded",
        resource_type="attachment",
        resource_id=meta["id"],
        details={"incident_key": meta["incident_key"], "via": "api"},
    )
    return Response(
        content=data,
        media_type=meta["content_type"],
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(meta['filename'])}",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/issues/{incident_key:path}")
def issue_detail(
    incident_key: str,
    svc: AppServices = Depends(services),
    session: Session = Depends(db_session),
    _: Principal = Depends(require(Role.VIEWER)),
) -> dict[str, Any]:
    detail = svc.read_models.issue_detail(session, _incident_key(incident_key))
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "issue not found")
    return detail


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


@router.get("/analytics/operations")
def operations(
    days: int = Query(default=90, ge=7, le=730),
    svc: AppServices = Depends(services),
    session: Session = Depends(db_session),
    _: Principal = Depends(require(Role.VIEWER)),
) -> dict[str, Any]:
    """Trends by application/branch/category, missing fields per category, quality after updates, reopen
    and recurrence after resolution (``UNKNOWN`` where the sources provide no status history)."""
    return svc.read_models.operational_intelligence(session, days)


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

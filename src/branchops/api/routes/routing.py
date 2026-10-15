"""Routing decisions, notification history and routing analytics."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from branchops.api.dependencies import AppServices, db_session, require, services
from branchops.application.commands.notifications import NotificationService, resend, send_test
from branchops.application.commands.routing import IncidentRouter
from branchops.application.queries.routing import notification_log, routing_analytics, unowned_screens
from branchops.domain.entities.incident import incident_key as make_incident_key
from branchops.domain.entities.incident import is_valid_incident_key, parse_incident_key
from branchops.domain.enums import Role
from branchops.infrastructure.persistence.ownership_repository import OwnershipRepository
from branchops.infrastructure.security.auth import Principal

router = APIRouter(prefix="/api")
Status = Literal["PENDING", "PROCESSING", "DONE", "DEAD_LETTER"]


def _key(value: str) -> str:
    if not is_valid_incident_key(value):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid incident key")
    return make_incident_key(*parse_incident_key(value))


@router.post("/routing/{incident_key:path}/reroute")
def reroute(
    incident_key: str,
    svc: AppServices = Depends(services),
    principal: Principal = Depends(require(Role.ANALYST)),
) -> dict[str, Any]:
    """Resolve the owner again with the current registry and notify the (new) teams; audited."""
    key = _key(incident_key)
    router_ = IncidentRouter(svc.db, svc.settings)
    if router_.routing_of(key) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "incident has no routing (not reported in-app)")
    return router_.reroute(key, principal.username)


@router.get("/routing/{incident_key:path}")
def routing(
    incident_key: str, svc: AppServices = Depends(services), _: Principal = Depends(require(Role.VIEWER))
) -> dict[str, Any]:
    found = IncidentRouter(svc.db, svc.settings).routing_of(_key(incident_key))
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no routing for this incident")
    return found


@router.get("/notifications")
def notifications(
    incident_key: str | None = Query(default=None, max_length=200),
    status_: Status | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(db_session),
    _: Principal = Depends(require(Role.VIEWER)),
) -> list[dict[str, Any]]:
    key = _key(incident_key) if incident_key else None
    return notification_log(session, incident_key=key, status=status_, limit=limit)


@router.post("/notifications/{notification_id}/resend")
def resend_notification(
    notification_id: str,
    session: Session = Depends(db_session),
    principal: Principal = Depends(require(Role.ADMIN)),
) -> dict[str, str]:
    if not resend(session, notification_id, principal.username):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "only delivered or dead-lettered notifications can be resent"
        )
    return {"status": "queued"}


@router.post("/admin/ownership/teams/{team_id}/test-notification")
def test_notification(
    team_id: int,
    svc: AppServices = Depends(services),
    session: Session = Depends(db_session),
    principal: Principal = Depends(require(Role.ADMIN)),
) -> dict[str, int]:
    registry = OwnershipRepository().load_registry(session)
    if registry.team(team_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown team")
    return {
        "queued": send_test(session, NotificationService(svc.settings), registry, team_id, principal.username)
    }


@router.get("/analytics/routing")
def analytics(
    days: int = Query(default=30, ge=1, le=365),
    session: Session = Depends(db_session),
    _: Principal = Depends(require(Role.VIEWER)),
) -> dict[str, Any]:
    return {**routing_analytics(session, days=days), "unowned_screens": unowned_screens(session, days=days)}

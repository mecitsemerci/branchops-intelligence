"""Ownership registry REST API: ADMIN CRUD under /api/admin/ownership/*, read endpoints for other systems."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, ValidationError

from branchops.api.dependencies import AppServices, require, services
from branchops.application.commands.ownership import OwnershipService
from branchops.domain.entities.ownership import (
    ApplicationInput,
    RoutingClassification,
    RoutingInput,
    RuleInput,
    ScreenInput,
    TeamInput,
)
from branchops.domain.enums import Role
from branchops.infrastructure.persistence.ownership_repository import OwnershipError
from branchops.infrastructure.security.auth import Principal

router = APIRouter(prefix="/api")
Entity = Literal["team", "application", "screen", "rule"]
MAX_CSV_BYTES = 2_000_000


def ownership(svc: AppServices = Depends(services)) -> OwnershipService:
    return OwnershipService(svc.db, svc.settings)


def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except OwnershipError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.errors(include_url=False)) from exc


# ---------------------------------------------------------------- read endpoints (VIEWER)
@router.get("/ownership/teams")
def list_teams(
    own: OwnershipService = Depends(ownership), _: Principal = Depends(require(Role.VIEWER))
) -> list[dict[str, Any]]:
    """Active teams with public contact data (no secret references)."""
    return [
        {k: v for k, v in t.items() if k != "zoom_webhook_secret_ref"} for t in own.teams() if t["active"]
    ]


@router.get("/ownership/applications")
def application_tree(
    own: OwnershipService = Depends(ownership), _: Principal = Depends(require(Role.VIEWER))
) -> list[dict[str, Any]]:
    return own.tree()


@router.get("/ownership/resolve")
def resolve(
    app: str | None = Query(default=None, max_length=64, description="Application key"),
    screen: str | None = Query(default=None, max_length=64, description="Screen key"),
    route: str | None = Query(default=None, max_length=500, description="Route / internal URL"),
    error_code: str | None = Query(default=None, max_length=100),
    branch_code: str | None = Query(default=None, max_length=32),
    category: str | None = Query(default=None, max_length=64),
    own: OwnershipService = Depends(ownership),
    _: Principal = Depends(require(Role.VIEWER)),
) -> dict[str, Any]:
    """Who would receive a report about this application / screen (deterministic, see docs)."""
    registry = own.registry()
    decision = own.resolve(
        RoutingInput(
            app_key=app, screen_key=screen, route=route, error_code=error_code, branch_code=branch_code
        ),
        RoutingClassification(category=category),
        registry,
    )
    return own.describe(decision, registry)


@router.get("/ownership/who-owns")
def who_owns(
    q: str = Query(min_length=2, max_length=100),
    own: OwnershipService = Depends(ownership),
    _: Principal = Depends(require(Role.VIEWER)),
) -> list[dict[str, Any]]:
    return [
        {**r, "owner": {k: v for k, v in (r["owner"] or {}).items() if k != "zoom_webhook_secret_ref"}}
        for r in own.who_owns(q)
    ]


# ---------------------------------------------------------------- admin (ADMIN)
admin = APIRouter(prefix="/api/admin/ownership")


@admin.get("/teams")
def admin_teams(
    own: OwnershipService = Depends(ownership), _: Principal = Depends(require(Role.ADMIN))
) -> list[dict[str, Any]]:
    return own.teams()


@admin.post("/teams", status_code=status.HTTP_201_CREATED)
def create_team(
    data: TeamInput, own: OwnershipService = Depends(ownership), p: Principal = Depends(require(Role.ADMIN))
) -> dict[str, Any]:
    return _call(own.save_team, data, p.username)  # type: ignore[no-any-return]


@admin.put("/teams/{team_id}")
def update_team(
    team_id: int,
    data: TeamInput,
    own: OwnershipService = Depends(ownership),
    p: Principal = Depends(require(Role.ADMIN)),
) -> dict[str, Any]:
    return _call(own.save_team, data, p.username, team_id)  # type: ignore[no-any-return]


@admin.post("/applications", status_code=status.HTTP_201_CREATED)
def create_application(
    data: ApplicationInput,
    own: OwnershipService = Depends(ownership),
    p: Principal = Depends(require(Role.ADMIN)),
) -> dict[str, Any]:
    return _call(own.save_application, data, p.username)  # type: ignore[no-any-return]


@admin.put("/applications/{application_id}")
def update_application(
    application_id: int,
    data: ApplicationInput,
    own: OwnershipService = Depends(ownership),
    p: Principal = Depends(require(Role.ADMIN)),
) -> dict[str, Any]:
    return _call(own.save_application, data, p.username, application_id)  # type: ignore[no-any-return]


@admin.post("/screens", status_code=status.HTTP_201_CREATED)
def create_screen(
    data: ScreenInput, own: OwnershipService = Depends(ownership), p: Principal = Depends(require(Role.ADMIN))
) -> dict[str, Any]:
    return _call(own.save_screen, data, p.username)  # type: ignore[no-any-return]


@admin.put("/screens/{screen_id}")
def update_screen(
    screen_id: int,
    data: ScreenInput,
    own: OwnershipService = Depends(ownership),
    p: Principal = Depends(require(Role.ADMIN)),
) -> dict[str, Any]:
    return _call(own.save_screen, data, p.username, screen_id)  # type: ignore[no-any-return]


class BulkOwner(BaseModel):
    screen_ids: list[int] = Field(min_length=1, max_length=1000)
    owner_team_id: int


@admin.post("/screens/bulk-owner")
def bulk_owner(
    body: BulkOwner, own: OwnershipService = Depends(ownership), p: Principal = Depends(require(Role.ADMIN))
) -> dict[str, int]:
    return {"changed": _call(own.bulk_set_screen_owner, body.screen_ids, body.owner_team_id, p.username)}


@admin.get("/rules")
def admin_rules(
    own: OwnershipService = Depends(ownership), _: Principal = Depends(require(Role.ADMIN))
) -> list[dict[str, Any]]:
    return own.rules()


@admin.post("/rules", status_code=status.HTTP_201_CREATED)
def create_rule(
    data: RuleInput, own: OwnershipService = Depends(ownership), p: Principal = Depends(require(Role.ADMIN))
) -> dict[str, Any]:
    return _call(own.save_rule, data, p.username)  # type: ignore[no-any-return]


@admin.put("/rules/{rule_id}")
def update_rule(
    rule_id: int,
    data: RuleInput,
    own: OwnershipService = Depends(ownership),
    p: Principal = Depends(require(Role.ADMIN)),
) -> dict[str, Any]:
    return _call(own.save_rule, data, p.username, rule_id)  # type: ignore[no-any-return]


class RuleOrder(BaseModel):
    rule_ids: list[int] = Field(min_length=1, max_length=1000)


@admin.post("/rules/order")
def reorder_rules(
    body: RuleOrder, own: OwnershipService = Depends(ownership), p: Principal = Depends(require(Role.ADMIN))
) -> dict[str, str]:
    _call(own.reorder_rules, body.rule_ids, p.username)
    return {"status": "ok"}


@admin.post("/rules/preview")
def preview_rule(
    data: RuleInput,
    days: int = Query(default=30, ge=1, le=365),
    own: OwnershipService = Depends(ownership),
    _: Principal = Depends(require(Role.ADMIN)),
) -> dict[str, Any]:
    return dict(own.preview_rule(data, days=days).__dict__)


class ActiveFlag(BaseModel):
    active: bool


@admin.post("/{entity}/{entity_id}/active")
def set_active(
    entity: Entity,
    entity_id: int,
    body: ActiveFlag,
    own: OwnershipService = Depends(ownership),
    p: Principal = Depends(require(Role.ADMIN)),
) -> dict[str, Any]:
    return _call(own.set_active, entity, entity_id, body.active, p.username)  # type: ignore[no-any-return]


@admin.delete("/{entity}/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entity(
    entity: Entity,
    entity_id: int,
    own: OwnershipService = Depends(ownership),
    p: Principal = Depends(require(Role.ADMIN)),
) -> None:
    _call(own.delete, entity, entity_id, p.username)


@admin.get("/export.csv", response_class=PlainTextResponse)
def export_csv(
    own: OwnershipService = Depends(ownership), _: Principal = Depends(require(Role.ADMIN))
) -> PlainTextResponse:
    return PlainTextResponse(
        own.export_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="ekran-sahipligi.csv"'},
    )


@admin.post("/import")
async def import_csv(
    request: Request,
    dry_run: bool = Query(default=False),
    own: OwnershipService = Depends(ownership),
    p: Principal = Depends(require(Role.ADMIN)),
) -> dict[str, Any]:
    """Body: CSV text (``application_key,screen_key,title,route_pattern,owner_team_key``)."""
    body = await request.body()
    if len(body) > MAX_CSV_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "CSV too large")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "CSV must be UTF-8") from exc
    report = _call(own.import_csv, text, p.username, dry_run=dry_run)
    if report.errors:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, report.as_dict())
    return report.as_dict()  # type: ignore[no-any-return]


@admin.get("/changes")
def changes(
    entity: Entity | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    own: OwnershipService = Depends(ownership),
    _: Principal = Depends(require(Role.ADMIN)),
) -> list[dict[str, Any]]:
    return own.changes(limit=limit, entity=entity)

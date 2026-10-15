"""FastAPI dependencies: application services and role-based authorization."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from branchops.application.interfaces.source_adapter import SourceRegistry
from branchops.application.queries.read_models import ReadModels
from branchops.config.settings import Settings
from branchops.domain.enums import Role
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.security.auth import AuthenticationError, Principal, resolve_principal
from branchops.templates.registry import TemplateRegistry


@dataclass
class AppServices:
    settings: Settings
    db: Database
    registry: TemplateRegistry
    read_models: ReadModels
    sources: SourceRegistry


def services(request: Request) -> AppServices:
    svc: AppServices = request.app.state.services
    return svc


def db_session(svc: AppServices = Depends(services)) -> Iterator[Session]:
    with svc.db.session() as session:
        yield session


def current_principal(request: Request, svc: AppServices = Depends(services)) -> Principal:
    try:
        principal = resolve_principal(
            request.headers, request.client.host if request.client else None, svc.settings.security
        )
    except AuthenticationError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    request.state.principal = principal
    return principal


def require(role: Role) -> Callable[..., Principal]:
    def checker(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.has(role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=f"role {role.value} required")
        return principal

    return checker

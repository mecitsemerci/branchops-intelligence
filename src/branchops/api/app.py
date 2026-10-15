"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from branchops.api.dependencies import AppServices
from branchops.api.middleware import CorrelationIdMiddleware, SecurityHeadersMiddleware
from branchops.api.routes import health, issues, webhooks
from branchops.application.queries.read_models import ReadModels
from branchops.config.settings import Settings
from branchops.container import Container
from branchops.infrastructure.observability.telemetry import setup_telemetry

logger = logging.getLogger(__name__)


def create_app(settings: Settings, container: Container | None = None) -> FastAPI:
    setup_telemetry(settings, role="api")
    container = container or Container.build(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        container.close()

    app = FastAPI(
        title="BranchOps Intelligence API",
        version="0.1.0",
        lifespan=lifespan,
        # Interactive docs load JS/CSS from public CDNs by default; disabled for the air-gapped deployment.
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.services = AppServices(
        settings=settings,
        db=container.db,
        registry=container.registry,
        read_models=ReadModels(settings.scoring.low_quality_threshold),
        sources=container.sources,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(issues.router)
    return app

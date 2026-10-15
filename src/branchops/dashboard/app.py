"""NiceGUI dashboard process.

All assets (Vue, Quasar, ECharts, Tailwind runtime, icons) are bundled with NiceGUI and served from the
dashboard itself; no CDN, web fonts or NiceGUI On Air relay are used.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import Request
from fastapi.responses import Response
from nicegui import app, ui

from branchops.application.commands.attachments import AttachmentService
from branchops.application.commands.ownership import OwnershipService
from branchops.application.queries.read_models import ReadModels
from branchops.config.settings import Settings
from branchops.dashboard.context import DashboardContext
from branchops.dashboard.pages import admin, analytics, executions, issues, live, overview
from branchops.dashboard.pages.attachments import serve_attachment
from branchops.infrastructure.attachments import build_attachment_store
from branchops.infrastructure.database.session import Database, create_db_engine
from branchops.infrastructure.observability.telemetry import setup_telemetry
from branchops.templates.registry import TemplateRegistry


def build_dashboard(settings: Settings) -> DashboardContext:
    ctx = DashboardContext(
        settings=settings,
        db=Database(create_db_engine(settings.database)),
        registry=TemplateRegistry.from_directory(Path(settings.templates.directory)),
        read=ReadModels(settings.scoring.low_quality_threshold),
    )
    ctx.ownership = OwnershipService(ctx.db, settings)
    ctx.attachments = AttachmentService(settings, build_attachment_store(settings))
    for module in (live, overview, issues, analytics, executions, admin):
        module.register(ctx)

    @app.get("/attachments/{attachment_id}")
    def attachment(attachment_id: str, request: Request, download: bool = False) -> Response:
        """Attachment content for VIEWERs (audited); images inline, everything else as a download."""
        return serve_attachment(ctx, request, attachment_id, download=download)

    @app.get("/health/live")
    def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        ctx.db.ping()
        return {"status": "ready"}

    app.on_shutdown(ctx.db.engine.dispose)
    return ctx


def run_dashboard(settings: Settings) -> None:
    setup_telemetry(settings, role="dashboard")
    build_dashboard(settings)
    storage_secret = os.environ.get(settings.dashboard.storage_secret_env) or secrets.token_urlsafe(32)
    ui.run(
        host=settings.dashboard.host,
        port=settings.dashboard.port,
        title=settings.dashboard.title,
        language="tr",
        reload=False,
        show=False,
        on_air=None,
        storage_secret=storage_secret,
        show_welcome_message=False,
        fastapi_docs=False,
        uvicorn_logging_level="warning",
    )

"""Attachments and in-app context on the incident detail page, and the audited download endpoint."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import Response
from nicegui import ui
from sqlalchemy import select

from branchops.dashboard.components.layout import load
from branchops.dashboard.context import DashboardContext
from branchops.domain.enums import Role
from branchops.infrastructure.database.models import IncidentAppContext
from branchops.infrastructure.persistence.audit import audit
from branchops.infrastructure.security.auth import AuthenticationError, resolve_principal


def serve_attachment(
    ctx: DashboardContext, request: Request, attachment_id: str, *, download: bool
) -> Response:
    try:
        principal = resolve_principal(
            request.headers, request.client.host if request.client else None, ctx.settings.security
        )
    except AuthenticationError:
        return Response(status_code=401)
    if not principal.has(Role.VIEWER) or ctx.attachments is None:
        return Response(status_code=403)
    service = ctx.attachments
    with ctx.db.session() as s:
        loaded = service.load(s, attachment_id)
        if loaded is None:
            return Response(status_code=404)
        meta, data = loaded
        audit(
            s,
            actor=principal.username,
            action="attachment_downloaded" if download else "attachment_viewed",
            resource_type="attachment",
            resource_id=meta["id"],
            details={"incident_key": meta["incident_key"], "via": "dashboard"},
        )
    inline = meta["content_type"].startswith("image/") and not download
    disposition = "inline" if inline else "attachment"
    return Response(
        content=data,
        media_type=meta["content_type"],
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(meta['filename'])}",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
        },
    )


def _context(ctx: DashboardContext, incident_key: str) -> dict[str, Any] | None:
    with ctx.db.session() as s:
        row = s.execute(
            select(IncidentAppContext).where(IncidentAppContext.incident_key == incident_key)
        ).scalar_one_or_none()
        return {"context": row.context, "security_flags": row.security_flags} if row else None


def format_bytes(size: int) -> str:
    return f"{size / 1024:.0f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"


async def attachments_panel(ctx: DashboardContext, incident_key: str) -> None:
    """Attachment previews/downloads and the masked in-app context (shown only when present)."""
    service = ctx.attachments
    items: list[dict[str, Any]] = []
    if service is not None:
        items = await load(lambda: ctx.query(lambda s: service.list_for_incident(s, incident_key)))
    app_ctx = await load(lambda: _context(ctx, incident_key))
    if not items and app_ctx is None:
        ui.label("Bu kayıtta ek veya uygulama bağlamı yok.")
        return
    if items:
        ui.label("Ekler (modele gönderilmez; görüntüleme ve indirme denetim kaydına yazılır)").classes(
            "text-sm text-slate-500"
        )
        with ui.row().classes("gap-4 flex-wrap"):
            for a in items:
                with ui.card().classes("w-72"):
                    url = f"/attachments/{a['id']}"
                    if a["content_type"].startswith("image/"):
                        ui.image(url).classes("w-full h-40 object-contain bg-slate-100")
                    else:
                        ui.icon("description").classes("text-5xl text-slate-400")
                    ui.label(a["filename"]).classes("font-semibold truncate")
                    blurred = f" · {a['blurred_regions']} alan bulanık" if a["blurred_regions"] else ""
                    ui.label(f"{a['kind']} · {format_bytes(a['size_bytes'])}{blurred}").classes(
                        "text-xs text-slate-500"
                    )
                    ui.link("İndir", f"{url}?download=true").props("download")
    if app_ctx is not None:
        if app_ctx["security_flags"]:
            ui.label(
                "Uygulama bağlamında güvenlik uyarıları: " + ", ".join(app_ctx["security_flags"])
            ).classes("text-orange-700")
        with ui.expansion("Uygulama bağlamı (maskeli)", icon="apps").classes("w-full"):
            ui.code(json.dumps(app_ctx["context"], ensure_ascii=False, indent=2), language="json").classes(
                "w-full"
            )

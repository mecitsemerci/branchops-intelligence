"""Bildirimler (/bildirimler): notification history, status, dead letters and resend (ADMIN)."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from nicegui import ui

from branchops.application.commands.notifications import resend
from branchops.application.queries.routing import notification_log
from branchops.dashboard.components.layout import authorize, frame, issue_url, load
from branchops.dashboard.context import DashboardContext
from branchops.domain.enums import Role
from branchops.infrastructure.security.auth import Principal

STATUSES = ["PENDING", "PROCESSING", "DONE", "DEAD_LETTER"]
STATUS_BADGE = {"PENDING": "info", "PROCESSING": "warning", "DONE": "positive", "DEAD_LETTER": "negative"}
EVENT_LABELS = {
    "incident_reported": "Yeni bildirim",
    "analysis_ready": "Analiz tamamlandı",
    "ownership_unresolved": "Sahipsiz ekran",
    "update_added": "Güncelleme",
    "test": "Test",
}
LOG_COLUMNS = [
    {"name": k, "label": label, "field": k, "align": "left"}
    for k, label in (
        ("created_at", "Zaman"),
        ("incident_key", "Kayıt"),
        ("event", "Olay"),
        ("channel", "Kanal"),
        ("recipient", "Alıcı"),
        ("team_key", "Ekip"),
        ("role", "Rol"),
        ("status", "Durum"),
        ("attempts", "Deneme"),
        ("off_hours", "Mesai dışı"),
        ("last_error", "Hata"),
    )
]


def log_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Display rows: Turkish event names, To/CC role, yes/blank flags."""
    return [
        {
            **r,
            "event": EVENT_LABELS.get(r["event"], r["event"]),
            "role": "CC" if r.get("role") == "cc" else "To",
            "off_hours": "evet" if r.get("off_hours") else "",
            "last_error": r.get("last_error") or "",
        }
        for r in items
    ]


def navigable(incident_key: str | None) -> bool:
    return bool(incident_key) and not str(incident_key).startswith("test:")


def log_table(
    ctx: DashboardContext,
    principal: Principal,
    rows: list[dict[str, Any]],
    on_change: Any,
) -> None:
    columns = [*LOG_COLUMNS]
    is_admin = principal.has(Role.ADMIN)
    if is_admin:
        columns.append({"name": "actions", "label": "", "field": "id"})
    table = ui.table(columns=columns, rows=log_rows(rows), row_key="id").classes("w-full")
    table.add_slot(
        "body-cell-status",
        """<q-td :props="props"><q-badge :color="{PENDING:'info',PROCESSING:'warning',DONE:'positive',
        DEAD_LETTER:'negative'}[props.value]">{{ props.value }}</q-badge></q-td>""",
    )
    table.add_slot(
        "body-cell-incident_key",
        """<q-td :props="props"><a v-if="props.value && !props.value.startsWith('test:')" class="text-primary
        cursor-pointer" @click="$parent.$emit('open', props.value)">{{ props.value }}</a>
        <span v-else>{{ props.value }}</span></q-td>""",
    )
    table.on("open", lambda e: ui.navigate.to(issue_url(e.args)) if navigable(e.args) else None)
    if is_admin:
        table.add_slot(
            "body-cell-actions",
            """<q-td :props="props"><q-btn flat dense icon="replay" label="Yeniden gönder"
            @click="$parent.$emit('resend', props.value)" /></q-td>""",
        )

        async def do_resend(e: Any) -> None:
            notification_id = str(e.args)

            def requeue() -> bool:
                with ctx.db.session() as s:
                    return resend(s, notification_id, principal.username)

            if await load(requeue):
                ui.notify("Bildirim yeniden kuyruğa alındı", type="positive")
                await on_change()
            else:
                ui.notify("Bildirim yeniden gönderilemedi", type="negative")

        table.on("resend", do_resend)


def register(ctx: DashboardContext) -> None:
    @ui.page("/bildirimler")
    async def notifications_page(request: Request) -> None:
        principal = authorize(ctx, request)
        if principal is None:
            return
        with frame(ctx, principal, "Bildirimler"):
            with ui.tabs() as tabs:
                t_all = ui.tab("Geçmiş")
                t_dead = ui.tab("Ölü mektuplar")
            with ui.tab_panels(tabs, value=t_all).classes("w-full"):
                with ui.tab_panel(t_all):
                    with ui.row().classes("items-end gap-3"):
                        key = ui.input("Kayıt anahtarı").classes("w-64")
                        status = ui.select([None, *STATUSES], label="Durum", value=None).classes("w-44")
                        ui.button("Filtrele", icon="filter_alt", on_click=lambda: refresh())  # noqa: PLW0108 - defined below
                    all_box = ui.column().classes("w-full")
                with ui.tab_panel(t_dead):
                    dead_box = ui.column().classes("w-full")

            async def refresh() -> None:
                k = (key.value or "").strip() or None
                st = status.value
                rows, dead = await load(
                    lambda: ctx.query(
                        lambda s: (
                            notification_log(s, incident_key=k, status=st),
                            notification_log(s, status="DEAD_LETTER"),
                        )
                    )
                )
                all_box.clear()
                with all_box:
                    log_table(ctx, principal, rows, refresh)
                dead_box.clear()
                with dead_box:
                    if dead:
                        log_table(ctx, principal, dead, refresh)
                    else:
                        ui.label("Ölü mektup yok.").classes("text-slate-500")

            await refresh()

"""Issue detail: routing decision (owner, CC, match level), notification history and re-route (ANALYST)."""

from __future__ import annotations

from typing import Any

from nicegui import ui

from branchops.application.queries.routing import notification_log
from branchops.dashboard.components.layout import load
from branchops.dashboard.context import DashboardContext
from branchops.dashboard.pages.notifications import log_rows
from branchops.domain.enums import Role
from branchops.infrastructure.security.auth import Principal

NO_ROUTING = "Bu kayıt için yönlendirme yok (yalnızca uygulama içinden bildirilen kayıtlar yönlendirilir)."
HISTORY_COLUMNS = [
    {"name": k, "label": label, "field": k, "align": "left"}
    for k, label in (
        ("created_at", "Zaman"),
        ("event", "Olay"),
        ("channel", "Kanal"),
        ("recipient", "Alıcı"),
        ("role", "Rol"),
        ("status", "Durum"),
        ("attempts", "Deneme"),
        ("last_error", "Hata"),
        ("off_hours", "Mesai dışı"),
    )
]


def team_list(names: list[str] | None) -> str:
    return ", ".join(names or []) or "—"


async def routing_panel(ctx: DashboardContext, principal: Principal, incident_key: str) -> None:
    box = ui.column().classes("w-full gap-3")

    async def render() -> None:
        router = ctx.router
        routing: dict[str, Any] | None = (
            await load(lambda: router.routing_of(incident_key)) if router else None
        )
        history = await load(lambda: ctx.query(lambda s: notification_log(s, incident_key=incident_key)))
        box.clear()
        with box:
            if routing is None:
                ui.label(NO_ROUTING).classes("text-slate-500")
            else:
                with ui.row().classes("gap-6 flex-wrap items-start"):
                    for label, value in (
                        ("Sahip ekip", routing.get("owner_team") or "—"),
                        ("Doğrudan alıcılar", team_list(routing.get("to_teams"))),
                        ("CC ekipler", team_list(routing.get("cc_teams"))),
                        ("Eşleşme seviyesi", routing.get("matched_level_tr") or routing.get("matched_level")),
                        ("İlk bildirim", routing.get("first_notified_at") or "—"),
                        ("Karar", f"{routing.get('decided_by')} · {routing.get('decided_at') or ''}"),
                    ):
                        with ui.column().classes("gap-0"):
                            ui.label(label).classes("text-xs text-slate-500")
                            ui.label(str(value)).classes("font-semibold")
                with ui.row().classes("gap-2"):
                    if routing.get("unowned_screen"):
                        ui.badge("Sahipsiz ekran", color="negative")
                    if routing.get("screen_unregistered"):
                        ui.badge("Kayıtsız ekran", color="warning")
                    key = f"{routing.get('application_key') or '—'}/{routing.get('screen_key') or '—'}"
                    ui.badge(key, color="grey").props("outline")
                if routing.get("reasons"):
                    with ui.expansion("Gerekçeler", icon="info").classes("w-full"):
                        for reason in routing["reasons"]:
                            ui.label(reason).classes("text-sm font-mono")
                if principal.has(Role.ANALYST) and router is not None:

                    async def reroute() -> None:
                        result = await load(lambda: router.reroute(incident_key, principal.username))
                        owner = result.get("owner_team_name") or result.get("owner_team") or "—"
                        ui.notify(f"Yeniden yönlendirildi: {owner}", type="positive")
                        await render()

                    ui.button("Yeniden yönlendir", icon="alt_route", on_click=reroute).props("outline")
            ui.label("Bildirim geçmişi").classes("font-semibold mt-2")
            if history:
                table = ui.table(columns=HISTORY_COLUMNS, rows=log_rows(history), row_key="id").classes(
                    "w-full"
                )
                table.add_slot(
                    "body-cell-status",
                    """<q-td :props="props"><q-badge :color="{PENDING:'info',PROCESSING:'warning',
                    DONE:'positive',DEAD_LETTER:'negative'}[props.value]">{{ props.value }}</q-badge></q-td>""",
                )
            else:
                ui.label("Bildirim yok.").classes("text-slate-500")

    await render()

"""Yönetim (/yonetim, ADMIN): teams, applications & screens, routing rules, "who owns?" and the change log.

Every write goes through :class:`OwnershipService`, which records an ``ownership_changes`` row.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from nicegui import events, ui
from pydantic import ValidationError

from branchops.application.commands.notifications import NotificationService, send_test
from branchops.application.commands.ownership import OwnershipService
from branchops.application.queries.routing import unowned_screens
from branchops.dashboard.components.layout import authorize, frame, load
from branchops.dashboard.context import DashboardContext
from branchops.domain.entities.ownership import (
    CHANNELS,
    ApplicationInput,
    RoutingInput,
    RuleInput,
    RuleMatch,
    ScreenInput,
    TeamInput,
    TeamKind,
)
from branchops.domain.enums import Role
from branchops.infrastructure.persistence.ownership_repository import OwnershipError, OwnershipRepository
from branchops.infrastructure.security.auth import Principal

TEAM_COLUMNS = [
    {"name": k, "label": label, "field": k, "align": "left"}
    for k, label in (
        ("key", "Anahtar"),
        ("name", "Ad"),
        ("kind", "Tür"),
        ("email", "E-posta"),
        ("zoom_channel_id", "Zoom kanal"),
        ("jira_project_key", "Jira projesi"),
        ("oncall_contact", "Nöbetçi"),
        ("working_hours", "Mesai"),
        ("active", "Aktif"),
        ("actions", ""),
    )
]
WHO_COLUMNS = [
    {"name": k, "label": label, "field": k, "align": "left"}
    for k, label in (
        ("application", "Uygulama"),
        ("screen", "Ekran"),
        ("route_pattern", "Rota"),
        ("owner", "Sahip ekip"),
        ("email", "E-posta"),
        ("oncall", "Nöbetçi"),
    )
]
CHANGE_COLUMNS = [
    {"name": k, "label": label, "field": k, "align": "left"}
    for k, label in (
        ("occurred_at", "Zaman"),
        ("actor", "Kişi"),
        ("entity", "Varlık"),
        ("entity_id", "Id"),
        ("action", "İşlem"),
        ("before", "Önce"),
        ("after", "Sonra"),
    )
]
ENTITY_LABELS = {"team": "Takım", "application": "Uygulama", "screen": "Ekran", "rule": "Kural"}


# --------------------------------------------------------------------------------------------- helpers
def parse_tags(text: str | None) -> list[str]:
    """``"kasa, eft ,  ,kasa"`` -> ``["kasa", "eft"]``."""
    return list(dict.fromkeys(t.strip() for t in (text or "").split(",") if t.strip()))


def blank_to_none(value: Any) -> Any:
    return None if isinstance(value, str) and not value.strip() else value


def shorten_json(value: Any, limit: int = 120) -> str:
    if value is None:
        return "—"
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def moved(ids: list[int], index: int, delta: int) -> list[int]:
    """Order after moving ``ids[index]`` by ``delta`` positions (clamped)."""
    target = max(0, min(len(ids) - 1, index + delta))
    out = list(ids)
    out.insert(target, out.pop(index))
    return out


def error_text(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors())
    return str(exc)


def tree_nodes(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Applications -> screens for ``ui.tree`` with owner team keys in the labels."""
    nodes = []
    for app in tree:
        owner = app.get("owner_team_key") or "?"
        default = app.get("default_screen_owner_team_key")
        label = f"{app['name']} ({app['key']}) — sahip: {owner}" + (
            f", ekran varsayılanı: {default}" if default else ""
        )
        nodes.append(
            {
                "id": f"app:{app['id']}",
                "label": label + ("" if app["active"] else " [pasif]"),
                "children": [
                    {
                        "id": f"screen:{sc['id']}",
                        "label": f"{sc['title']} ({sc['key']}) — {sc.get('owner_team_key') or '?'}"
                        + (f" · {sc['route_pattern']}" if sc.get("route_pattern") else "")
                        + ("" if sc["active"] else " [pasif]"),
                    }
                    for sc in app.get("screens", [])
                ],
            }
        )
    return nodes


def rule_summary(rule: dict[str, Any], team_keys: dict[int, str]) -> str:
    conditions = ", ".join(f"{k}={v}" for k, v in sorted(rule["match"].items()) if v)
    to = ", ".join(team_keys.get(t, str(t)) for t in rule["notify_to_team_ids"]) or "—"
    cc = ", ".join(team_keys.get(t, str(t)) for t in rule["cc_team_ids"]) or "—"
    return f"{conditions} → kime: {to} · bilgi: {cc} · kanal: {', '.join(rule['channels']) or '—'}"


def team_options(teams: list[dict[str, Any]]) -> dict[int, str]:
    return {t["id"]: f"{t['name']} ({t['key']})" + ("" if t["active"] else " [pasif]") for t in teams}


async def attempt[T](fn: Callable[[], T], success: str | None = None) -> T | None:
    """Run a service call off the event loop; show validation/registry errors instead of raising."""
    try:
        result = await load(fn)
    except (OwnershipError, ValidationError) as exc:
        ui.notify(error_text(exc), type="negative", multi_line=True)
        return None
    if success:
        ui.notify(success, type="positive")
    return result


def test_notification_button(ctx: DashboardContext, team: dict[str, Any], actor: str) -> None:
    """Enqueue a test message to every configured channel (e-mail / Zoom) of the team."""

    async def send() -> None:
        service = ctx.notifications or NotificationService(ctx.settings)

        def enqueue() -> int:
            with ctx.db.session() as s:
                registry = OwnershipRepository().load_registry(s)
                return send_test(s, service, registry, int(team["id"]), actor)

        count = await load(enqueue)
        if count:
            ui.notify(f"{count} bildirim kuyruğa alındı", type="positive")
        else:
            ui.notify("Ekipte e-posta/Zoom kanalı tanımlı değil", type="warning")

    with ui.button("Test bildirimi", icon="campaign", on_click=send).props("flat dense"):
        ui.tooltip("Ekibin e-posta ve Zoom kanallarına test bildirimi gönderir")


UNOWNED_COLUMNS = [
    {"name": k, "label": label, "field": k, "align": "left"}
    for k, label in (
        ("application_key", "Uygulama"),
        ("screen_key", "Ekran"),
        ("screen_title", "Başlık"),
        ("route", "Rota"),
        ("incidents", "Kayıt"),
        ("fell_back_to_branch_ops", "Şube operasyona düşen"),
        ("last_seen", "Son görülme"),
        ("actions", ""),
    )
]


def unowned_row(row: dict[str, Any]) -> dict[str, Any]:
    """Table row with ``assignable`` = both keys present (an owner can be assigned)."""
    return {
        **row,
        "screen_title": row.get("screen_title") or "",
        "route": row.get("route") or "",
        "assignable": bool(row.get("application_key") and row.get("screen_key")),
    }


async def unowned_screens_card(ctx: DashboardContext, principal: Principal) -> None:
    """Screens named by reports that fell back to branch operations or are not registered."""
    service = ctx.ownership or OwnershipService(ctx.db, ctx.settings)
    actor = principal.username
    with ui.card().classes("w-full"):
        ui.label("Sahipsiz ekranlar").classes("text-lg font-semibold")
        ui.label(
            "Son 90 günde sahibi bulunamayan veya kayıtlı olmayan ekranlardan gelen bildirimler."
        ).classes("text-sm text-slate-500")
        box = ui.column().classes("w-full")

    async def render() -> None:
        rows, teams_list, tree = await load(
            lambda: (ctx.query(lambda s: unowned_screens(s, days=90)), service.teams(), service.tree())
        )
        teams = team_options([t for t in teams_list if t["active"]])
        apps = {a["key"]: a for a in tree}
        box.clear()
        with box:
            if not rows:
                ui.label("Sahipsiz ekran yok.").classes("text-slate-500")
                return
            for row in (unowned_row(r) for r in rows):
                with ui.row().classes("items-center gap-3 w-full border-b py-1"):
                    ui.label(f"{row['application_key'] or '—'} / {row['screen_key'] or '—'}").classes(
                        "font-mono w-64"
                    )
                    ui.label(row["screen_title"] or "—").classes("w-48")
                    ui.label(row["route"] or "—").classes("w-48 text-slate-500")
                    ui.label(f"{row['incidents']} kayıt ({row['fell_back_to_branch_ops']} şube op.)")
                    ui.label(row["last_seen"] or "").classes("text-xs text-slate-400")
                    ui.space()
                    if row["assignable"]:
                        ui.button(
                            "Sahip ata",
                            icon="person_add",
                            on_click=lambda r=row: assign_dialog(r, teams, apps),
                        ).props("flat dense")
                    else:
                        with ui.button("Sahip ata", icon="person_add").props("flat dense disable"):
                            ui.tooltip("Uygulama/ekran anahtarı yok")

    def assign_dialog(row: dict[str, Any], teams: dict[int, str], apps: dict[str, dict[str, Any]]) -> None:
        with ui.dialog() as dialog, ui.card().classes("min-w-[28rem]"):
            ui.label(f"Sahip ata: {row['application_key']}/{row['screen_key']}").classes(
                "text-lg font-semibold"
            )
            team = ui.select(teams, label="Sahip ekip").classes("w-full")
            title = ui.input("Ekran başlığı", value=row["screen_title"] or row["screen_key"]).classes(
                "w-full"
            )
            route = ui.input("Rota kalıbı (isteğe bağlı)", placeholder="/eft/new").classes("w-full")
            if row["application_key"] not in apps:
                ui.label("Uygulama kayıtlı değil: seçilen ekip uygulama sahibi olarak da atanır.").classes(
                    "text-sm text-orange-700"
                )

            async def save() -> None:
                if team.value is None:
                    ui.notify("Ekip seçin", type="warning")
                    return
                owner = int(team.value)

                def create() -> int:
                    app = apps.get(row["application_key"])
                    if app is None:
                        app = service.save_application(
                            ApplicationInput(
                                key=row["application_key"], name=row["application_key"], owner_team_id=owner
                            ),
                            actor,
                        )
                    service.save_screen(
                        ScreenInput(
                            application_id=app["id"],
                            key=row["screen_key"],
                            title=title.value or row["screen_key"],
                            route_pattern=blank_to_none(route.value),
                            owner_team_id=owner,
                        ),
                        actor,
                    )
                    rerouted = 0
                    if ctx.router is not None:
                        for key in row["incident_keys"][:5]:
                            ctx.router.reroute(key, actor)
                            rerouted += 1
                    return rerouted

                rerouted = await attempt(create)
                if rerouted is not None:
                    ui.notify(f"Ekran kaydedildi, {rerouted} kayıt yeniden yönlendirildi", type="positive")
                    dialog.close()
                    await render()

            with ui.row():
                ui.button("Kaydet", on_click=save)
                ui.button("Vazgeç", on_click=dialog.close).props("flat")
        dialog.open()

    await render()


# --------------------------------------------------------------------------------------------- page
def register(ctx: DashboardContext) -> None:
    @ui.page("/yonetim")
    async def admin_page(request: Request) -> None:
        principal = authorize(ctx, request, Role.ADMIN)
        if principal is None:
            return
        service = ctx.ownership or OwnershipService(ctx.db, ctx.settings)
        with frame(ctx, principal, "Yönetim"):
            with ui.tabs().classes("w-full") as tabs:
                t_teams = ui.tab("Takımlar")
                t_apps = ui.tab("Uygulamalar & Ekranlar")
                t_rules = ui.tab("Yönlendirme kuralları")
                t_who = ui.tab("Kim sahip?")
                t_log = ui.tab("Değişiklik günlüğü")
            with ui.tab_panels(tabs, value=t_teams).classes("w-full"):
                with ui.tab_panel(t_teams):
                    await teams_panel(ctx, service, principal)
                with ui.tab_panel(t_apps):
                    await applications_panel(ctx, service, principal)
                with ui.tab_panel(t_rules):
                    await rules_panel(service, principal)
                with ui.tab_panel(t_who):
                    who_owns_panel(service)
                with ui.tab_panel(t_log):
                    await change_log_panel(service)


# --------------------------------------------------------------------------------------------- teams
def team_dialog(
    service: OwnershipService,
    actor: str,
    team: dict[str, Any] | None,
    on_saved: Callable[[], Awaitable[None]],
) -> None:
    t = team or {}
    with ui.dialog() as dialog, ui.card().classes("min-w-[560px]"):
        ui.label("Takımı düzenle" if team else "Yeni takım").classes("text-lg font-semibold")
        key = ui.input("Anahtar", value=t.get("key", "")).classes("w-full")
        name = ui.input("Ad", value=t.get("name", "")).classes("w-full")
        kind = ui.select(
            [k.value for k in TeamKind], label="Tür", value=t.get("kind", TeamKind.APP_OWNER.value)
        )
        email = ui.input("E-posta", value=t.get("email") or "").classes("w-full")
        zoom = ui.input("Zoom kanal kimliği", value=t.get("zoom_channel_id") or "").classes("w-full")
        zoom_ref = ui.input(
            "Zoom webhook sırrı (ortam değişkeni adı)", value=t.get("zoom_webhook_secret_ref") or ""
        ).classes("w-full")
        jira = ui.input("Jira proje anahtarı", value=t.get("jira_project_key") or "").classes("w-full")
        oncall = ui.input("Nöbetçi iletişim", value=t.get("oncall_contact") or "").classes("w-full")
        hours = ui.input(
            "Mesai saatleri (örn. Mon-Fri 08:30-17:30)", value=t.get("working_hours") or ""
        ).classes("w-full")
        active = ui.switch("Aktif", value=t.get("active", True))

        async def save() -> None:
            def build_and_save() -> dict[str, Any]:
                data = TeamInput(
                    key=key.value,
                    name=name.value,
                    kind=TeamKind(kind.value),
                    email=blank_to_none(email.value),
                    zoom_channel_id=blank_to_none(zoom.value),
                    zoom_webhook_secret_ref=blank_to_none(zoom_ref.value),
                    jira_project_key=blank_to_none(jira.value),
                    oncall_contact=blank_to_none(oncall.value),
                    working_hours=blank_to_none(hours.value),
                    active=active.value,
                )
                return service.save_team(data, actor, t.get("id"))

            if await attempt(build_and_save, "Takım kaydedildi") is not None:
                dialog.close()
                await on_saved()

        with ui.row():
            ui.button("Kaydet", on_click=save)
            ui.button("Vazgeç", on_click=dialog.close).props("flat")
    dialog.open()


def confirm(text: str, on_yes: Callable[[], Awaitable[None]]) -> None:
    with ui.dialog() as dialog, ui.card():
        ui.label(text)

        async def yes() -> None:
            dialog.close()
            await on_yes()

        with ui.row():
            ui.button("Evet", on_click=yes).props("color=negative")
            ui.button("Hayır", on_click=dialog.close).props("flat")
    dialog.open()


async def teams_panel(ctx: DashboardContext, service: OwnershipService, principal: Principal) -> None:
    actor = principal.username
    box = ui.column().classes("w-full gap-2")

    async def render() -> None:
        teams = await load(service.teams)
        box.clear()
        with box:
            ui.button("Yeni takım", icon="add", on_click=lambda: team_dialog(service, actor, None, render))
            if not teams:
                ui.label("Henüz takım yok.").classes("text-slate-500")
            for team in teams:
                with ui.card().classes("w-full"), ui.row().classes("items-center gap-4 w-full"):
                    with ui.column().classes("gap-0 grow"):
                        ui.label(f"{team['name']} ({team['key']}) · {team['kind']}").classes("font-semibold")
                        ui.label(
                            " · ".join(
                                f"{label}: {team[k]}"
                                for k, label in (
                                    ("email", "e-posta"),
                                    ("zoom_channel_id", "Zoom"),
                                    ("jira_project_key", "Jira"),
                                    ("oncall_contact", "nöbetçi"),
                                    ("working_hours", "mesai"),
                                )
                                if team.get(k)
                            )
                            or "iletişim bilgisi yok"
                        ).classes("text-sm text-slate-600")

                    async def toggle(
                        e: events.ValueChangeEventArguments[Any], team_id: int = team["id"]
                    ) -> None:
                        await attempt(lambda: service.set_active("team", team_id, bool(e.value), actor))
                        await render()

                    ui.switch("Aktif", value=team["active"], on_change=toggle)
                    test_notification_button(ctx, team, actor)
                    ui.button(
                        icon="edit", on_click=lambda t=team: team_dialog(service, actor, t, render)
                    ).props("flat dense")

                    async def remove(team_id: int = team["id"], key: str = team["key"]) -> None:
                        async def go() -> None:
                            await attempt(lambda: service.delete("team", team_id, actor), "Takım silindi")
                            await render()

                        confirm(f"'{key}' takımı silinsin mi?", go)

                    ui.button(icon="delete", on_click=remove).props("flat dense color=negative")

    await render()


# --------------------------------------------------------------------------------------------- applications
def application_dialog(
    service: OwnershipService,
    actor: str,
    teams: dict[int, str],
    app: dict[str, Any] | None,
    on_saved: Callable[[], Awaitable[None]],
) -> None:
    a = app or {}
    with ui.dialog() as dialog, ui.card().classes("min-w-[520px]"):
        ui.label("Uygulamayı düzenle" if app else "Yeni uygulama").classes("text-lg font-semibold")
        key = ui.input("Anahtar", value=a.get("key", "")).classes("w-full")
        name = ui.input("Ad", value=a.get("name", "")).classes("w-full")
        owner = ui.select(teams, label="Sahip ekip", value=a.get("owner_team_id")).classes("w-full")
        default_owner = ui.select(
            {0: "—", **teams},
            label="Ekranların varsayılan sahibi",
            value=a.get("default_screen_owner_team_id") or 0,
        ).classes("w-full")
        active = ui.switch("Aktif", value=a.get("active", True))

        async def save() -> None:
            def build_and_save() -> dict[str, Any]:
                data = ApplicationInput(
                    key=key.value,
                    name=name.value,
                    owner_team_id=owner.value,
                    default_screen_owner_team_id=default_owner.value or None,
                    active=active.value,
                )
                return service.save_application(data, actor, a.get("id"))

            if await attempt(build_and_save, "Uygulama kaydedildi") is not None:
                dialog.close()
                await on_saved()

        with ui.row():
            ui.button("Kaydet", on_click=save)
            ui.button("Vazgeç", on_click=dialog.close).props("flat")
    dialog.open()


def screen_dialog(
    service: OwnershipService,
    actor: str,
    teams: dict[int, str],
    apps: dict[int, str],
    screen: dict[str, Any] | None,
    on_saved: Callable[[], Awaitable[None]],
) -> None:
    sc = screen or {}
    with ui.dialog() as dialog, ui.card().classes("min-w-[560px]"):
        ui.label("Ekranı düzenle" if screen else "Yeni ekran").classes("text-lg font-semibold")
        app = ui.select(apps, label="Uygulama", value=sc.get("application_id")).classes("w-full")
        key = ui.input("Anahtar", value=sc.get("key", "")).classes("w-full")
        title = ui.input("Başlık", value=sc.get("title", "")).classes("w-full")
        route = ui.input("Rota deseni (örn. /eft/{id}/onay)", value=sc.get("route_pattern") or "").classes(
            "w-full"
        )
        owner = ui.select(teams, label="Sahip ekip", value=sc.get("owner_team_id")).classes("w-full")
        tags = ui.input("Etiketler (virgülle)", value=", ".join(sc.get("tags", []))).classes("w-full")
        active = ui.switch("Aktif", value=sc.get("active", True))

        async def save() -> None:
            def build_and_save() -> dict[str, Any]:
                data = ScreenInput(
                    application_id=app.value,
                    key=key.value,
                    title=title.value,
                    route_pattern=blank_to_none(route.value),
                    owner_team_id=owner.value,
                    tags=parse_tags(tags.value),
                    active=active.value,
                )
                return service.save_screen(data, actor, sc.get("id"))

            if await attempt(build_and_save, "Ekran kaydedildi") is not None:
                dialog.close()
                await on_saved()

        with ui.row():
            ui.button("Kaydet", on_click=save)
            ui.button("Vazgeç", on_click=dialog.close).props("flat")
    dialog.open()


async def applications_panel(ctx: DashboardContext, service: OwnershipService, principal: Principal) -> None:
    actor = principal.username
    box = ui.column().classes("w-full gap-3")

    async def render() -> None:
        tree, teams_list = await load(lambda: (service.tree(), service.teams()))
        teams = team_options(teams_list)
        apps = {a["id"]: f"{a['name']} ({a['key']})" for a in tree}
        by_node: dict[str, dict[str, Any]] = {f"app:{a['id']}": a for a in tree}
        by_node.update({f"screen:{sc['id']}": sc for a in tree for sc in a["screens"]})
        screen_choices = {
            sc["id"]: f"{a['key']}/{sc['key']} — {sc['title']}" for a in tree for sc in a["screens"]
        }
        box.clear()
        with box:
            with ui.row().classes("gap-2"):
                ui.button(
                    "Yeni uygulama",
                    icon="add",
                    on_click=lambda: application_dialog(service, actor, teams, None, render),
                )
                ui.button(
                    "Yeni ekran",
                    icon="add",
                    on_click=lambda: screen_dialog(service, actor, teams, apps, None, render),
                ).props("outline")

            def edit(e: events.ValueChangeEventArguments[str | None]) -> None:
                node = by_node.get(str(e.value)) if e.value else None
                if node is None:
                    return
                if str(e.value).startswith("app:"):
                    application_dialog(service, actor, teams, node, render)
                else:
                    screen_dialog(service, actor, teams, apps, node, render)

            if tree:
                ui.label("Düzenlemek için bir uygulamaya veya ekrana tıklayın.").classes(
                    "text-sm text-slate-500"
                )
                ui.tree(tree_nodes(tree), on_select=edit).expand()
            else:
                ui.label("Henüz uygulama yok.").classes("text-slate-500")

            with ui.card().classes("w-full"):
                ui.label("Toplu sahip değişikliği").classes("text-lg font-semibold")
                with ui.row().classes("items-end gap-3 w-full"):
                    picked = ui.select(screen_choices, multiple=True, label="Ekranlar").classes(
                        "min-w-96 grow"
                    )
                    new_owner = ui.select(teams, label="Yeni sahip").classes("w-64")

                    async def apply_bulk() -> None:
                        if not picked.value or new_owner.value is None:
                            ui.notify("Ekran ve ekip seçin", type="warning")
                            return
                        changed = await attempt(
                            lambda: service.bulk_set_screen_owner(list(picked.value), new_owner.value, actor)
                        )
                        if changed is not None:
                            ui.notify(f"{changed} ekranın sahibi değişti", type="positive")
                            await render()

                    ui.button("Uygula", on_click=apply_bulk)

            csv_card(service, actor, render)
            await unowned_screens_card(ctx, principal)

    await render()


def csv_card(service: OwnershipService, actor: str, on_done: Callable[[], Awaitable[None]]) -> None:
    with ui.card().classes("w-full"):
        ui.label("CSV içe / dışa aktarma").classes("text-lg font-semibold")
        ui.label("Sütunlar: application_key, screen_key, title, route_pattern, owner_team_key").classes(
            "text-sm text-slate-500"
        )
        dry_run = ui.checkbox("Önizleme (dry-run)", value=True)
        result = ui.column()

        async def on_upload(e: events.UploadEventArguments) -> None:
            text = await e.file.text()
            report = await attempt(lambda: service.import_csv(text, actor, dry_run=bool(dry_run.value)))
            result.clear()
            if report is None:
                return
            with result:
                ui.label(
                    ("Önizleme: " if report.dry_run else "")
                    + f"{report.created_applications} uygulama, {report.created_screens} yeni ekran, "
                    f"{report.updated_screens} güncellenen, {report.unchanged} değişmeyen"
                )
                for err in report.errors:
                    ui.label(err).classes("text-sm text-negative")
                if report.errors:
                    ui.label("Hata olduğu için hiçbir değişiklik uygulanmadı.").classes("text-negative")
            if not report.dry_run and not report.errors:
                await on_done()

        ui.upload(label="CSV yükle", auto_upload=True, on_upload=on_upload).props('accept=".csv"')

        async def export() -> None:
            content = await load(service.export_csv)
            ui.download(content.encode("utf-8"), "ekran-sahipligi.csv", "text/csv")

        ui.button("CSV dışa aktar", icon="download", on_click=export).props("outline")


# --------------------------------------------------------------------------------------------- rules
def rule_dialog(
    service: OwnershipService,
    actor: str,
    teams: dict[int, str],
    rule: dict[str, Any] | None,
    on_saved: Callable[[], Awaitable[None]],
) -> None:
    r = rule or {}
    m = r.get("match", {})
    with ui.dialog() as dialog, ui.card().classes("min-w-[640px]"):
        ui.label("Kuralı düzenle" if rule else "Yeni kural").classes("text-lg font-semibold")
        name = ui.input("Ad", value=r.get("name", "")).classes("w-full")
        priority = ui.number("Öncelik (küçük önce)", value=r.get("priority", 100), min=0, step=10)
        ui.label("Eşleşme koşulları (boş = koşulsuz; glob: * ?)").classes("text-sm text-slate-500")
        with ui.row().classes("w-full gap-2"):
            category = ui.input("Kategori", value=m.get("category") or "")
            app_key = ui.input("Uygulama anahtarı", value=m.get("application_key") or "")
            screen_key = ui.input("Ekran anahtarı", value=m.get("screen_key") or "")
            error_glob = ui.input("Hata kodu (glob)", value=m.get("error_code_glob") or "")
            branch_glob = ui.input("Şube kodu (glob)", value=m.get("branch_code_glob") or "")
        notify = ui.select(teams, multiple=True, label="Kime", value=r.get("notify_to_team_ids", [])).classes(
            "w-full"
        )
        cc = ui.select(teams, multiple=True, label="Bilgi (CC)", value=r.get("cc_team_ids", [])).classes(
            "w-full"
        )
        channels = ui.select(
            list(CHANNELS), multiple=True, label="Kanallar", value=r.get("channels", list(CHANNELS))
        )
        active = ui.switch("Aktif", value=r.get("active", True))
        preview_label = ui.label().classes("text-sm")

        def build() -> RuleInput:
            return RuleInput(
                name=name.value,
                priority=int(priority.value or 0),
                match=RuleMatch(
                    category=blank_to_none(category.value),
                    application_key=blank_to_none(app_key.value),
                    screen_key=blank_to_none(screen_key.value),
                    error_code_glob=blank_to_none(error_glob.value),
                    branch_code_glob=blank_to_none(branch_glob.value),
                ),
                notify_to_team_ids=list(notify.value or []),
                cc_team_ids=list(cc.value or []),
                channels=list(channels.value or []),
                active=active.value,
            )

        async def preview() -> None:
            result = await attempt(lambda: service.preview_rule(build(), days=30))
            if result is not None:
                sample = ", ".join(result.sample_keys)
                preview_label.text = (
                    f"Bu kural son {result.days} günde {result.matched} kaydı eşlerdi (taranan {result.scanned})"
                    + (f": {sample}" if sample else "")
                )

        async def save() -> None:
            if (
                await attempt(lambda: service.save_rule(build(), actor, r.get("id")), "Kural kaydedildi")
                is not None
            ):
                dialog.close()
                await on_saved()

        with ui.row():
            ui.button("Önizle", on_click=preview).props("outline")
            ui.button("Kaydet", on_click=save)
            ui.button("Vazgeç", on_click=dialog.close).props("flat")
    dialog.open()


async def rules_panel(service: OwnershipService, principal: Principal) -> None:
    actor = principal.username
    box = ui.column().classes("w-full gap-2")

    async def render() -> None:
        rules, teams_list = await load(lambda: (service.rules(), service.teams()))
        teams = team_options(teams_list)
        keys = {t["id"]: t["key"] for t in teams_list}
        ids = [r["id"] for r in rules]
        box.clear()
        with box:
            ui.button(
                "Yeni kural", icon="add", on_click=lambda: rule_dialog(service, actor, teams, None, render)
            )
            if not rules:
                ui.label("Henüz kural yok.").classes("text-slate-500")
            for index, rule in enumerate(rules):
                with ui.card().classes("w-full"), ui.row().classes("items-center gap-3 w-full"):

                    async def move(delta: int, index: int = index) -> None:
                        order = moved(ids, index, delta)
                        if order != ids:
                            await attempt(lambda: service.reorder_rules(order, actor))
                            await render()

                    ui.button(icon="arrow_upward", on_click=lambda: move(-1)).props("flat dense").set_enabled(
                        index > 0
                    )
                    ui.button(icon="arrow_downward", on_click=lambda: move(1)).props(
                        "flat dense"
                    ).set_enabled(index < len(rules) - 1)
                    with ui.column().classes("gap-0 grow"):
                        ui.label(f"{rule['priority']} · {rule['name']}").classes("font-semibold")
                        ui.label(rule_summary(rule, keys)).classes("text-sm text-slate-600")

                    async def toggle(
                        e: events.ValueChangeEventArguments[Any], rule_id: int = rule["id"]
                    ) -> None:
                        await attempt(lambda: service.set_active("rule", rule_id, bool(e.value), actor))
                        await render()

                    ui.switch("Etkin", value=rule["active"], on_change=toggle)
                    ui.button(
                        icon="edit", on_click=lambda r=rule: rule_dialog(service, actor, teams, r, render)
                    ).props("flat dense")

                    async def remove(rule_id: int = rule["id"], rule_name: str = rule["name"]) -> None:
                        async def go() -> None:
                            await attempt(lambda: service.delete("rule", rule_id, actor), "Kural silindi")
                            await render()

                        confirm(f"'{rule_name}' kuralı silinsin mi?", go)

                    ui.button(icon="delete", on_click=remove).props("flat dense color=negative")

    await render()


# --------------------------------------------------------------------------------------------- who owns / log
def who_owns_panel(service: OwnershipService) -> None:
    with ui.row().classes("items-end gap-3"):
        query = ui.input("Uygulama / ekran / rota ara").classes("w-96")
        search_btn = ui.button("Ara", icon="search")
    table = ui.table(columns=WHO_COLUMNS, rows=[], row_key="key").classes("w-full")

    async def search() -> None:
        if not (query.value or "").strip():
            return
        found = await load(lambda: service.who_owns(query.value))
        table.rows = [
            {
                "key": f"{r['application_key']}/{r['screen_key'] or '*'}",
                "application": f"{r['application_name']} ({r['application_key']})",
                "screen": f"{r['screen_title']} ({r['screen_key']})" if r["screen_key"] else "— (uygulama)",
                "route_pattern": r["route_pattern"] or "",
                "owner": (r["owner"] or {}).get("name", "?"),
                "email": (r["owner"] or {}).get("email") or "",
                "oncall": (r["owner"] or {}).get("oncall_contact") or "",
            }
            for r in found
        ]
        table.update()

    search_btn.on_click(search)
    query.on("keydown.enter", search)

    ui.separator()
    ui.label("Yönlendirme denemesi").classes("text-lg font-semibold")
    with ui.row().classes("items-end gap-3"):
        app_key = ui.input("Uygulama anahtarı")
        screen_key = ui.input("Ekran anahtarı")
        route = ui.input("Rota / URL").classes("w-80")
        resolve_btn = ui.button("Çözümle", icon="alt_route")
    result = ui.column()

    async def resolve() -> None:
        ctx_in = RoutingInput(
            app_key=blank_to_none(app_key.value),
            screen_key=blank_to_none(screen_key.value),
            route=blank_to_none(route.value),
        )
        described = await load(lambda: service.describe(service.resolve(ctx_in)))
        result.clear()
        with result:
            owner = described.get("owner") or {}
            ui.label(
                f"Sahip: {owner.get('name', '—')} ({owner.get('key', '—')}) · seviye: {described['matched_level']}"
            )
            ui.label("Kime: " + (", ".join(t["key"] for t in described["to"] if t) or "—"))
            ui.label("Bilgi (CC): " + (", ".join(t["key"] for t in described["cc"] if t) or "—"))
            if described["unowned_screen"]:
                ui.badge("sahipsiz ekran", color="warning")
            for reason in described["reasons"]:
                ui.label(f"• {reason}").classes("text-sm text-slate-600")

    resolve_btn.on_click(resolve)


async def change_log_panel(service: OwnershipService) -> None:
    entity = ui.select({"": "Tümü", **ENTITY_LABELS}, value="", label="Varlık").classes("w-48")
    table = ui.table(columns=CHANGE_COLUMNS, rows=[], row_key="id").classes("w-full")

    async def refresh() -> None:
        rows = await load(lambda: service.changes(limit=200, entity=entity.value or None))
        table.rows = [
            {
                **r,
                "entity": ENTITY_LABELS.get(r["entity"], r["entity"]),
                "before": shorten_json(r["before"]),
                "after": shorten_json(r["after"]),
            }
            for r in rows
        ]
        table.update()

    entity.on_value_change(refresh)
    ui.button("Yenile", icon="refresh", on_click=refresh).props("flat")
    await refresh()

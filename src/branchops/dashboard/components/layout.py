"""Shared page frame, auth guard and small UI helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar
from urllib.parse import quote

from fastapi import Request
from nicegui import run, ui

from branchops.dashboard.context import DashboardContext
from branchops.domain.enums import Role
from branchops.infrastructure.security.auth import AuthenticationError, Principal, resolve_principal

T = TypeVar("T")

NAV = [
    ("/live", "Canlı Akış", "sensors"),
    ("/", "Genel Bakış", "dashboard"),
    ("/issues", "Kayıt Gezgini", "search"),
    ("/analytics", "Kalite Analitiği", "insights"),
    ("/executions", "Ajan Çalıştırmaları", "memory"),
    ("/bildirimler", "Bildirimler", "notifications"),
    ("/yonetim", "Yönetim", "admin_panel_settings"),
]

STATUS_COLORS = {
    "COMPLETED": "positive",
    "PARTIAL": "warning",
    "NEEDS_REVIEW": "orange",
    "FAILED": "negative",
    "RUNNING": "info",
}
STATE_COLORS = {
    "SUFFICIENT": "positive",
    "INSUFFICIENT": "warning",
    "MISSING": "negative",
    "UNKNOWN": "grey",
    "NOT_APPLICABLE": "blue-grey-3",
}


def authorize(ctx: DashboardContext, request: Request, role: Role = Role.VIEWER) -> Principal | None:
    try:
        principal = resolve_principal(
            request.headers, request.client.host if request.client else None, ctx.settings.security
        )
    except AuthenticationError as exc:
        ui.label(f"Yetkisiz erişim: {exc}").classes("text-negative text-lg p-8")
        return None
    if not principal.has(role):
        ui.label(f"Bu sayfa için {role.value} rolü gerekir.").classes("text-negative text-lg p-8")
        return None
    return principal


@contextmanager
def frame(ctx: DashboardContext, principal: Principal, title: str) -> Iterator[None]:
    with ui.header().classes("items-center bg-slate-800 text-white px-4 py-2"):
        ui.label(ctx.settings.dashboard.title).classes("text-lg font-semibold mr-8")
        for path, label, icon in NAV:
            ui.button(label, icon=icon, on_click=lambda p=path: ui.navigate.to(p)).props(
                "flat color=white no-caps"
            )
        ui.space()
        roles = ", ".join(sorted(r.value for r in principal.roles))
        ui.label(f"{principal.username} ({roles})").classes("text-sm opacity-80")
    with ui.column().classes("w-full max-w-screen-2xl mx-auto p-4 gap-4"):
        ui.label(title).classes("text-2xl font-semibold")
        yield


async def load[T](fn: Callable[..., T], *args: Any) -> T:
    result = await run.io_bound(fn, *args)
    return result  # type: ignore[return-value]  # io_bound is typed Optional for shutdown cases


def kpi(label: str, value: Any, *, suffix: str = "", color: str = "text-slate-800") -> None:
    with ui.card().classes("min-w-44"):
        ui.label(label).classes("text-sm text-slate-500")
        shown = "—" if value is None else f"{value}{suffix}"
        ui.label(shown).classes(f"text-3xl font-bold {color}")


def score_color(score: float | None, threshold: float) -> str:
    if score is None:
        return "text-slate-400"
    if score < threshold:
        return "text-red-600"
    if score < 75:
        return "text-amber-600"
    return "text-green-700"


def status_badge(status: str | None) -> None:
    if status:
        ui.badge(status, color=STATUS_COLORS.get(status, "grey"))


def issue_url(incident_key: str) -> str:
    """Dashboard URL of an incident (keys may contain "/" and ":", e.g. ``csv:2026-10/row-17``)."""
    return "/issues/" + quote(incident_key, safe=":/@")

"""Dashboard routing / notification pages: registration and pure helpers."""

from __future__ import annotations

from branchops.dashboard.pages import analytics, notifications, routing_panel
from branchops.dashboard.pages.admin import unowned_row
from branchops.dashboard.pages.analytics import routing_kpis
from branchops.dashboard.pages.notifications import log_rows, navigable
from branchops.dashboard.pages.routing_panel import team_list


def test_modules_register() -> None:
    assert callable(notifications.register) and callable(analytics.register)
    assert callable(routing_panel.routing_panel)


def test_log_rows_and_navigation() -> None:
    rows = log_rows(
        [
            {"id": "1", "event": "incident_reported", "role": "cc", "off_hours": True, "last_error": None},
            {"id": "2", "event": "custom", "role": "to", "off_hours": False, "last_error": "x"},
        ]
    )
    assert [(r["event"], r["role"], r["off_hours"], r["last_error"]) for r in rows] == [
        ("Yeni bildirim", "CC", "evet", ""),
        ("custom", "To", "", "x"),
    ]
    assert navigable("app:RPT-1") and not navigable("test:eft-team") and not navigable(None)


def test_routing_kpis_and_team_list() -> None:
    kpis = routing_kpis(
        {"routed_incidents": 4, "median_minutes_to_first_notification": 1.5, "unowned_screen_rate": 0.25}
    )
    assert kpis == [
        ("Yönlendirilen kayıt", 4, ""),
        ("Medyan ilk bildirim süresi", 1.5, " dk"),
        ("Sahipsiz ekran oranı", 25.0, " %"),
    ]
    assert routing_kpis({"unowned_screen_rate": None})[2][1] is None
    assert team_list(["EFT", "Şube Op"]) == "EFT, Şube Op" and team_list(None) == "—"


def test_unowned_row_assignable() -> None:
    assert unowned_row({"application_key": "eft", "screen_key": "s", "screen_title": None})["assignable"]
    row = unowned_row({"application_key": None, "screen_key": "s"})
    assert not row["assignable"] and row["route"] == "" and row["screen_title"] == ""

"""Quality Analytics page."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from nicegui import ui

from branchops.application.queries.routing import routing_analytics
from branchops.dashboard.components.charts import bar_chart
from branchops.dashboard.components.layout import authorize, frame, kpi, load
from branchops.dashboard.context import DashboardContext
from branchops.domain.entities.issue import FIELD_LABELS_TR


def _table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], key: str) -> ui.table:
    return ui.table(
        columns=[{"name": f, "label": label, "field": f, "align": "left"} for f, label in columns],
        rows=rows,
        row_key=key,
    ).classes("w-full")


def routing_kpis(data: dict[str, Any]) -> list[tuple[str, Any, str]]:
    """(label, value, suffix) of the routing KPIs; rates as percentages."""
    rate = data.get("unowned_screen_rate")
    return [
        ("Yönlendirilen kayıt", data.get("routed_incidents"), ""),
        ("Medyan ilk bildirim süresi", data.get("median_minutes_to_first_notification"), " dk"),
        ("Sahipsiz ekran oranı", round(rate * 100, 1) if rate is not None else None, " %"),
    ]


def routing_section(data: dict[str, Any]) -> None:
    with ui.card().classes("w-full"):
        ui.label("Yönlendirme").classes("text-lg font-semibold")
        with ui.row().classes("gap-4 flex-wrap"):
            for label, value, suffix in routing_kpis(data):
                kpi(label, value, suffix=suffix)
        with ui.row().classes("w-full gap-4 flex-nowrap"):
            with ui.column().classes("w-1/2"):
                ui.label("Sahip ekibe göre kayıt").classes("font-semibold")
                _table(data["per_owner_team"], [("team_name", "Ekip"), ("incidents", "Kayıt")], "team_key")
            with ui.column().classes("w-1/2"):
                ui.label("En çok kayıt gelen ekranlar").classes("font-semibold")
                rows = [{**r, "id": f"{r['application_key']}/{r['screen_key']}"} for r in data["top_screens"]]
                _table(
                    rows,
                    [("application_key", "Uygulama"), ("screen_key", "Ekran"), ("incidents", "Kayıt")],
                    "id",
                )


def register(ctx: DashboardContext) -> None:
    @ui.page("/analytics")
    async def analytics_page(request: Request) -> None:
        principal = authorize(ctx, request)
        if principal is None:
            return
        with frame(ctx, principal, "Kalite Analitiği"):
            days = ui.select(
                {30: "Son 30 gün", 90: "Son 90 gün", 365: "Son 1 yıl"}, value=90, label="Dönem"
            ).classes("w-48")
            body = ui.column().classes("w-full gap-4")

            async def render() -> None:
                d = int(days.value)
                data = await load(lambda: ctx.query(lambda s: ctx.read.quality_analytics(s, d)))
                routing = await load(lambda: ctx.query(lambda s: routing_analytics(s, days=d)))
                body.clear()
                with body:
                    routing_section(routing)
                    with ui.row().classes("w-full gap-4 flex-nowrap"):
                        with ui.card().classes("w-1/2"):
                            ui.label("Kategoriye göre ortalama kalite").classes("font-semibold")
                            cats = data["by_category"]
                            ui.echart(
                                bar_chart(
                                    [c["category"] for c in cats],
                                    [c["avg_quality"] for c in cats],
                                    "Kalite",
                                    max_value=100,
                                )
                            ).classes("h-72 w-full")
                        with ui.card().classes("w-1/2"):
                            ui.label("En sık eksik alanlar").classes("font-semibold")
                            missing = list(reversed(data["most_missing_fields"]))
                            ui.echart(
                                bar_chart(
                                    [FIELD_LABELS_TR.get(m["field"], m["field"]) for m in missing],
                                    [m["count"] for m in missing],
                                    "Kayıt",
                                    horizontal=True,
                                )
                            ).classes("h-72 w-full")
                    with ui.card().classes("w-full"):
                        ui.label("Şubeye göre kalite").classes("font-semibold")
                        _table(
                            data["by_branch"],
                            [
                                ("branch_code", "Şube"),
                                ("issues", "Kayıt"),
                                ("avg_completeness", "Ort. tamlık"),
                                ("avg_quality", "Ort. kalite"),
                            ],
                            "branch_code",
                        )
                    with ui.card().classes("w-full"):
                        ui.label("Kalite iyileşmeleri (ilk → son analiz)").classes("font-semibold")
                        _table(
                            data["improvements"],
                            [
                                ("incident_key", "Kayıt"),
                                ("first_quality", "İlk kalite"),
                                ("last_quality", "Son kalite"),
                                ("quality_delta", "Fark"),
                                ("first_completeness", "İlk tamlık"),
                                ("last_completeness", "Son tamlık"),
                            ],
                            "incident_key",
                        )
                    with ui.card().classes("w-full"):
                        ui.label("Tekrarlayan sorunlar (aynı hata imzası)").classes("font-semibold")
                        rows = [
                            {**r, "incident_keys": ", ".join(r["incident_keys"])} for r in data["recurring"]
                        ]
                        _table(
                            rows,
                            [
                                ("error_signature", "Hata imzası"),
                                ("issues", "Kayıt"),
                                ("branches", "Şube sayısı"),
                                ("first_seen", "İlk"),
                                ("last_seen", "Son"),
                                ("sources", "Kaynak sayısı"),
                                ("incident_keys", "Kayıtlar"),
                            ],
                            "error_signature",
                        )

            days.on_value_change(render)
            await render()

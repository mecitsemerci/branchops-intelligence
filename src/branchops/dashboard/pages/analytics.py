"""Quality Analytics page."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from nicegui import ui

from branchops.dashboard.components.charts import bar_chart
from branchops.dashboard.components.layout import authorize, frame, load
from branchops.dashboard.context import DashboardContext
from branchops.domain.entities.issue import FIELD_LABELS_TR


def _table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], key: str) -> ui.table:
    return ui.table(
        columns=[{"name": f, "label": label, "field": f, "align": "left"} for f, label in columns],
        rows=rows,
        row_key=key,
    ).classes("w-full")


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
                body.clear()
                with body:
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
                                ("issue_key", "Kayıt"),
                                ("first_quality", "İlk kalite"),
                                ("last_quality", "Son kalite"),
                                ("quality_delta", "Fark"),
                                ("first_completeness", "İlk tamlık"),
                                ("last_completeness", "Son tamlık"),
                            ],
                            "issue_key",
                        )
                    with ui.card().classes("w-full"):
                        ui.label("Tekrarlayan sorunlar (aynı hata imzası)").classes("font-semibold")
                        rows = [{**r, "issue_keys": ", ".join(r["issue_keys"])} for r in data["recurring"]]
                        _table(
                            rows,
                            [
                                ("error_signature", "Hata imzası"),
                                ("issues", "Kayıt"),
                                ("branches", "Şube sayısı"),
                                ("first_seen", "İlk"),
                                ("last_seen", "Son"),
                                ("issue_keys", "Kayıtlar"),
                            ],
                            "error_signature",
                        )

            days.on_value_change(render)
            await render()

"""Overview page: KPIs, trends, category distribution and deterministic report."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from nicegui import ui

from branchops.agents.reporting import deterministic_report
from branchops.dashboard.components.charts import pie_chart, trend_chart
from branchops.dashboard.components.layout import authorize, frame, kpi, load, score_color
from branchops.dashboard.context import DashboardContext


def register(ctx: DashboardContext) -> None:
    @ui.page("/")
    async def overview_page(request: Request) -> None:
        principal = authorize(ctx, request)
        if principal is None:
            return
        threshold = ctx.settings.scoring.low_quality_threshold

        def fetch(days: int) -> dict[str, Any]:
            def q(s: Any) -> dict[str, Any]:
                ov = ctx.read.overview(s, days)
                missing = ctx.read.quality_analytics(s, days)["most_missing_fields"][:5]
                return {"overview": ov, "missing": missing}

            return ctx.query(q)

        with frame(ctx, principal, "Genel Bakış"):
            days_select = ui.select(
                {7: "Son 7 gün", 30: "Son 30 gün", 90: "Son 90 gün", 365: "Son 1 yıl"},
                value=30,
                label="Dönem",
            ).classes("w-48")
            body = ui.column().classes("w-full gap-4")

            async def render() -> None:
                data = await load(fetch, int(days_select.value))
                ov = data["overview"]
                body.clear()
                with body:
                    with ui.row().classes("gap-4 flex-wrap"):
                        kpi("Toplam kayıt", ov.total_issues)
                        kpi("Analiz edilen", ov.analysed_issues)
                        kpi(
                            "Ort. tamlık",
                            ov.avg_completeness,
                            color=score_color(ov.avg_completeness, threshold),
                        )
                        kpi("Ort. kalite", ov.avg_quality, color=score_color(ov.avg_quality, threshold))
                        kpi(f"Düşük kalite (<{threshold:g})", ov.low_quality_issues, color="text-red-600")
                        kpi("İnceleme bekleyen", ov.needs_review, color="text-orange-600")
                    with ui.row().classes("w-full gap-4 flex-nowrap"):
                        with ui.card().classes("w-2/3"):
                            ui.label("Kayıt eğilimi").classes("font-semibold")
                            ui.echart(trend_chart(ov.daily_trend)).classes("h-80 w-full")
                        with ui.card().classes("w-1/3"):
                            ui.label("Kategori dağılımı").classes("font-semibold")
                            ui.echart(pie_chart(ov.category_distribution, "category", "count")).classes(
                                "h-80 w-full"
                            )
                    metrics = {
                        "period_days": days_select.value,
                        "overview": {k: v for k, v in ov.__dict__.items() if k != "daily_trend"},
                        "most_missing_fields": data["missing"],
                    }
                    report = deterministic_report(metrics)
                    with ui.card().classes("w-full"):
                        ui.label(report.title).classes("font-semibold")
                        for h in report.highlights:
                            ui.label(f"• {h}")

            days_select.on_value_change(render)
            await render()

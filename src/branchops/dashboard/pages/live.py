"""Live agent activity: who is doing what, right now (auto-refresh)."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from nicegui import ui

from branchops.application.queries.read_models import WORKFLOW_NODES, live_activity
from branchops.dashboard.components.charts import NODE_COLORS, NODE_STATE_TR, workflow_graph
from branchops.dashboard.components.layout import STATUS_COLORS, authorize, frame, issue_url, kpi, load
from branchops.dashboard.context import DashboardContext

REFRESH_SECONDS = 2.0
STEP_LABELS = {node: label for node, label, _ in WORKFLOW_NODES}


def _ago(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} sn"
    if seconds < 3600:
        return f"{seconds // 60} dk {seconds % 60} sn"
    return f"{seconds // 3600} sa {(seconds % 3600) // 60} dk"


def _short_worker(worker_id: str | None) -> str:
    return (worker_id or "—").split(":")[0][-12:]


def _legend() -> None:
    with ui.row().classes("gap-3 items-center text-xs"):
        for state, color in NODE_COLORS.items():
            with ui.row().classes("items-center gap-1"):
                ui.element("div").style(f"width:12px;height:12px;border-radius:3px;background:{color}")
                ui.label(NODE_STATE_TR[state])
        ui.label("· yuvarlak = LLM ajanı (Gemma), kare = deterministik Python adımı").classes(
            "text-slate-500"
        )


def _steps_table(steps: list[dict[str, Any]]) -> None:
    rows = [
        {
            "step": s["step"],
            "status": s["status"],
            "duration": f"{s['duration_ms'] / 1000:.1f} sn",
            "attempts": s["attempts"],
            "mode": s["output_mode"] or "",
            "tokens": s["tokens"],
            "error": (s["error"] or "")[:160],
        }
        for s in steps
    ]
    ui.table(
        columns=[
            {"name": k, "label": label, "field": k, "align": "left"}
            for k, label in (
                ("step", "Adım"),
                ("status", "Durum"),
                ("duration", "Süre"),
                ("attempts", "Deneme"),
                ("mode", "Çıktı modu"),
                ("tokens", "Token"),
                ("error", "Hata"),
            )
        ],
        rows=rows,
        row_key="step",
    ).props("dense flat").classes("w-full text-xs")


def _flow_dialog(run: dict[str, Any]) -> None:
    with ui.dialog() as dialog, ui.card().classes("min-w-[1150px]"):
        ui.label(f"{run['incident_key']} — {run.get('summary') or ''}").classes("font-semibold")
        ui.echart(workflow_graph(WORKFLOW_NODES, run["node_states"])).classes("w-full h-44")
        _steps_table(run["steps"])
        with ui.row():
            ui.button("Kayda git", on_click=lambda: ui.navigate.to(issue_url(run["incident_key"]))).props(
                "outline"
            )
            ui.button("Kapat", on_click=dialog.close)
    dialog.open()


def register(ctx: DashboardContext) -> None:
    @ui.page("/live")
    async def live_page(request: Request) -> None:
        principal = authorize(ctx, request)
        if principal is None:
            return
        charts: dict[str, Any] = {}
        state: dict[str, Any] = {"running_ids": None}

        with frame(ctx, principal, "Canlı Ajan Akışı"):
            model = "mock (deterministik)" if ctx.settings.llm.provider == "mock" else ctx.settings.llm.model
            ui.label(
                f"Webhook → kuyruk → işçi → LangGraph iş akışı · LLM: {model} · "
                f"sayfa her {REFRESH_SECONDS:g} saniyede güncellenir."
            ).classes("text-sm text-slate-500")
            kpi_row = ui.row().classes("gap-4 flex-wrap")
            ui.label("Ajanlar şu anda ne yapıyor?").classes("text-lg font-semibold mt-2")
            lanes_box = ui.row().classes("gap-3 flex-wrap w-full")
            with ui.row().classes("items-center w-full"):
                ui.label("Çalışan iş akışları").classes("text-lg font-semibold")
                ui.space()
                _legend()
            running_box = ui.column().classes("w-full gap-3")
            with ui.row().classes("w-full gap-4 flex-nowrap items-start"):
                with ui.column().classes("w-2/3"):
                    ui.label("Son tamamlanan analizler").classes("text-lg font-semibold")
                    recent_box = ui.column().classes("w-full gap-2")
                with ui.column().classes("w-1/3"):
                    ui.label("Gelen olaylar (webhook)").classes("text-lg font-semibold")
                    events_box = ui.column().classes("w-full gap-1")
                    ui.label("İşçiler (son 30 dk)").classes("text-lg font-semibold mt-4")
                    workers_box = ui.column().classes("w-full gap-1")

            def render_kpis(data: dict[str, Any]) -> None:
                kpi_row.clear()
                inbox = data["inbox"]
                last = data["events"][0] if data["events"] else None
                with kpi_row:
                    kpi("Kuyrukta bekleyen", inbox.get("PENDING", 0))
                    kpi(
                        "İşleniyor",
                        len([r for r in data["running"] if not r["stale"]]),
                        color="text-blue-700",
                    )
                    kpi("Tamamlanan olay", inbox.get("COMPLETED", 0), color="text-green-700")
                    kpi("Ölü mektup", inbox.get("DEAD_LETTER", 0), color="text-red-600")
                    kpi("Son gelen kayıt", f"{_ago(last['age_s'])} önce" if last else None)
                    kpi("Aktif işçi", len([w for w in data["workers"] if w["busy"]]))

            def render_lanes(data: dict[str, Any]) -> None:
                lanes_box.clear()
                with lanes_box:
                    for lane in data["lanes"]:
                        busy = lane["busy"]
                        border = "border-blue-600 border-2 bg-blue-50" if busy else "border border-slate-200"
                        with ui.card().classes(f"w-56 {border}").props("flat"):
                            with ui.row().classes("items-center gap-1"):
                                ui.icon("smart_toy" if lane["is_agent"] else "settings").classes(
                                    "text-blue-700" if lane["is_agent"] else "text-slate-500"
                                )
                                ui.label(lane["label"]).classes("font-semibold text-sm")
                            if busy:
                                for b in busy:
                                    with ui.row().classes("items-center gap-1"):
                                        ui.spinner(size="xs")
                                        ui.label(
                                            f"{b['incident_key']} · {_short_worker(b['worker_id'])} · {_ago(b['elapsed_s'])}"
                                        ).classes("text-xs text-blue-800")
                            else:
                                ui.label("boşta").classes("text-xs text-slate-400")
                            if lane["last_output"]:
                                ui.label(f"Son çıktı → {lane['last_output']}").classes(
                                    "text-xs text-slate-700"
                                )
                            avg = f"{lane['avg_ms'] / 1000:.1f} sn" if lane["avg_ms"] is not None else "—"
                            ui.label(
                                f"24 sa: {lane['runs_24h']} çalışma · ort. {avg} · hata {lane['failures_24h']}"
                                + (f" · {lane['tokens_24h']} token" if lane["is_agent"] else "")
                            ).classes("text-xs text-slate-500")

            def render_running(data: dict[str, Any]) -> None:
                runs = data["running"]
                ids = [r["analysis_id"] for r in runs]
                if ids != state["running_ids"]:
                    state["running_ids"] = ids
                    charts.clear()
                    running_box.clear()
                    with running_box:
                        if not runs:
                            ui.label("Şu anda çalışan iş akışı yok — yeni kayıt bekleniyor.").classes(
                                "text-slate-500"
                            )
                        for run in runs:
                            with ui.card().classes("w-full"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.badge(
                                        "STALE" if run["stale"] else "RUNNING",
                                        color="grey" if run["stale"] else "info",
                                    )
                                    ui.link(run["incident_key"], issue_url(run["incident_key"])).classes(
                                        "font-semibold"
                                    )
                                    ui.label(run.get("summary") or "").classes("text-sm truncate max-w-xl")
                                    ui.space()
                                    meta = ui.label().classes("text-xs text-slate-500")
                                chart = ui.echart(workflow_graph(WORKFLOW_NODES, run["node_states"])).classes(
                                    "w-full h-52"
                                )
                                charts[run["analysis_id"]] = (chart, meta)
                for run in runs:
                    chart, meta = charts[run["analysis_id"]]
                    chart.options.clear()
                    chart.options.update(workflow_graph(WORKFLOW_NODES, run["node_states"]))
                    chart.update()
                    current = STEP_LABELS.get(run["current_step"] or "", run["current_step"] or "—")
                    meta.text = (
                        f"Şu an: {current} · işçi {_short_worker(run['worker_id'])} · model {run['model_id']} · "
                        f"{_ago(run['elapsed_s'])}"
                    )

            def render_recent(data: dict[str, Any]) -> None:
                recent_box.clear()
                with recent_box:
                    for run in data["recent"]:
                        with ui.card().classes("w-full py-1").props("flat bordered"):
                            with ui.row().classes("items-center gap-2 w-full"):
                                ui.badge(run["status"], color=STATUS_COLORS.get(run["status"], "grey"))
                                ui.link(run["incident_key"], issue_url(run["incident_key"])).classes(
                                    "font-semibold"
                                )
                                ui.label(run.get("summary") or "").classes("text-sm truncate max-w-md")
                                ui.space()
                                ui.label(
                                    f"tamlık {run['completeness'] if run['completeness'] is not None else '—'} · "
                                    f"kalite {run['quality'] if run['quality'] is not None else '—'} · "
                                    f"{(run['duration_ms'] or 0) / 1000:.0f} sn · {run['tokens']} token"
                                ).classes("text-xs text-slate-600")
                                ui.button(icon="account_tree", on_click=lambda r=run: _flow_dialog(r)).props(
                                    "flat dense"
                                ).tooltip("Ajan akışını göster")
                            with ui.row().classes("gap-1"):
                                for node, label, _ in WORKFLOW_NODES:
                                    st = run["node_states"].get(node, "pending")
                                    ui.element("div").style(
                                        f"width:22px;height:8px;border-radius:2px;background:{NODE_COLORS[st]}"
                                    ).tooltip(f"{label}: {NODE_STATE_TR[st]}")

            def render_side(data: dict[str, Any]) -> None:
                events_box.clear()
                with events_box:
                    for e in data["events"]:
                        with ui.row().classes("items-center gap-2 text-xs"):
                            ui.badge(
                                e["status"],
                                color={
                                    "COMPLETED": "positive",
                                    "PENDING": "warning",
                                    "PROCESSING": "info",
                                    "DEAD_LETTER": "negative",
                                }.get(e["status"], "grey"),
                            )
                            ui.label(f"{e['incident_key'] or '—'} · {e['event']} · {_ago(e['age_s'])} önce")
                workers_box.clear()
                with workers_box:
                    if not data["workers"]:
                        ui.label("Henüz işçi etkinliği yok").classes("text-xs text-slate-400")
                    for w in data["workers"]:
                        with ui.row().classes("items-center gap-2 text-xs"):
                            ui.icon("engineering").classes("text-blue-700" if w["busy"] else "text-slate-400")
                            ui.label(
                                f"{_short_worker(w['worker_id'])} · {'çalışıyor' if w['busy'] else 'boşta'} · "
                                f"{w['runs']} analiz"
                            )

            async def refresh() -> None:
                data = await load(lambda: ctx.query(live_activity))
                render_kpis(data)
                render_lanes(data)
                render_running(data)
                render_recent(data)
                render_side(data)

            await refresh()
            ui.timer(REFRESH_SECONDS, refresh)

"""Agent Executions page (plus inbox / dead-letter view for administrators)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from nicegui import ui

from branchops.dashboard.components.layout import authorize, frame, load
from branchops.dashboard.context import DashboardContext
from branchops.domain.enums import Role
from branchops.infrastructure.persistence.audit import audit
from branchops.infrastructure.persistence.inbox_repository import InboxRepository

EXEC_COLUMNS = [
    {"name": "started_at", "label": "Başlangıç", "field": "started_at", "align": "left"},
    {"name": "issue_key", "label": "Kayıt", "field": "issue_key", "align": "left"},
    {"name": "status", "label": "Durum", "field": "status"},
    {"name": "duration_ms", "label": "Süre (ms)", "field": "duration_ms"},
    {"name": "model_id", "label": "Model", "field": "model_id"},
    {"name": "template", "label": "Şablon", "field": "template"},
    {"name": "tokens", "label": "Token (girdi/çıktı)", "field": "tokens"},
    {"name": "inbox_attempts", "label": "Deneme", "field": "inbox_attempts"},
    {"name": "event", "label": "Olay", "field": "event"},
    {"name": "error_count", "label": "Hata", "field": "error_count"},
]
STEP_COLUMNS = [
    {"name": k, "label": label, "field": k, "align": "left"}
    for k, label in (
        ("step", "Adım"),
        ("status", "Durum"),
        ("duration_ms", "Süre (ms)"),
        ("attempts", "Deneme"),
        ("model_id", "Model"),
        ("output_mode", "Çıktı modu"),
        ("prompt_tokens", "Girdi token"),
        ("completion_tokens", "Çıktı token"),
        ("error", "Hata"),
    )
]


def register(ctx: DashboardContext) -> None:
    @ui.page("/executions")
    async def executions_page(request: Request) -> None:
        principal = authorize(ctx, request)
        if principal is None:
            return
        state: dict[str, Any] = {"page": 1}
        with frame(ctx, principal, "Ajan Çalıştırmaları"):
            with ui.row().classes("items-end gap-3"):
                status = ui.select(
                    [None, "COMPLETED", "PARTIAL", "NEEDS_REVIEW", "FAILED"], value=None, label="Durum"
                ).classes("w-48")
                page_label = ui.label()
            table = ui.table(columns=EXEC_COLUMNS, rows=[], row_key="analysis_id").classes("w-full")
            with ui.row():
                prev_btn = ui.button(icon="chevron_left").props("flat")
                next_btn = ui.button(icon="chevron_right").props("flat")

            async def show_steps(row: dict[str, Any]) -> None:
                steps = await load(
                    lambda: ctx.query(lambda s: ctx.read.execution_steps(s, row["analysis_id"]))
                )
                with ui.dialog() as dialog, ui.card().classes("min-w-[1000px]"):
                    ui.label(
                        f"{row['issue_key']} · {row['analysis_id']} · korelasyon {row['correlation_id']}"
                    ).classes("font-semibold")
                    ui.table(columns=STEP_COLUMNS, rows=steps, row_key="step").classes("w-full")
                    for err in row["errors"]:
                        ui.label(err).classes("text-sm font-mono text-negative")
                    ui.button("Kapat", on_click=dialog.close)
                dialog.open()

            table.on("rowClick", lambda e: show_steps(e.args[1]))

            async def refresh() -> None:
                result = await load(
                    lambda: ctx.query(
                        lambda s: ctx.read.executions(
                            s, status=status.value, page=state["page"], page_size=25
                        )
                    )
                )
                table.rows = [
                    {
                        **r,
                        "tokens": f"{r['prompt_tokens']}/{r['completion_tokens']}",
                        "error_count": len(r["errors"]),
                    }
                    for r in result.items
                ]
                table.update()
                pages = max(1, -(-result.total // result.page_size))
                state["pages"] = pages
                page_label.text = f"Sayfa {result.page}/{pages} — {result.total} çalıştırma"
                prev_btn.set_enabled(result.page > 1)
                next_btn.set_enabled(result.page < pages)

            async def go(delta: int) -> None:
                state["page"] = max(1, min(state.get("pages", 1), state["page"] + delta))
                await refresh()

            prev_btn.on_click(lambda: go(-1))
            next_btn.on_click(lambda: go(1))

            async def on_status() -> None:
                state["page"] = 1
                await refresh()

            status.on_value_change(on_status)
            await refresh()

            if principal.has(Role.ADMIN):
                ui.separator()
                ui.label("Gelen kutusu (webhook inbox)").classes("text-xl font-semibold")
                inbox_box = ui.column().classes("w-full")

                async def render_inbox() -> None:
                    summary = await load(lambda: ctx.query(ctx.read.inbox_summary))
                    inbox_box.clear()
                    with inbox_box:
                        ui.label(" · ".join(f"{k}: {v}" for k, v in sorted(summary["by_status"].items())))
                        for d in summary["dead_letters"]:
                            with ui.card().classes("w-full"), ui.row().classes("items-center gap-3"):
                                ui.label(f"{d['issue_key']} · {d['event']} · {d['attempts']} deneme")
                                ui.label(d["last_error"]).classes("text-xs font-mono text-negative max-w-2xl")

                                async def replay(event_id: str = d["event_id"]) -> None:
                                    def do() -> bool:
                                        with ctx.db.session() as s:
                                            ok = InboxRepository().replay(s, uuid.UUID(event_id))
                                            if ok:
                                                audit(
                                                    s,
                                                    actor=principal.username,
                                                    action="inbox_replay",
                                                    resource_type="inbox_event",
                                                    resource_id=event_id,
                                                )
                                            return ok

                                    ok = await load(do)
                                    ui.notify("Yeniden kuyruğa alındı" if ok else "Yeniden oynatılamadı")
                                    await render_inbox()

                                ui.button("Yeniden oynat", on_click=replay).props("outline dense")

                await render_inbox()

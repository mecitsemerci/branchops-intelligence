"""Incident Explorer and Incident Detail pages (all sources)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from nicegui import ui

from branchops.application.commands.ingest_webhook import RequestReanalysis
from branchops.application.queries.read_models import IssueFilter
from branchops.dashboard.components.charts import history_chart
from branchops.dashboard.components.layout import (
    STATE_COLORS,
    authorize,
    frame,
    issue_url,
    kpi,
    load,
    score_color,
    status_badge,
)
from branchops.dashboard.context import DashboardContext
from branchops.dashboard.pages.attachments import attachments_panel
from branchops.domain.entities.incident import incident_key as make_incident_key
from branchops.domain.entities.incident import is_valid_incident_key, parse_incident_key
from branchops.domain.entities.issue import FIELD_LABELS_TR
from branchops.domain.enums import Role
from branchops.infrastructure.persistence.audit import audit

ISSUE_COLUMNS = [
    {"name": "incident_key", "label": "Kayıt", "field": "incident_key", "align": "left"},
    {"name": "source", "label": "Kaynak", "field": "source"},
    {"name": "summary", "label": "Özet", "field": "summary", "align": "left", "classes": "max-w-md truncate"},
    {"name": "category", "label": "Kategori", "field": "category", "align": "left"},
    {"name": "branch_code", "label": "Şube", "field": "branch_code"},
    {"name": "application", "label": "Uygulama", "field": "application", "align": "left"},
    {"name": "completeness", "label": "Tamlık", "field": "completeness"},
    {"name": "quality", "label": "Kalite", "field": "quality"},
    {"name": "analysis_status", "label": "Analiz", "field": "analysis_status"},
    {"name": "updated_at", "label": "Güncelleme", "field": "updated_at"},
]


def register(ctx: DashboardContext) -> None:
    @ui.page("/issues")
    async def explorer_page(request: Request) -> None:
        principal = authorize(ctx, request)
        if principal is None:
            return
        state: dict[str, Any] = {"page": 1}
        categories = sorted(ctx.registry.taxonomy())
        sources = [s.id for s in ctx.settings.sources]
        with frame(ctx, principal, "Kayıt Gezgini"):
            with ui.row().classes("items-end gap-3 flex-wrap"):
                search = ui.input("Ara (anahtar / metin)").classes("w-64")
                source = ui.select([None, *sources], label="Kaynak", value=None).classes("w-36")
                category = ui.select([None, *categories], label="Kategori", value=None).classes("w-48")
                branch = ui.input("Şube kodu").classes("w-32")
                application = ui.input("Uygulama").classes("w-48")
                status = ui.select(
                    [None, "COMPLETED", "PARTIAL", "NEEDS_REVIEW", "FAILED"],
                    label="Analiz durumu",
                    value=None,
                ).classes("w-44")
                sort = ui.select(
                    {
                        "updated": "Güncelleme",
                        "quality": "Kalite",
                        "completeness": "Tamlık",
                        "incident_key": "Kayıt",
                    },
                    value="updated",
                    label="Sırala",
                ).classes("w-36")
                descending = ui.checkbox("Azalan", value=True)
                low_only = ui.checkbox("Sadece düşük kalite")
            table = ui.table(columns=ISSUE_COLUMNS, rows=[], row_key="incident_key").classes("w-full")
            table.on("rowClick", lambda e: ui.navigate.to(issue_url(e.args[1]["incident_key"])))
            with ui.row().classes("items-center gap-2"):
                prev_btn = ui.button(icon="chevron_left").props("flat")
                page_label = ui.label()
                next_btn = ui.button(icon="chevron_right").props("flat")

            async def refresh(reset: bool = False) -> None:
                if reset:
                    state["page"] = 1
                f = IssueFilter(
                    search=search.value or None,
                    source=source.value,
                    category=category.value,
                    branch_code=branch.value or None,
                    application=application.value or None,
                    status=status.value,
                    low_quality_only=bool(low_only.value),
                    sort=sort.value,
                    descending=bool(descending.value),
                    page=state["page"],
                    page_size=25,
                )
                result = await load(lambda: ctx.query(lambda s: ctx.read.list_issues(s, f)))
                table.rows = result.items
                table.update()
                pages = max(1, -(-result.total // result.page_size))
                state["pages"] = pages
                page_label.text = f"Sayfa {result.page} / {pages} — toplam {result.total} kayıt"
                prev_btn.set_enabled(result.page > 1)
                next_btn.set_enabled(result.page < pages)

            async def go(delta: int) -> None:
                state["page"] = max(1, min(state.get("pages", 1), state["page"] + delta))
                await refresh()

            prev_btn.on_click(lambda: go(-1))
            next_btn.on_click(lambda: go(1))
            for el in (source, category, status, sort, descending, low_only):
                el.on_value_change(lambda: refresh(reset=True))
            for text_input in (search, branch, application):
                text_input.on("keydown.enter", lambda: refresh(reset=True))
            ui.button("Filtrele", icon="filter_alt", on_click=lambda: refresh(reset=True))
            await refresh()

    @ui.page("/issues/{raw_key:path}")
    async def detail_page(request: Request, raw_key: str) -> None:
        principal = authorize(ctx, request)
        if principal is None:
            return
        if not is_valid_incident_key(raw_key):
            ui.label("Geçersiz kayıt anahtarı").classes("text-negative")
            return
        incident_key = make_incident_key(*parse_incident_key(raw_key))
        detail = await load(lambda: ctx.query(lambda s: ctx.read.issue_detail(s, incident_key)))
        threshold = ctx.settings.scoring.low_quality_threshold
        with frame(ctx, principal, f"{incident_key}"):
            if detail is None:
                ui.label("Kayıt bulunamadı.")
                return
            issue = detail["issue"]
            analysis = detail["latest_analysis"]
            with ui.row().classes("items-center gap-3"):
                ui.label(issue["summary"] or "").classes("text-lg")
                if analysis:
                    status_badge(analysis["status"])
                if principal.has(Role.ANALYST):

                    async def reanalyze() -> None:
                        def enqueue() -> str | None:
                            with ctx.db.session() as s:
                                audit(
                                    s,
                                    actor=principal.username,
                                    action="reanalysis_requested",
                                    resource_type="issue",
                                    resource_id=incident_key,
                                )
                            return RequestReanalysis(ctx.db, ctx.settings).execute(
                                incident_key, requested_by=principal.username, correlation_id=uuid.uuid4().hex
                            )

                        await load(enqueue)
                        ui.notify("Yeniden analiz kuyruğa alındı", type="positive")

                    ui.button("Yeniden analiz et", icon="refresh", on_click=reanalyze).props("outline")

            if analysis:
                with ui.row().classes("gap-4 flex-wrap"):
                    kpi(
                        "Tamlık",
                        analysis["completeness_score"],
                        color=score_color(analysis["completeness_score"], threshold),
                    )
                    kpi(
                        "Kalite",
                        analysis["quality_score"],
                        color=score_color(analysis["quality_score"], threshold),
                    )
                    kpi("Şablon", f"{analysis['template_id']}@{analysis['template_version']}")
                    cls = analysis.get("classification") or {}
                    kpi("Kategori", f"{cls.get('category', '—')} / {cls.get('subcategory') or '—'}")
                    kpi("Model", analysis["model_id"])
                if analysis["security_flags"]:
                    with ui.card().classes("w-full bg-orange-50"):
                        ui.label("Güvenlik uyarıları (manuel inceleme gerekli)").classes(
                            "font-semibold text-orange-800"
                        )
                        for flag in analysis["security_flags"]:
                            ui.label(flag)
                if analysis["errors"]:
                    with ui.expansion("Analiz hataları / kısmi sonuçlar", icon="warning").classes("w-full"):
                        for err in analysis["errors"]:
                            ui.label(err).classes("text-sm font-mono")

            with ui.tabs() as tabs:
                t_fields = ui.tab("Alanlar")
                t_recs = ui.tab("Öneriler")
                t_original = ui.tab("Orijinal kayıt")
                t_similar = ui.tab("Benzer kayıtlar")
                t_attachments = ui.tab("Ekler & bağlam")
                t_history = ui.tab("Puan geçmişi")
            with ui.tab_panels(tabs, value=t_fields).classes("w-full"):
                with ui.tab_panel(t_fields):
                    _fields_panel(analysis)
                with ui.tab_panel(t_recs):
                    _recommendations_panel(analysis)
                with ui.tab_panel(t_original):
                    ui.label(
                        f"Kaynak: {issue['source']} · Durum: {issue['source_status']} · "
                        f"Grup: {issue['group_key']} · "
                        f"Oluşturma: {issue['source_created_at']}"
                    ).classes("text-sm text-slate-500")
                    ui.label(issue["description"] or "").classes("whitespace-pre-wrap")
                    for c in detail["comments"]:
                        with ui.card().classes("w-full bg-slate-50"):
                            ui.label(f"{c['author'] or ''} · {c['created_at'] or ''}").classes(
                                "text-xs text-slate-500"
                            )
                            ui.label(c["body"]).classes("whitespace-pre-wrap")
                with ui.tab_panel(t_similar):
                    _similar_panel(detail["relationships"], analysis)
                with ui.tab_panel(t_attachments):
                    await attachments_panel(ctx, incident_key)
                with ui.tab_panel(t_history):
                    if detail["score_history"]:
                        ui.echart(history_chart(detail["score_history"])).classes("h-72 w-full")
                    ui.table(
                        columns=[
                            {"name": k, "label": k, "field": k}
                            for k in (
                                "started_at",
                                "status",
                                "completeness",
                                "quality",
                                "template",
                                "scoring_policy_version",
                                "model_id",
                            )
                        ],
                        rows=detail["score_history"],
                        row_key="analysis_id",
                    ).classes("w-full")


def _fields_panel(analysis: dict[str, Any] | None) -> None:
    if not analysis:
        ui.label("Bu kayıt henüz analiz edilmedi.")
        return
    canonical = (analysis.get("canonical_issue") or {}).get("fields", {})
    levels = {a["field"]: a for a in analysis.get("assessments", [])}
    rows = []
    for fs in analysis.get("field_scores", []):
        name = fs["field"]
        extracted = canonical.get(name, {})
        evidence = extracted.get("evidence") or []
        rows.append(
            {
                "field": FIELD_LABELS_TR.get(name, name),
                "value": (extracted.get("value") or "")[:300],
                "state": fs["state"],
                "required": "✓" if fs["required"] else "",
                "weight": fs["weight"],
                "level": fs.get("rubric_level") or "",
                "reason": (levels.get(name) or {}).get("reason", ""),
                "source": (extracted.get("provenance") or {}).get("source", ""),
                "evidence": "✓" if any(e.get("verified") for e in evidence) else ("✗" if evidence else ""),
                "validation": "; ".join(fs.get("validation_errors") or []),
            }
        )
    cols = [
        {"name": "field", "label": "Alan", "field": "field", "align": "left"},
        {
            "name": "value",
            "label": "Değer",
            "field": "value",
            "align": "left",
            "classes": "max-w-md whitespace-normal",
        },
        {"name": "state", "label": "Durum", "field": "state"},
        {"name": "required", "label": "Zorunlu", "field": "required"},
        {"name": "weight", "label": "Ağırlık", "field": "weight"},
        {"name": "level", "label": "Seviye", "field": "level"},
        {"name": "source", "label": "Kaynak", "field": "source"},
        {"name": "evidence", "label": "Kanıt doğrulandı", "field": "evidence"},
        {"name": "validation", "label": "Doğrulama", "field": "validation", "align": "left"},
        {
            "name": "reason",
            "label": "Değerlendirme",
            "field": "reason",
            "align": "left",
            "classes": "whitespace-normal",
        },
    ]
    table = ui.table(columns=cols, rows=rows, row_key="field").classes("w-full")
    table.add_slot(
        "body-cell-state",
        """<q-td :props="props"><q-badge :color="{SUFFICIENT:'positive',INSUFFICIENT:'warning',MISSING:'negative',
        UNKNOWN:'grey',NOT_APPLICABLE:'blue-grey-3'}[props.value]">{{ props.value }}</q-badge></q-td>""",
    )
    with ui.row().classes("gap-2 mt-2"):
        for label, key in (
            ("Eksik", "missing_fields"),
            ("Yetersiz", "insufficient_fields"),
            ("Bilinmiyor", "unknown_fields"),
        ):
            for f in analysis.get(key, []):
                color = {"Eksik": STATE_COLORS["MISSING"], "Yetersiz": STATE_COLORS["INSUFFICIENT"]}.get(
                    label, "grey"
                )
                ui.chip(f"{label}: {FIELD_LABELS_TR.get(f, f)}", color=color).props("outline")


def _recommendations_panel(analysis: dict[str, Any] | None) -> None:
    recs = (analysis or {}).get("recommendations") or []
    if not recs:
        ui.label("Öneri yok — kayıt şablon gereksinimlerini karşılıyor.")
        return
    for r in recs:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center gap-2"):
                ui.badge(
                    r["priority"],
                    color={"HIGH": "negative", "MEDIUM": "warning", "LOW": "grey"}[r["priority"]],
                )
                ui.label(FIELD_LABELS_TR.get(r.get("field") or "", r.get("field") or "")).classes(
                    "font-semibold"
                )
                ui.label(f"({r['source']})").classes("text-xs text-slate-400")
            ui.label(r["message"])


def _similar_panel(relationships: list[dict[str, Any]], analysis: dict[str, Any] | None) -> None:
    if analysis and analysis.get("recurring"):
        ui.label(
            f"Tekrarlayan sorun: aynı hata imzası {analysis['recurring_count']} kayıtta görüldü."
        ).classes("text-orange-700 font-semibold")
    ui.label("Benzerlik aynı kök nedeni kanıtlamaz; kayıtlar otomatik birleştirilmez.").classes(
        "text-xs text-slate-500"
    )
    cols = [
        {"name": "incident_key", "label": "Kayıt", "field": "incident_key", "align": "left"},
        {"name": "summary", "label": "Özet", "field": "summary", "align": "left"},
        {"name": "relationship", "label": "İlişki", "field": "relationship"},
        {"name": "signals", "label": "Sinyaller", "field": "signals", "align": "left"},
        {
            "name": "rationale",
            "label": "Gerekçe",
            "field": "rationale",
            "align": "left",
            "classes": "whitespace-normal",
        },
    ]
    rows = [{**r, "signals": ", ".join(r.get("signals") or [])} for r in relationships]
    table = ui.table(columns=cols, rows=rows, row_key="incident_key").classes("w-full")
    table.on("rowClick", lambda e: ui.navigate.to(issue_url(e.args[1]["incident_key"])))

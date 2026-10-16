"""Operational intelligence from stored incident data: trends, gaps, improvements, reopen and recurrence.

Principles:

* Counts are counts of *records*, never presented as business impact: no customer or transaction volumes are
  derived or estimated.
* Where the sources do not provide the data, the metric is reported as ``"UNKNOWN"`` together with its
  coverage, never as zero. Reopen and recurrence-after-resolution need source status history, which exists
  only for incidents whose status changed after this release.
* Every number is a fixed, parameterised SQL aggregate over the same tables the dashboard and MCP use, and it
  honours the caller's scope.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from branchops.application.queries._common import _r
from branchops.application.queries.scope import UNRESTRICTED, QueryScope

UNKNOWN = "UNKNOWN"
IMPACT_NOTE = (
    "Kayıt sayıları iş etkisi göstergesi değildir; müşteri veya işlem hacmi kaynak verilerde yoktur ve "
    "tahmin edilmez."
)


def _rows(s: Session, sql: str, params: dict[str, Any]) -> list[Any]:
    return list(s.execute(text(sql), params).all())


def weekly_trend(
    s: Session, dimension: str, *, days: int, scope: QueryScope, top: int = 8
) -> list[dict[str, Any]]:
    """Records and average scores per ISO week for the ``top`` most frequent values of ``dimension``."""
    if dimension not in ("application", "branch_code", "category"):
        raise ValueError(f"unsupported dimension {dimension!r}")
    scope_sql, scope_params = scope.sql()
    sql = f"""
        WITH base AS (
            SELECT coalesce({dimension}, 'UNKNOWN') AS value,
                   date_trunc('week', coalesce(source_created_at, created_at)) AS week,
                   latest_quality, latest_completeness
            FROM issues
            WHERE NOT is_deleted AND coalesce(source_created_at, created_at) >= now() - make_interval(days => :days)
            {scope_sql}
        ), top_values AS (
            SELECT value FROM base GROUP BY value ORDER BY count(*) DESC, value LIMIT :top
        )
        SELECT b.value, b.week, count(*) AS n, avg(b.latest_quality) AS quality,
               avg(b.latest_completeness) AS completeness
        FROM base b JOIN top_values t ON t.value = b.value
        GROUP BY b.value, b.week ORDER BY b.value, b.week
    """
    return [
        {
            dimension: r.value,
            "week": r.week.date().isoformat(),
            "issues": int(r.n),
            "avg_quality": _r(r.quality),
            "avg_completeness": _r(r.completeness),
        }
        for r in _rows(s, sql, {"days": days, "top": top, **scope_params})
    ]


def missing_by_category(s: Session, *, days: int, scope: QueryScope) -> list[dict[str, Any]]:
    """Template fields most often MISSING (and, separately, UNKNOWN) per category, latest analyses only."""
    scope_sql, scope_params = scope.sql("i")
    sql = f"""
        SELECT coalesce(i.category, 'UNCLASSIFIED') AS category, f.field,
               count(*) FILTER (WHERE f.kind = 'MISSING') AS missing,
               count(*) FILTER (WHERE f.kind = 'UNKNOWN') AS unknown
        FROM issues i JOIN analyses a ON a.id = i.latest_analysis_id
        CROSS JOIN LATERAL (
            SELECT x, 'MISSING' FROM jsonb_array_elements_text(coalesce(a.missing_fields, '[]'::jsonb)) x
            UNION ALL
            SELECT x, 'UNKNOWN' FROM jsonb_array_elements_text(coalesce(a.unknown_fields, '[]'::jsonb)) x
        ) AS f(field, kind)
        WHERE NOT i.is_deleted AND coalesce(i.source_created_at, i.created_at) >= now() - make_interval(days => :days)
        {scope_sql}
        GROUP BY 1, 2 ORDER BY 1, missing DESC, unknown DESC, 2
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for r in _rows(s, sql, {"days": days, **scope_params}):
        items = out.setdefault(r.category, [])
        if len(items) < 5:
            items.append({"field": r.field, "missing": int(r.missing), "unknown": int(r.unknown)})
    return [{"category": k, "fields": v} for k, v in out.items()]


def quality_after_updates(s: Session, *, days: int, scope: QueryScope) -> dict[str, Any]:
    """How quality changed between the first and the latest non-provisional analysis of re-analysed records
    (re-analysis follows source updates: new comments or edited fields)."""
    scope_sql, scope_params = scope.sql("i")
    sql = f"""
        WITH ranked AS (
            SELECT incident_key, quality_score, started_at,
                   row_number() OVER (PARTITION BY incident_key ORDER BY started_at) AS first_rn,
                   row_number() OVER (PARTITION BY incident_key ORDER BY started_at DESC) AS last_rn,
                   count(*) OVER (PARTITION BY incident_key) AS analyses
            FROM analyses WHERE quality_score IS NOT NULL AND NOT recoverable
        )
        SELECT f.incident_key, f.quality_score AS first_q, l.quality_score AS last_q, l.analyses,
               extract(epoch FROM l.started_at - f.started_at) / 3600.0 AS hours
        FROM ranked f JOIN ranked l ON l.incident_key = f.incident_key AND l.last_rn = 1
        JOIN issues i ON i.incident_key = f.incident_key AND NOT i.is_deleted
        WHERE f.first_rn = 1 AND l.started_at > f.started_at
          AND l.started_at >= now() - make_interval(days => :days)
        {scope_sql}
    """
    rows = _rows(s, sql, {"days": days, **scope_params})
    if not rows:
        return {
            "reanalysed_records": 0,
            "improved": 0,
            "unchanged": 0,
            "worsened": 0,
            "mean_delta": None,
            "median_hours_to_latest": None,
        }
    deltas = [float(r.last_q) - float(r.first_q) for r in rows]
    hours = sorted(float(r.hours) for r in rows)
    return {
        "reanalysed_records": len(rows),
        "improved": sum(1 for d in deltas if d > 0.5),
        "unchanged": sum(1 for d in deltas if -0.5 <= d <= 0.5),
        "worsened": sum(1 for d in deltas if d < -0.5),
        "mean_delta": _r(sum(deltas) / len(deltas)),
        "median_hours_to_latest": _r(hours[len(hours) // 2]),
    }


def reopen_and_recurrence(
    s: Session, *, days: int, scope: QueryScope, recurrence_window_days: int = 30
) -> dict[str, Any]:
    """Reopened records and recurrence of an error signature after a record with it was resolved."""
    scope_sql, scope_params = scope.sql("i")
    params = {"days": days, "window": recurrence_window_days, **scope_params}
    coverage = _rows(
        s,
        f"""
        SELECT count(DISTINCT e.incident_key) AS with_history,
               count(DISTINCT e.incident_key) FILTER (WHERE e.resolved) AS resolved
        FROM issue_status_events e JOIN issues i ON i.incident_key = e.incident_key AND NOT i.is_deleted
        WHERE e.changed_at >= now() - make_interval(days => :days) {scope_sql}
        """,
        params,
    )[0]
    with_history, resolved = int(coverage.with_history), int(coverage.resolved)
    result: dict[str, Any] = {
        "coverage": {"records_with_status_history": with_history, "records_resolved": resolved},
        "reopened_records": UNKNOWN,
        "reopen_rate": UNKNOWN,
        "recurrence_after_resolution": UNKNOWN,
    }
    if resolved == 0:
        result["note"] = (
            "Kaynak sistemlerden çözüm/durum geçmişi henüz gelmedi; yeniden açılma ve çözüm sonrası tekrar "
            "metrikleri hesaplanamaz."
        )
        return result
    reopened = _rows(
        s,
        f"""
        SELECT count(DISTINCT e.incident_key) AS n
        FROM issue_status_events e JOIN issues i ON i.incident_key = e.incident_key AND NOT i.is_deleted
        WHERE e.from_resolved AND NOT e.resolved AND e.changed_at >= now() - make_interval(days => :days)
        {scope_sql}
        """,
        params,
    )[0]
    recurrence = _rows(
        s,
        f"""
        WITH resolutions AS (
            SELECT i.error_signature, e.incident_key, e.changed_at
            FROM issue_status_events e JOIN issues i ON i.incident_key = e.incident_key
            WHERE e.resolved AND i.error_signature IS NOT NULL
              AND e.changed_at >= now() - make_interval(days => :days) {scope_sql}
        )
        SELECT r.error_signature, count(DISTINCT i.incident_key) AS recurrences,
               min(r.changed_at) AS first_resolved
        FROM resolutions r
        JOIN issues i ON i.error_signature = r.error_signature AND i.incident_key <> r.incident_key
             AND NOT i.is_deleted
             AND coalesce(i.source_created_at, i.created_at) > r.changed_at
             AND coalesce(i.source_created_at, i.created_at) <= r.changed_at + make_interval(days => :window)
        WHERE TRUE {scope_sql}
        GROUP BY r.error_signature ORDER BY recurrences DESC LIMIT 20
        """,
        params,
    )
    result["reopened_records"] = int(reopened.n)
    result["reopen_rate"] = _r(int(reopened.n) / resolved)
    result["recurrence_after_resolution"] = [
        {
            "error_signature": r.error_signature,
            "new_records_within_window": int(r.recurrences),
            "first_resolved": r.first_resolved.isoformat(),
        }
        for r in recurrence
    ]
    result["recurrence_window_days"] = recurrence_window_days
    return result


def operational_intelligence(
    s: Session, *, days: int = 90, scope: QueryScope = UNRESTRICTED
) -> dict[str, Any]:
    return {
        "days": days,
        "note": IMPACT_NOTE,
        "application_trend": weekly_trend(s, "application", days=days, scope=scope),
        "branch_trend": weekly_trend(s, "branch_code", days=days, scope=scope),
        "category_trend": weekly_trend(s, "category", days=days, scope=scope),
        "missing_by_category": missing_by_category(s, days=days, scope=scope),
        "quality_after_updates": quality_after_updates(s, days=days, scope=scope),
        "reopen_and_recurrence": reopen_and_recurrence(s, days=days, scope=scope),
    }

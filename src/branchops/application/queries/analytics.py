"""Quality analytics: per-category/branch scores, missing fields, improvements, recurrences."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from branchops.application.queries._common import _r
from branchops.application.queries.scope import UNRESTRICTED, QueryScope


def quality_analytics(
    s: Session, *, days: int = 90, low_quality_threshold: float, scope: QueryScope = UNRESTRICTED
) -> dict[str, Any]:
    scope_sql, scope_params = scope.sql()
    scope_i_sql, _ = scope.sql("i")
    params: dict[str, Any] = {"days": days, **scope_params}
    by_category = s.execute(
        text(
            """
                SELECT coalesce(category, 'UNCLASSIFIED') AS category, count(*) AS n,
                       avg(latest_completeness) AS completeness, avg(latest_quality) AS quality
                FROM issues WHERE NOT is_deleted AND latest_analysis_id IS NOT NULL
                  AND coalesce(source_created_at, created_at) >= now() - make_interval(days => :days)
                """
            + scope_sql
            + """
                GROUP BY 1 ORDER BY quality NULLS LAST
                """
        ),
        params,
    ).all()
    by_branch = s.execute(
        text(
            """
                SELECT coalesce(branch_code, 'BİLİNMİYOR') AS branch, count(*) AS n,
                       avg(latest_completeness) AS completeness, avg(latest_quality) AS quality
                FROM issues WHERE NOT is_deleted AND latest_analysis_id IS NOT NULL
                  AND coalesce(source_created_at, created_at) >= now() - make_interval(days => :days)
                """
            + scope_sql
            + """
                GROUP BY 1 ORDER BY n DESC LIMIT 25
                """
        ),
        params,
    ).all()
    missing = s.execute(
        text(
            """
                SELECT f.field,
                       count(*) FILTER (WHERE f.kind = 'MISSING') AS n,
                       count(*) FILTER (WHERE f.kind = 'UNKNOWN') AS unknown
                FROM issues i JOIN analyses a ON a.id = i.latest_analysis_id
                CROSS JOIN LATERAL (
                    SELECT x, 'MISSING' FROM jsonb_array_elements_text(coalesce(a.missing_fields, '[]'::jsonb)) x
                    UNION ALL
                    SELECT x, 'UNKNOWN' FROM jsonb_array_elements_text(coalesce(a.unknown_fields, '[]'::jsonb)) x
                ) AS f(field, kind)
                WHERE NOT i.is_deleted
                  AND coalesce(i.source_created_at, i.created_at) >= now() - make_interval(days => :days)
                """
            + scope_i_sql
            + """
                GROUP BY f.field ORDER BY n DESC, unknown DESC, f.field LIMIT 15
                """
        ),
        params,
    ).all()
    improvements = s.execute(
        text(
            """
                WITH ranked AS (
                    SELECT incident_key, quality_score, completeness_score, started_at,
                           row_number() OVER (PARTITION BY incident_key ORDER BY started_at) AS first_rn,
                           row_number() OVER (PARTITION BY incident_key ORDER BY started_at DESC) AS last_rn
                    FROM analyses WHERE quality_score IS NOT NULL
                )
                SELECT f.incident_key, i.source, i.external_key,
                       f.quality_score AS first_quality, l.quality_score AS last_quality,
                       f.completeness_score AS first_completeness, l.completeness_score AS last_completeness
                FROM ranked f JOIN ranked l ON l.incident_key = f.incident_key AND l.last_rn = 1
                JOIN issues i ON i.incident_key = f.incident_key AND NOT i.is_deleted
                WHERE f.first_rn = 1 AND l.started_at > f.started_at
                  AND l.started_at >= now() - make_interval(days => :days)
                """
            + scope_i_sql
            + """
                ORDER BY (l.quality_score - f.quality_score) DESC LIMIT 20
                """
        ),
        params,
    ).all()
    recurring = s.execute(
        text(
            """
                SELECT error_signature, count(*) AS n, min(coalesce(source_created_at, created_at)) AS first_seen,
                       max(coalesce(source_created_at, created_at)) AS last_seen,
                       count(DISTINCT branch_code) AS branches,
                       count(DISTINCT source) AS sources,
                       array_agg(incident_key ORDER BY incident_key) AS incident_keys,
                       array_agg(external_key ORDER BY incident_key) AS issue_keys
                FROM issues
                WHERE NOT is_deleted AND error_signature IS NOT NULL
                  AND coalesce(source_created_at, created_at) >= now() - make_interval(days => :days)
                """
            + scope_sql
            + """
                GROUP BY error_signature HAVING count(*) >= 2
                ORDER BY n DESC LIMIT 20
                """
        ),
        params,
    ).all()
    return {
        "by_category": [
            {
                "category": r.category,
                "issues": int(r.n),
                "avg_completeness": _r(r.completeness),
                "avg_quality": _r(r.quality),
            }
            for r in by_category
        ],
        "by_branch": [
            {
                "branch_code": r.branch,
                "issues": int(r.n),
                "avg_completeness": _r(r.completeness),
                "avg_quality": _r(r.quality),
            }
            for r in by_branch
        ],
        # ``count`` = confirmed MISSING; ``unknown`` = not determinable (e.g. extraction failed) - kept apart.
        "most_missing_fields": [
            {"field": r.field, "count": int(r.n), "unknown": int(r.unknown)} for r in missing
        ],
        "improvements": [
            {
                "incident_key": r.incident_key,
                "source": r.source,
                "issue_key": r.external_key,  # deprecated: use incident_key
                "first_quality": _r(r.first_quality),
                "last_quality": _r(r.last_quality),
                "quality_delta": _r((r.last_quality or 0) - (r.first_quality or 0)),
                "first_completeness": _r(r.first_completeness),
                "last_completeness": _r(r.last_completeness),
            }
            for r in improvements
        ],
        "recurring": [
            {
                "error_signature": r.error_signature,
                "issues": int(r.n),
                "branches": int(r.branches),
                "first_seen": r.first_seen.isoformat() if r.first_seen else None,
                "last_seen": r.last_seen.isoformat() if r.last_seen else None,
                "sources": int(r.sources),
                "incident_keys": list(r.incident_keys)[:20],
                "issue_keys": list(r.issue_keys)[:20],  # deprecated: use incident_keys
            }
            for r in recurring
        ],
    }


def recurring_errors(
    s: Session,
    *,
    days: int = 30,
    min_count: int = 3,
    branch_code: str | None = None,
    application: str | None = None,
    limit: int = 50,
    scope: QueryScope = UNRESTRICTED,
) -> list[dict[str, Any]]:
    """Incidents grouped by ``error_signature`` with at least ``min_count`` occurrences in ``days``."""
    scope_sql, scope_params = scope.sql()
    filters = ""
    params: dict[str, Any] = {
        "days": days,
        "min_count": max(1, min_count),
        "limit": max(1, min(limit, 200)),
        **scope_params,
    }
    if branch_code:
        filters += " AND branch_code = :branch_code"
        params["branch_code"] = branch_code
    if application:
        filters += " AND application ILIKE :application"
        params["application"] = f"%{application[:100]}%"
    rows = s.execute(
        text(
            """
                SELECT error_signature, count(*) AS n,
                       min(coalesce(source_created_at, created_at)) AS first_seen,
                       max(coalesce(source_created_at, created_at)) AS last_seen,
                       array_agg(DISTINCT branch_code) FILTER (WHERE branch_code IS NOT NULL) AS branches,
                       array_agg(DISTINCT application) FILTER (WHERE application IS NOT NULL) AS applications,
                       array_agg(DISTINCT source) AS sources,
                       (array_agg(incident_key ORDER BY coalesce(source_created_at, created_at) DESC))[1:20] AS incident_keys
                FROM issues
                WHERE NOT is_deleted AND error_signature IS NOT NULL
                  AND coalesce(source_created_at, created_at) >= now() - make_interval(days => :days)
                """
            + filters
            + scope_sql
            + """
                GROUP BY error_signature HAVING count(*) >= :min_count
                ORDER BY n DESC, last_seen DESC LIMIT :limit
                """
        ),
        params,
    ).all()
    return [
        {
            "error_signature": r.error_signature,
            "count": int(r.n),
            "first_seen": r.first_seen.isoformat() if r.first_seen else None,
            "last_seen": r.last_seen.isoformat() if r.last_seen else None,
            "branch_codes": sorted(r.branches or []),
            "applications": sorted(r.applications or []),
            "sources": sorted(r.sources or []),
            "incident_keys": list(r.incident_keys or []),
        }
        for r in rows
    ]

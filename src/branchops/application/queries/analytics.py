"""Quality analytics: per-category/branch scores, missing fields, improvements, recurrences."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from branchops.application.queries._common import _r


def quality_analytics(s: Session, *, days: int = 90, low_quality_threshold: float) -> dict[str, Any]:
    params = {"days": days}
    by_category = s.execute(
        text(
            """
                SELECT coalesce(category, 'UNCLASSIFIED') AS category, count(*) AS n,
                       avg(latest_completeness) AS completeness, avg(latest_quality) AS quality
                FROM issues WHERE NOT is_deleted AND latest_analysis_id IS NOT NULL
                  AND coalesce(source_created_at, created_at) >= now() - make_interval(days => :days)
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
                GROUP BY 1 ORDER BY n DESC LIMIT 25
                """
        ),
        params,
    ).all()
    missing = s.execute(
        text(
            """
                SELECT f AS field, count(*) AS n
                FROM issues i JOIN analyses a ON a.id = i.latest_analysis_id
                CROSS JOIN LATERAL jsonb_array_elements_text(coalesce(a.missing_fields, '[]'::jsonb)) AS f
                WHERE NOT i.is_deleted
                  AND coalesce(i.source_created_at, i.created_at) >= now() - make_interval(days => :days)
                GROUP BY f ORDER BY n DESC LIMIT 15
                """
        ),
        params,
    ).all()
    improvements = s.execute(
        text(
            """
                WITH ranked AS (
                    SELECT issue_key, quality_score, completeness_score, started_at,
                           row_number() OVER (PARTITION BY issue_key ORDER BY started_at) AS first_rn,
                           row_number() OVER (PARTITION BY issue_key ORDER BY started_at DESC) AS last_rn
                    FROM analyses WHERE quality_score IS NOT NULL
                )
                SELECT f.issue_key, f.quality_score AS first_quality, l.quality_score AS last_quality,
                       f.completeness_score AS first_completeness, l.completeness_score AS last_completeness
                FROM ranked f JOIN ranked l ON l.issue_key = f.issue_key AND l.last_rn = 1
                JOIN issues i ON i.issue_key = f.issue_key AND NOT i.is_deleted
                WHERE f.first_rn = 1 AND l.started_at > f.started_at
                  AND l.started_at >= now() - make_interval(days => :days)
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
                       array_agg(issue_key ORDER BY issue_key) AS issue_keys
                FROM issues
                WHERE NOT is_deleted AND error_signature IS NOT NULL
                  AND coalesce(source_created_at, created_at) >= now() - make_interval(days => :days)
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
        "most_missing_fields": [{"field": r.field, "count": int(r.n)} for r in missing],
        "improvements": [
            {
                "issue_key": r.issue_key,
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
                "issue_keys": list(r.issue_keys)[:20],
            }
            for r in recurring
        ],
    }

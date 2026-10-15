"""Portfolio overview: headline counts, category distribution and daily trend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, literal_column, select, text
from sqlalchemy.orm import Session

from branchops.application.queries._common import _r
from branchops.infrastructure.database.models import Issue


@dataclass
class Overview:
    total_issues: int
    analysed_issues: int
    avg_completeness: float | None
    avg_quality: float | None
    low_quality_issues: int
    needs_review: int
    category_distribution: list[dict[str, Any]] = field(default_factory=list)
    daily_trend: list[dict[str, Any]] = field(default_factory=list)


def overview(s: Session, *, days: int = 30, low_quality_threshold: float) -> Overview:
    active = Issue.is_deleted.is_(False)
    row = s.execute(
        select(
            func.count(),
            func.count(Issue.latest_analysis_id),
            func.avg(Issue.latest_completeness),
            func.avg(Issue.latest_quality),
            func.count().filter(Issue.latest_quality < low_quality_threshold),
            func.count().filter(Issue.latest_status == "NEEDS_REVIEW"),
        ).where(active)
    ).one()
    category = func.coalesce(Issue.category, literal_column("'UNCLASSIFIED'"))
    categories = s.execute(
        select(category, func.count(), func.avg(Issue.latest_quality))
        .where(active)
        .group_by(category)
        .order_by(func.count().desc())
    ).all()
    trend = s.execute(
        text(
            """
                SELECT date_trunc('day', coalesce(source_created_at, created_at))::date AS day,
                       count(*) AS issues, avg(latest_completeness) AS completeness, avg(latest_quality) AS quality
                FROM issues
                WHERE NOT is_deleted AND coalesce(source_created_at, created_at) >= now() - make_interval(days => :days)
                GROUP BY 1 ORDER BY 1
                """
        ),
        {"days": days},
    ).all()
    return Overview(
        total_issues=int(row[0]),
        analysed_issues=int(row[1]),
        avg_completeness=_r(row[2]),
        avg_quality=_r(row[3]),
        low_quality_issues=int(row[4]),
        needs_review=int(row[5]),
        category_distribution=[
            {"category": c, "count": int(n), "avg_quality": _r(q)} for c, n, q in categories
        ],
        daily_trend=[
            {"day": d.isoformat(), "issues": int(n), "avg_completeness": _r(c), "avg_quality": _r(q)}
            for d, n, c, q in trend
        ],
    )

"""Read-side queries shared by the REST API, the dashboard and the reporting agent.

Every number shown to users is computed here with SQL; nothing in this module calls a model.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import and_, func, literal_column, or_, select, text
from sqlalchemy.orm import Session

from branchops.infrastructure.database.models import (
    AgentStep,
    Analysis,
    Issue,
    IssueComment,
    IssueRelationship,
    WebhookInbox,
)

SortField = Literal["updated", "quality", "completeness", "issue_key"]


@dataclass
class IssueFilter:
    search: str | None = None
    category: str | None = None
    branch_code: str | None = None
    application: str | None = None
    status: str | None = None
    low_quality_only: bool = False
    sort: SortField = "updated"
    descending: bool = True
    page: int = 1
    page_size: int = 25


@dataclass
class Page:
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


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


def _r(v: Any) -> float | None:
    return round(float(v), 2) if v is not None else None


class ReadModels:
    def __init__(self, low_quality_threshold: float) -> None:
        self.low_quality_threshold = low_quality_threshold

    # --- overview ---------------------------------------------------------------------------------

    def overview(self, s: Session, days: int = 30) -> Overview:
        active = Issue.is_deleted.is_(False)
        row = s.execute(
            select(
                func.count(),
                func.count(Issue.latest_analysis_id),
                func.avg(Issue.latest_completeness),
                func.avg(Issue.latest_quality),
                func.count().filter(Issue.latest_quality < self.low_quality_threshold),
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

    # --- explorer ---------------------------------------------------------------------------------

    def list_issues(self, s: Session, f: IssueFilter) -> Page:
        conditions: list[Any] = [Issue.is_deleted.is_(False)]
        if f.search:
            term = f.search.strip()[:200]
            conditions.append(
                or_(
                    Issue.issue_key.ilike(f"%{term}%"),
                    Issue.search_vector.op("@@")(func.websearch_to_tsquery("turkish", term)),
                )
            )
        if f.category:
            conditions.append(Issue.category == f.category)
        if f.branch_code:
            conditions.append(Issue.branch_code == f.branch_code)
        if f.application:
            conditions.append(Issue.application.ilike(f"%{f.application[:100]}%"))
        if f.status:
            conditions.append(Issue.latest_status == f.status)
        if f.low_quality_only:
            conditions.append(Issue.latest_quality < self.low_quality_threshold)
        sort_col = {
            "updated": func.coalesce(Issue.source_updated_at, Issue.updated_at),
            "quality": Issue.latest_quality,
            "completeness": Issue.latest_completeness,
            "issue_key": Issue.issue_key,
        }[f.sort]
        order = sort_col.desc().nulls_last() if f.descending else sort_col.asc().nulls_last()
        total = s.execute(select(func.count()).select_from(Issue).where(and_(*conditions))).scalar_one()
        page_size = max(1, min(f.page_size, 200))
        page = max(1, f.page)
        rows = s.execute(
            select(
                Issue.issue_key,
                Issue.summary,
                Issue.status,
                Issue.category,
                Issue.subcategory,
                Issue.branch_code,
                Issue.application,
                Issue.latest_completeness,
                Issue.latest_quality,
                Issue.latest_status,
                Issue.source_updated_at,
            )
            .where(and_(*conditions))
            .order_by(order, Issue.issue_key)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        items = [
            {
                "issue_key": r.issue_key,
                "summary": r.summary,
                "jira_status": r.status,
                "category": r.category,
                "subcategory": r.subcategory,
                "branch_code": r.branch_code,
                "application": r.application,
                "completeness": _r(r.latest_completeness),
                "quality": _r(r.latest_quality),
                "analysis_status": r.latest_status,
                "updated_at": r.source_updated_at.isoformat() if r.source_updated_at else None,
            }
            for r in rows
        ]
        return Page(items=items, total=int(total), page=page, page_size=page_size)

    # --- detail -----------------------------------------------------------------------------------

    def issue_detail(self, s: Session, issue_key: str) -> dict[str, Any] | None:
        issue = s.get(Issue, issue_key)
        if issue is None or issue.is_deleted:
            return None
        latest = s.get(Analysis, issue.latest_analysis_id) if issue.latest_analysis_id else None
        history = s.execute(
            select(
                Analysis.id,
                Analysis.started_at,
                Analysis.status,
                Analysis.completeness_score,
                Analysis.quality_score,
                Analysis.template_id,
                Analysis.template_version,
                Analysis.scoring_policy_version,
                Analysis.model_id,
            )
            .where(Analysis.issue_key == issue_key)
            .order_by(Analysis.started_at)
        ).all()
        relationships = s.execute(
            select(IssueRelationship, Issue.summary)
            .join(
                Issue,
                Issue.issue_key
                == func.coalesce(
                    func.nullif(IssueRelationship.target_issue_key, issue_key),
                    IssueRelationship.source_issue_key,
                ),
            )
            .where(
                or_(
                    IssueRelationship.source_issue_key == issue_key,
                    IssueRelationship.target_issue_key == issue_key,
                ),
                Issue.is_deleted.is_(False),
            )
            .order_by(IssueRelationship.text_rank.desc())
        ).all()
        comments = s.execute(
            select(IssueComment)
            .where(IssueComment.issue_key == issue_key, IssueComment.is_deleted.is_(False))
            .order_by(IssueComment.created_at)
        ).scalars()
        return {
            "issue": {
                "issue_key": issue.issue_key,
                "project_key": issue.project_key,
                "summary": issue.summary,
                "description": issue.description,
                "jira_status": issue.status,
                "category": issue.category,
                "subcategory": issue.subcategory,
                "branch_code": issue.branch_code,
                "application": issue.application,
                "source_created_at": issue.source_created_at.isoformat() if issue.source_created_at else None,
                "source_updated_at": issue.source_updated_at.isoformat() if issue.source_updated_at else None,
            },
            "comments": [
                {
                    "id": c.comment_id,
                    "author": c.author,
                    "body": c.body,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                }
                for c in comments
            ],
            "latest_analysis": _analysis_dict(latest) if latest else None,
            "score_history": [
                {
                    "analysis_id": str(h.id),
                    "started_at": h.started_at.isoformat(),
                    "status": h.status,
                    "completeness": _r(h.completeness_score),
                    "quality": _r(h.quality_score),
                    "template": f"{h.template_id}@{h.template_version}" if h.template_id else None,
                    "scoring_policy_version": h.scoring_policy_version,
                    "model_id": h.model_id,
                }
                for h in history
            ],
            "relationships": [
                {
                    "issue_key": rel.target_issue_key
                    if rel.source_issue_key == issue_key
                    else rel.source_issue_key,
                    "summary": summary,
                    "relationship": rel.relationship_type,
                    "text_rank": rel.text_rank,
                    "signals": rel.signals,
                    "rationale": rel.rationale,
                    "direction": "outgoing" if rel.source_issue_key == issue_key else "incoming",
                }
                for rel, summary in relationships
            ],
        }

    # --- analytics --------------------------------------------------------------------------------

    def quality_analytics(self, s: Session, days: int = 90) -> dict[str, Any]:
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

    # --- executions -------------------------------------------------------------------------------

    def executions(
        self, s: Session, *, status: str | None = None, page: int = 1, page_size: int = 25
    ) -> Page:
        cond = [Analysis.status == status] if status else []
        total = s.execute(select(func.count()).select_from(Analysis).where(*cond)).scalar_one()
        page_size = max(1, min(page_size, 200))
        rows = s.execute(
            select(Analysis, WebhookInbox.attempts, WebhookInbox.raw_event_name)
            .outerjoin(WebhookInbox, WebhookInbox.id == Analysis.inbox_event_id)
            .where(*cond)
            .order_by(Analysis.started_at.desc())
            .offset((max(1, page) - 1) * page_size)
            .limit(page_size)
        ).all()
        items = [
            {
                "analysis_id": str(a.id),
                "issue_key": a.issue_key,
                "status": a.status,
                "started_at": a.started_at.isoformat(),
                "duration_ms": a.duration_ms,
                "model_id": a.model_id,
                "template": f"{a.template_id}@{a.template_version}" if a.template_id else None,
                "prompt_tokens": a.prompt_tokens,
                "completion_tokens": a.completion_tokens,
                "errors": a.errors or [],
                "security_flags": a.security_flags or [],
                "inbox_attempts": attempts,
                "event": event_name,
                "correlation_id": a.correlation_id,
            }
            for a, attempts, event_name in rows
        ]
        return Page(items=items, total=int(total), page=max(1, page), page_size=page_size)

    def execution_steps(self, s: Session, analysis_id: str) -> list[dict[str, Any]]:
        try:
            aid = uuid.UUID(analysis_id)
        except ValueError:
            return []
        steps = s.execute(
            select(AgentStep).where(AgentStep.analysis_id == aid).order_by(AgentStep.started_at, AgentStep.id)
        ).scalars()
        return [
            {
                "step": st.step,
                "status": st.status,
                "started_at": st.started_at.isoformat(),
                "duration_ms": st.duration_ms,
                "attempts": st.attempts,
                "model_id": st.model_id,
                "output_mode": st.output_mode,
                "prompt_tokens": st.prompt_tokens,
                "completion_tokens": st.completion_tokens,
                "error": st.error,
            }
            for st in steps
        ]

    def inbox_summary(self, s: Session) -> dict[str, Any]:
        counts = dict(
            s.execute(select(WebhookInbox.status, func.count()).group_by(WebhookInbox.status)).all()
        )
        dead = s.execute(
            select(
                WebhookInbox.id,
                WebhookInbox.issue_key,
                WebhookInbox.raw_event_name,
                WebhookInbox.attempts,
                WebhookInbox.last_error,
                WebhookInbox.received_at,
            )
            .where(WebhookInbox.status == "DEAD_LETTER")
            .order_by(WebhookInbox.received_at.desc())
            .limit(50)
        ).all()
        return {
            "by_status": {k: int(v) for k, v in counts.items()},
            "dead_letters": [
                {
                    "event_id": str(d.id),
                    "issue_key": d.issue_key,
                    "event": d.raw_event_name,
                    "attempts": d.attempts,
                    "last_error": (d.last_error or "")[:500],
                    "received_at": d.received_at.isoformat(),
                }
                for d in dead
            ],
        }


def _analysis_dict(a: Analysis) -> dict[str, Any]:
    return {
        "analysis_id": str(a.id),
        "status": a.status,
        "started_at": a.started_at.isoformat(),
        "completed_at": a.completed_at.isoformat() if a.completed_at else None,
        "duration_ms": a.duration_ms,
        "template_id": a.template_id,
        "template_version": a.template_version,
        "scoring_policy_version": a.scoring_policy_version,
        "prompt_version": a.prompt_version,
        "model_id": a.model_id,
        "completeness_score": _r(a.completeness_score),
        "quality_score": _r(a.quality_score),
        "classification": a.classification,
        "canonical_issue": a.canonical_issue,
        "field_scores": a.field_scores or [],
        "assessments": a.assessments or [],
        "missing_fields": a.missing_fields or [],
        "insufficient_fields": a.insufficient_fields or [],
        "unknown_fields": a.unknown_fields or [],
        "missing_required_fields": a.missing_required_fields or [],
        "recommendations": a.recommendations or [],
        "similar_issues": a.similar_issues or [],
        "recurring": a.recurring,
        "recurring_count": a.recurring_count,
        "security_flags": a.security_flags or [],
        "errors": a.errors or [],
        "prompt_tokens": a.prompt_tokens,
        "completion_tokens": a.completion_tokens,
    }


# --- live agent activity --------------------------------------------------------------------------

WORKFLOW_NODES: tuple[tuple[str, str, bool], ...] = (
    # (node, Turkish label, is_llm_agent)
    ("normalize", "Normalizasyon", False),
    ("classify", "Sınıflandırma Ajanı", True),
    ("select_template", "Şablon Seçimi", False),
    ("extract", "Çıkarım Ajanı", True),
    ("assess_quality", "Kalite Değerlendirme Ajanı", True),
    ("score", "Puanlama Motoru", False),
    ("similarity", "Benzerlik Ajanı", True),
    ("recommend", "Öneri Ajanı", True),
    ("persist", "Kaydetme", False),
)


def step_node(step: str) -> str:
    """Map a recorded step name (e.g. ``similarity.verify``) to its workflow node."""
    return step.split(".", 1)[0]


def node_states(steps: list[dict[str, Any]], current_step: str | None, running: bool) -> dict[str, str]:
    """Per workflow node: pending | running | succeeded | failed | skipped."""
    states = {node: "pending" for node, _, _ in WORKFLOW_NODES}
    for st in steps:
        node = step_node(st["step"])
        status = st["status"]
        mapped = {
            "SUCCEEDED": "succeeded",
            "FAILED": "failed",
            "DEGRADED": "failed",
            "SKIPPED": "skipped",
        }.get(status, "succeeded")
        previous = states.get(node, "pending")
        # a node with several steps (similarity.retrieve + .verify): failure wins, then success over skip
        if previous == "failed" or (previous == "succeeded" and mapped == "skipped"):
            continue
        states[node] = mapped
    if running and current_step in states:
        states[current_step] = "running"
    return states


def live_activity(s: Session, stale_minutes: int = 5, recent: int = 12) -> dict[str, Any]:
    running_rows = s.execute(
        text(
            """
            SELECT a.id, a.issue_key, a.current_step, a.worker_id, a.started_at, a.heartbeat_at, a.model_id,
                   i.summary, extract(epoch FROM now() - a.started_at) AS elapsed,
                   extract(epoch FROM now() - coalesce(a.heartbeat_at, a.started_at)) AS since_beat
            FROM analyses a JOIN issues i ON i.issue_key = a.issue_key
            WHERE a.status = 'RUNNING'
            ORDER BY a.started_at
            """
        )
    ).all()
    ids = [r.id for r in running_rows]
    recent_rows = s.execute(
        text(
            """
            SELECT a.id, a.issue_key, a.status, a.worker_id, a.started_at, a.completed_at, a.duration_ms,
                   a.completeness_score, a.quality_score, a.category, a.model_id, i.summary,
                   a.prompt_tokens + a.completion_tokens AS tokens
            FROM analyses a JOIN issues i ON i.issue_key = a.issue_key
            WHERE a.status <> 'RUNNING'
            ORDER BY coalesce(a.completed_at, a.started_at) DESC
            LIMIT :n
            """
        ),
        {"n": recent},
    ).all()
    ids += [r.id for r in recent_rows]
    steps_by_id: dict[Any, list[dict[str, Any]]] = {}
    if ids:
        for st in s.execute(
            select(AgentStep)
            .where(AgentStep.analysis_id.in_(ids))
            .order_by(AgentStep.started_at, AgentStep.id)
        ).scalars():
            steps_by_id.setdefault(st.analysis_id, []).append(
                {
                    "step": st.step,
                    "status": st.status,
                    "duration_ms": st.duration_ms,
                    "attempts": st.attempts,
                    "model_id": st.model_id,
                    "output_mode": st.output_mode,
                    "tokens": st.prompt_tokens + st.completion_tokens,
                    "error": st.error,
                }
            )
    lane_stats = {
        r.node: r
        for r in s.execute(
            text(
                """
                SELECT split_part(step, '.', 1) AS node, count(*) AS runs,
                       avg(duration_ms) FILTER (WHERE status <> 'SKIPPED') AS avg_ms,
                       count(*) FILTER (WHERE status = 'FAILED') AS failures,
                       sum(prompt_tokens + completion_tokens) AS tokens,
                       max(started_at) AS last_at
                FROM agent_steps WHERE started_at >= now() - interval '24 hours'
                GROUP BY 1
                """
            )
        ).all()
    }
    inbox = dict(s.execute(select(WebhookInbox.status, func.count()).group_by(WebhookInbox.status)).all())
    last_event = s.execute(
        text(
            """
            SELECT raw_event_name, issue_key, status, received_at, extract(epoch FROM now() - received_at) AS age
            FROM webhook_inbox ORDER BY received_at DESC LIMIT 10
            """
        )
    ).all()
    workers = s.execute(
        text(
            """
            SELECT worker_id, max(coalesce(heartbeat_at, started_at)) AS seen,
                   bool_or(status = 'RUNNING' AND heartbeat_at >= now() - interval '5 minutes') AS busy, count(*) AS runs
            FROM analyses WHERE worker_id IS NOT NULL AND coalesce(heartbeat_at, started_at) >= now() - interval '30 minutes'
            GROUP BY worker_id ORDER BY worker_id
            """
        )
    ).all()

    last_outputs = _last_outputs(s.get(Analysis, recent_rows[0].id) if recent_rows else None)
    running = []
    for r in running_rows:
        stale = float(r.since_beat or 0) > stale_minutes * 60
        steps = steps_by_id.get(r.id, [])
        running.append(
            {
                "analysis_id": str(r.id),
                "issue_key": r.issue_key,
                "summary": r.summary,
                "current_step": r.current_step,
                "worker_id": r.worker_id,
                "model_id": r.model_id,
                "elapsed_s": int(r.elapsed or 0),
                "stale": stale,
                "steps": steps,
                "node_states": node_states(steps, r.current_step, running=not stale),
            }
        )
    lanes = []
    for node, label, is_agent in WORKFLOW_NODES:
        busy = [r for r in running if r["current_step"] == node and not r["stale"]]
        stat = lane_stats.get(node)
        lanes.append(
            {
                "node": node,
                "label": label,
                "is_agent": is_agent,
                "busy": [
                    {"issue_key": b["issue_key"], "worker_id": b["worker_id"], "elapsed_s": b["elapsed_s"]}
                    for b in busy
                ],
                "runs_24h": int(stat.runs) if stat else 0,
                "avg_ms": int(stat.avg_ms) if stat and stat.avg_ms is not None else None,
                "failures_24h": int(stat.failures) if stat else 0,
                "tokens_24h": int(stat.tokens or 0) if stat else 0,
                "last_at": stat.last_at.isoformat() if stat and stat.last_at else None,
                "last_output": last_outputs.get(node),
            }
        )
    return {
        "running": running,
        "recent": [
            {
                "analysis_id": str(r.id),
                "issue_key": r.issue_key,
                "summary": r.summary,
                "status": r.status,
                "worker_id": r.worker_id,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "duration_ms": r.duration_ms,
                "completeness": _r(r.completeness_score),
                "quality": _r(r.quality_score),
                "category": r.category,
                "model_id": r.model_id,
                "tokens": int(r.tokens or 0),
                "node_states": node_states(steps_by_id.get(r.id, []), None, running=False),
                "steps": steps_by_id.get(r.id, []),
            }
            for r in recent_rows
        ],
        "lanes": lanes,
        "inbox": {k: int(v) for k, v in inbox.items()},
        "events": [
            {
                "event": e.raw_event_name,
                "issue_key": e.issue_key,
                "status": e.status,
                "received_at": e.received_at.isoformat(),
                "age_s": int(e.age or 0),
            }
            for e in last_event
        ],
        "workers": [
            {"worker_id": w.worker_id, "busy": bool(w.busy), "runs": int(w.runs), "seen": w.seen.isoformat()}
            for w in workers
        ],
    }


def _last_outputs(a: Analysis | None) -> dict[str, str]:
    """Short human-readable result of each node for the most recently finished analysis."""
    if a is None:
        return {}
    key = a.issue_key
    out: dict[str, str] = {}
    cls = a.classification or {}
    if cls:
        out["classify"] = (
            f"{key}: {cls.get('category')}/{cls.get('subcategory') or '—'} ({cls.get('confidence')})"
        )
    if a.template_id:
        out["select_template"] = f"{key}: {a.template_id}@{a.template_version}"
    fields = (a.canonical_issue or {}).get("fields", {})
    if fields:
        found = sum(1 for f in fields.values() if f.get("state") in ("SUFFICIENT", "INSUFFICIENT"))
        out["extract"] = f"{key}: {found}/{len(fields)} alan bulundu"
    if a.quality_score is not None:
        out["assess_quality"] = f"{key}: {len(a.assessments or [])} alan değerlendirildi"
    if a.completeness_score is not None:
        out["score"] = f"{key}: tamlık {a.completeness_score:.0f} · kalite " + (
            f"{a.quality_score:.0f}" if a.quality_score is not None else "—"
        )
    sims = a.similar_issues or []
    out["similarity"] = f"{key}: {len(sims)} benzer kayıt" + (" · tekrarlayan" if a.recurring else "")
    out["recommend"] = f"{key}: {len(a.recommendations or [])} öneri"
    out["persist"] = f"{key}: {a.status}"
    if a.security_flags:
        out["normalize"] = f"{key}: {len(a.security_flags)} güvenlik uyarısı"
    else:
        out["normalize"] = f"{key}: temiz"
    return out

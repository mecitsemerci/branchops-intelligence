"""Live agent activity snapshot for the dashboard: running/recent analyses, lanes, workers."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from branchops.application.queries._common import _last_outputs, _r
from branchops.application.queries.workflow_nodes import WORKFLOW_NODES, node_states
from branchops.infrastructure.database.models import AgentStep, Analysis, WebhookInbox


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

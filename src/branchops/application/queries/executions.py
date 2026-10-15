"""Analysis execution history, per-step traces and webhook inbox summary."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from branchops.application.queries.filters import Page
from branchops.infrastructure.database.models import AgentStep, Analysis, WebhookInbox


def executions(s: Session, *, status: str | None = None, page: int = 1, page_size: int = 25) -> Page:
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


def execution_steps(s: Session, analysis_id: str) -> list[dict[str, Any]]:
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


def inbox_summary(s: Session) -> dict[str, Any]:
    counts = dict(s.execute(select(WebhookInbox.status, func.count()).group_by(WebhookInbox.status)).all())
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

"""Routing and notification read models: per-team load, time to first notification, unowned screens."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


def routing_analytics(s: Session, *, days: int = 30) -> dict[str, Any]:
    params = {"days": days}
    per_team = s.execute(
        text(
            """
            SELECT t.key, t.name, count(*) AS n
            FROM incident_routing r JOIN teams t ON t.id = r.owner_team_id
            WHERE r.reported_at >= now() - make_interval(days => :days)
            GROUP BY t.key, t.name ORDER BY n DESC, t.key
            """
        ),
        params,
    ).all()
    summary = s.execute(
        text(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE unowned_screen) AS unowned,
                   count(*) FILTER (WHERE screen_unregistered) AS unregistered,
                   percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY extract(epoch FROM first_notified_at - reported_at)
                   ) FILTER (WHERE first_notified_at IS NOT NULL) AS median_seconds
            FROM incident_routing
            WHERE reported_at >= now() - make_interval(days => :days)
            """
        ),
        params,
    ).one()
    screens = s.execute(
        text(
            """
            SELECT coalesce(application_key, '—') AS application_key, coalesce(screen_key, '—') AS screen_key,
                   count(*) AS n
            FROM incident_routing
            WHERE reported_at >= now() - make_interval(days => :days)
            GROUP BY 1, 2 ORDER BY n DESC, 1, 2 LIMIT 15
            """
        ),
        params,
    ).all()
    total = int(summary.total)
    return {
        "days": days,
        "routed_incidents": total,
        "per_owner_team": [{"team_key": r.key, "team_name": r.name, "incidents": int(r.n)} for r in per_team],
        "median_minutes_to_first_notification": round(float(summary.median_seconds) / 60, 1)
        if summary.median_seconds is not None
        else None,
        "unowned_screen_rate": round(int(summary.unowned) / total, 3) if total else None,
        "unregistered_screen_rate": round(int(summary.unregistered) / total, 3) if total else None,
        "top_screens": [
            {"application_key": r.application_key, "screen_key": r.screen_key, "incidents": int(r.n)}
            for r in screens
        ],
    }


def unowned_screens(s: Session, *, days: int = 90, limit: int = 100) -> list[dict[str, Any]]:
    """Screens named by reports that fell back to branch-ops or are not registered (assign an owner)."""
    rows = s.execute(
        text(
            """
            SELECT r.application_key, r.screen_key, count(*) AS n,
                   count(*) FILTER (WHERE r.unowned_screen) AS fallback,
                   max(r.reported_at) AS last_seen,
                   (array_agg(c.context ->> 'route' ORDER BY r.reported_at DESC))[1] AS route,
                   (array_agg(c.context ->> 'screen_title' ORDER BY r.reported_at DESC))[1] AS screen_title,
                   (array_agg(r.incident_key ORDER BY r.reported_at DESC))[1:5] AS incident_keys
            FROM incident_routing r
            LEFT JOIN incident_app_context c ON c.incident_key = r.incident_key
            WHERE (r.unowned_screen OR r.screen_unregistered)
              AND r.reported_at >= now() - make_interval(days => :days)
              AND NOT EXISTS (
                  SELECT 1 FROM screens sc JOIN applications a ON a.id = sc.application_id
                  WHERE a.key = r.application_key AND sc.key = r.screen_key AND sc.active
              )
            GROUP BY r.application_key, r.screen_key
            ORDER BY n DESC, last_seen DESC LIMIT :limit
            """
        ),
        {"days": days, "limit": limit},
    ).all()
    return [
        {
            "application_key": r.application_key,
            "screen_key": r.screen_key,
            "screen_title": r.screen_title,
            "route": r.route,
            "incidents": int(r.n),
            "fell_back_to_branch_ops": int(r.fallback),
            "last_seen": r.last_seen.isoformat() if r.last_seen else None,
            "incident_keys": list(r.incident_keys or []),
        }
        for r in rows
    ]


def notification_log(
    s: Session, *, incident_key: str | None = None, status: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    where = ["TRUE"]
    params: dict[str, Any] = {"limit": max(1, min(limit, 1000))}
    if incident_key:
        where.append("n.incident_key = :incident_key")
        params["incident_key"] = incident_key
    if status:
        where.append("n.status = :status")
        params["status"] = status
    rows = s.execute(
        text(
            "SELECT n.id, n.incident_key, n.event, n.channel, n.recipient, n.role, n.status, n.attempts, "
            "n.last_error, n.created_at, n.completed_at, n.payload ->> 'subject' AS subject, "
            "(n.payload ->> 'off_hours')::boolean AS off_hours, t.key AS team_key "
            "FROM notification_outbox n LEFT JOIN teams t ON t.id = n.team_id "
            f"WHERE {' AND '.join(where)} ORDER BY n.created_at DESC LIMIT :limit"
        ),
        params,
    ).all()
    return [
        {
            "id": str(r.id),
            "incident_key": r.incident_key,
            "event": r.event,
            "channel": r.channel,
            "recipient": r.recipient,
            "team_key": r.team_key,
            "role": r.role,
            "status": r.status,
            "attempts": r.attempts,
            "last_error": (r.last_error or "")[:300] or None,
            "subject": r.subject,
            "off_hours": bool(r.off_hours),
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in rows
    ]

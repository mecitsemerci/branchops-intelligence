"""Recovery state machine for analyses degraded by model unavailability (table ``analysis_recovery``).

States (one row per incident)::

    RECOVERY_PENDING ──enqueue──▶ RUNNING ──non-degraded analysis stored──▶ COMPLETED
           ▲                         │
           │                         ├──degraded again / event dead-lettered──▶ RETRY_WAIT ──due──▶ RUNNING
           │                         └──attempts exhausted─────────────────────▶ FAILED_PERMANENT
           └── a new degraded analysis of a terminal row (new content) starts a new cycle

Transitions caused by a stored analysis happen in the same transaction as the analysis itself
(``record_analysis``), so the state can never disagree with the stored results. The scheduler only moves
due rows to RUNNING by inserting a recovery inbox event, and resolves RUNNING rows whose event ended without
an analysis (dead letter, skipped).
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from branchops.config.settings import RecoverySettings
from branchops.domain.entities.analysis import AnalysisResult
from branchops.infrastructure.database.models import AnalysisRecovery, WebhookInbox
from branchops.infrastructure.observability.telemetry import get_instruments


class RecoveryState(StrEnum):
    RECOVERY_PENDING = "RECOVERY_PENDING"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    COMPLETED = "COMPLETED"
    FAILED_PERMANENT = "FAILED_PERMANENT"


ACTIVE = (RecoveryState.RECOVERY_PENDING.value, RecoveryState.RUNNING.value, RecoveryState.RETRY_WAIT.value)
DUE = (RecoveryState.RECOVERY_PENDING.value, RecoveryState.RETRY_WAIT.value)


def recovery_delay(
    attempt: int, settings: RecoverySettings, rand: Callable[[], float] = random.random
) -> float:
    """``base * 2^attempt`` capped at ``max_delay``, jittered to 75-100 % so recoveries do not align."""
    exp = min(settings.max_delay_seconds, settings.base_delay_seconds * (2 ** max(0, attempt)))
    return float(round(exp * (0.75 + 0.25 * rand()), 3))


@dataclass(frozen=True)
class DueRecovery:
    incident_key: str
    attempts: int
    analysis_id: uuid.UUID | None


class RecoveryRepository:
    def __init__(self, settings: RecoverySettings) -> None:
        self._settings = settings

    # --- transitions driven by stored analyses ---------------------------------------------------

    def record_analysis(self, session: Session, result: AnalysisResult) -> str | None:
        """Apply the transition for a newly stored analysis; returns the new state (``None`` = no row)."""
        row = session.execute(
            select(AnalysisRecovery)
            .where(AnalysisRecovery.incident_key == result.incident_key)
            .with_for_update()
        ).scalar_one_or_none()
        now = datetime.now(UTC)
        analysis_id = uuid.UUID(result.analysis_id)
        if not result.recoverable:
            if row is None or row.state not in ACTIVE:
                return row.state if row else None
            recovered = row.state == RecoveryState.RUNNING.value
            row.state = RecoveryState.COMPLETED.value
            row.analysis_id = analysis_id
            row.completed_at = row.updated_at = now
            row.reason = "recovered" if recovered else "superseded by a complete analysis"
            get_instruments().recovery_events.add(1, {"event": "recovered" if recovered else "superseded"})
            return row.state
        error = "; ".join(result.errors)[:2000] or None
        if row is None or row.state not in ACTIVE:
            values: dict[str, Any] = {
                "incident_key": result.incident_key,
                "state": RecoveryState.RECOVERY_PENDING.value,
                "analysis_id": analysis_id,
                "input_hash": result.input_hash,
                "attempts": 0,
                "max_attempts": self._settings.max_attempts,
                "next_attempt_at": now + timedelta(seconds=recovery_delay(0, self._settings)),
                "inbox_event_id": None,
                "last_error": error,
                "reason": "model unavailable during analysis",
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
            stmt = insert(AnalysisRecovery).values(**values)
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["incident_key"],
                    set_={k: stmt.excluded[k] for k in values if k != "incident_key"},
                )
            )
            get_instruments().recovery_events.add(1, {"event": "recoverable_failure"})
            return RecoveryState.RECOVERY_PENDING.value
        if row.state == RecoveryState.RUNNING.value:  # the recovery run degraded again
            self._retry_or_give_up(row, error, now)
        row.analysis_id = analysis_id
        row.input_hash = result.input_hash
        row.updated_at = now
        return row.state

    def _retry_or_give_up(self, row: AnalysisRecovery, error: str | None, now: datetime) -> None:
        row.attempts += 1
        row.last_error = error
        row.inbox_event_id = None
        if row.attempts >= row.max_attempts:
            row.state = RecoveryState.FAILED_PERMANENT.value
            row.completed_at = now
            row.reason = "retry limit reached"
            get_instruments().recovery_events.add(1, {"event": "exhausted"})
        else:
            row.state = RecoveryState.RETRY_WAIT.value
            row.next_attempt_at = now + timedelta(seconds=recovery_delay(row.attempts, self._settings))

    # --- scheduler ---------------------------------------------------------------------------------

    def resolve_running(self, session: Session) -> dict[str, int]:
        """RUNNING rows whose recovery event finished without a stored analysis (dead letter, skipped,
        purged) are moved on, so a row can never hang in RUNNING."""
        rows = session.execute(
            select(AnalysisRecovery, WebhookInbox.status, WebhookInbox.last_error, WebhookInbox.outcome)
            .outerjoin(WebhookInbox, WebhookInbox.id == AnalysisRecovery.inbox_event_id)
            .where(AnalysisRecovery.state == RecoveryState.RUNNING.value)
            .with_for_update(of=AnalysisRecovery, skip_locked=True)
        ).all()
        counts = {"retry": 0, "completed": 0}
        now = datetime.now(UTC)
        for row, status, last_error, outcome in rows:
            if status in ("PENDING", "PROCESSING"):
                continue
            if status == "DEAD_LETTER" or status is None:
                self._retry_or_give_up(row, (last_error or "recovery event lost")[:2000], now)
                counts["retry"] += 1
            elif status in ("COMPLETED", "SKIPPED"):
                # Finished without a degraded/complete analysis transition: nothing left to recover
                # (issue deleted, no content) - record why.
                row.state = RecoveryState.COMPLETED.value
                row.completed_at = now
                row.reason = (outcome or "recovery event finished")[:200]
                counts["completed"] += 1
            row.updated_at = now
        return counts

    def due_count(self, session: Session) -> int:
        return int(
            session.execute(
                select(func.count())
                .select_from(AnalysisRecovery)
                .where(AnalysisRecovery.state.in_(DUE), AnalysisRecovery.next_attempt_at <= func.now())
            ).scalar_one()
        )

    def claim_due(self, session: Session, limit: int) -> list[DueRecovery]:
        rows = session.execute(
            select(AnalysisRecovery)
            .where(AnalysisRecovery.state.in_(DUE), AnalysisRecovery.next_attempt_at <= func.now())
            .order_by(AnalysisRecovery.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).scalars()
        return [DueRecovery(r.incident_key, r.attempts, r.analysis_id) for r in rows]

    def mark_running(self, session: Session, incident_key: str, inbox_event_id: uuid.UUID) -> None:
        session.execute(
            update(AnalysisRecovery)
            .where(AnalysisRecovery.incident_key == incident_key)
            .values(state=RecoveryState.RUNNING.value, inbox_event_id=inbox_event_id, updated_at=func.now())
        )

    def postpone(self, session: Session, incident_key: str, seconds: float, reason: str) -> None:
        session.execute(
            update(AnalysisRecovery)
            .where(AnalysisRecovery.incident_key == incident_key)
            .values(
                next_attempt_at=func.now() + func.make_interval(0, 0, 0, 0, 0, 0, seconds),
                reason=reason[:200],
                updated_at=func.now(),
            )
        )

    def manual(self, session: Session, incident_key: str) -> None:
        """Make a recovery due now (operator action); attempts are kept."""
        session.execute(
            update(AnalysisRecovery)
            .where(AnalysisRecovery.incident_key == incident_key, AnalysisRecovery.state.in_(DUE))
            .values(next_attempt_at=func.now(), reason="manual", updated_at=func.now())
        )

    # --- metrics -----------------------------------------------------------------------------------

    def counts_by_state(self, session: Session) -> dict[str, int]:
        rows = session.execute(
            select(AnalysisRecovery.state, func.count()).group_by(AnalysisRecovery.state)
        ).all()
        return {state: int(n) for state, n in rows}

    def oldest_pending_age_seconds(self, session: Session) -> float:
        value = session.execute(
            text(
                "SELECT coalesce(extract(epoch FROM now() - min(created_at)), 0) FROM analysis_recovery "
                "WHERE state IN ('RECOVERY_PENDING', 'RUNNING', 'RETRY_WAIT')"
            )
        ).scalar_one()
        return float(value or 0)

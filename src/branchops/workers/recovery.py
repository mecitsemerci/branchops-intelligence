"""Scheduled recovery of analyses degraded by model unavailability (runs in every worker replica).

Each tick:

1. closes stale RUNNING analysis rows (worker crash) as FAILED;
2. moves on recovery rows whose recovery event ended without an analysis (dead letter, skipped);
3. if recoveries are due, probes the model once; while the model is down nothing is enqueued and no attempt is
   consumed (no retry storm against a recovering gateway);
4. claims at most ``recovery.batch_size`` due rows (``SKIP LOCKED``, so replicas share the work) and enqueues
   one idempotent re-analysis inbox event per incident, skipping incidents that already have an event queued
   or in progress (no duplicate active executions).

The re-analysis reads the newest snapshot, and the latest-analysis rule never lets an older or provisional
result replace a newer complete one.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import exists, select, update

from branchops.application.interfaces.model_provider import ModelProvider
from branchops.application.interfaces.source_adapter import payload_digest
from branchops.config.settings import Settings
from branchops.domain.entities.incident import parse_incident_key
from branchops.domain.enums import AnalysisStatus, InboxStatus, IncidentEventType
from branchops.infrastructure.database.models import Analysis, WebhookInbox
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.observability.telemetry import get_instruments
from branchops.infrastructure.persistence.inbox_repository import InboxRepository, NewInboxEvent
from branchops.infrastructure.persistence.recovery_repository import RecoveryRepository

logger = logging.getLogger(__name__)
RECOVERY_EVENT = "branchops:recovery"


class ModelHealthProbe:
    """Cached model health (``branchops_model_up`` gauge); shared by the scheduler and the metrics."""

    def __init__(self, provider: ModelProvider, ttl_seconds: float = 30.0) -> None:
        self._provider = provider
        self._ttl = ttl_seconds
        self._checked_at = 0.0
        self.healthy: bool | None = None
        self.detail = "not checked"

    def check(self, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and self.healthy is not None and now - self._checked_at < self._ttl:
            return self.healthy
        try:
            health = self._provider.health()
            self.healthy, self.detail = health.healthy, health.detail
        except Exception as exc:  # a probe must never raise into the scheduler
            self.healthy, self.detail = False, type(exc).__name__
        self._checked_at = now
        get_instruments().model_health_checks.add(1, {"healthy": str(self.healthy)})
        return self.healthy


class RecoveryScheduler:
    def __init__(self, db: Database, settings: Settings, probe: ModelHealthProbe | None) -> None:
        self._db = db
        self._settings = settings
        self._repo = RecoveryRepository(settings.recovery)
        self._inbox = InboxRepository()
        self._probe = probe

    def tick(self) -> dict[str, Any]:
        cfg = self._settings.recovery
        instruments = get_instruments()
        summary: dict[str, Any] = {"stale_runs_closed": self.close_stale_runs()}
        with self._db.session() as s:
            resolved = self._repo.resolve_running(s)
        summary.update(resolved)
        if resolved.get("retry"):
            instruments.recovery_events.add(resolved["retry"], {"event": "retry"})
        with self._db.session() as s:
            due = self._repo.due_count(s)
        summary["due"] = due
        if due == 0:
            return summary
        if cfg.require_model_health and self._probe is not None and not self._probe.check():
            instruments.recovery_events.add(1, {"event": "skipped_model_down"})
            summary["skipped"] = f"model unavailable: {self._probe.detail}"[:200]
            return summary
        enqueued = 0
        with self._db.session() as s:
            for item in self._repo.claim_due(s, cfg.batch_size):
                if self._has_active_event(s, item.incident_key):
                    self._repo.postpone(s, item.incident_key, cfg.interval_seconds, "event already queued")
                    continue
                event_id = self._enqueue(s, item.incident_key, item.attempts, item.analysis_id)
                if event_id is not None:
                    self._repo.mark_running(s, item.incident_key, event_id)
                    enqueued += 1
        if enqueued:
            instruments.recovery_events.add(enqueued, {"event": "enqueued"})
        summary["enqueued"] = enqueued
        return summary

    def _has_active_event(self, s: Any, incident_key: str) -> bool:
        source, external_key = parse_incident_key(incident_key)
        return bool(
            s.execute(
                select(
                    exists().where(
                        WebhookInbox.source == source,
                        WebhookInbox.issue_key == external_key,
                        WebhookInbox.status.in_([InboxStatus.PENDING.value, InboxStatus.PROCESSING.value]),
                    )
                )
            ).scalar()
        )

    def _enqueue(
        self, s: Any, incident_key: str, attempt: int, analysis_id: uuid.UUID | None
    ) -> uuid.UUID | None:
        source, external_key = parse_incident_key(incident_key)
        payload = {"webhookEvent": RECOVERY_EVENT, "incident_key": incident_key, "attempt": attempt + 1}
        new = NewInboxEvent(
            # Idempotent per degraded analysis and attempt: a crashed tick cannot enqueue a retry twice.
            idempotency_key=f"recovery:{incident_key}:{analysis_id}:{attempt}"[:200],
            correlation_id=f"recovery-{uuid.uuid4().hex[:20]}",
            event_type=IncidentEventType.MANUAL_REANALYSIS.value,
            raw_event_name=RECOVERY_EVENT,
            source=source,
            issue_key=external_key,
            group_key=None,
            event_timestamp=None,
            payload=payload,
            payload_sha256=payload_digest(payload),
            max_attempts=self._settings.worker.max_attempts,
        )
        event_id, _ = self._inbox.insert(s, new)
        return event_id

    def close_stale_runs(self) -> int:
        """RUNNING analysis rows whose worker stopped sending heartbeats are closed as FAILED (REC-4)."""
        cutoff = datetime.now(UTC) - timedelta(minutes=self._settings.recovery.stale_running_minutes)
        with self._db.session() as s:
            result = s.execute(
                update(Analysis)
                .where(
                    Analysis.status == AnalysisStatus.RUNNING.value,
                    Analysis.heartbeat_at < cutoff,
                )
                .values(
                    status=AnalysisStatus.FAILED.value,
                    current_step=None,
                    errors=["worker stopped (no heartbeat); the inbox event is retried or recovered"],
                    completed_at=datetime.now(UTC),
                )
                .execution_options(synchronize_session=False)
            )
            return int(result.rowcount)  # type: ignore[attr-defined]

    def make_due(self, incident_key: str) -> None:
        with self._db.session() as s:
            self._repo.manual(s, incident_key)

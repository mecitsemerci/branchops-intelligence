"""Handler: apply one inbox event to the incident state and run the analysis workflow when needed."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from branchops.application.interfaces.source_adapter import (
    SourcePayloadError,
    SourceRegistry,
    UnknownSourceError,
)
from branchops.config.settings import Settings
from branchops.domain.entities.incident import IncidentSnapshot
from branchops.domain.enums import InboxStatus, IncidentEventType
from branchops.domain.services.masking import SensitiveDataMasker
from branchops.domain.services.snapshot_merge import merge_event
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.persistence.audit import audit
from branchops.infrastructure.persistence.inbox_repository import InboxItem
from branchops.infrastructure.persistence.issue_repository import IssueRepository
from branchops.templates.registry import TemplateRegistry
from branchops.workflows.issue_analysis import IssueAnalysisWorkflow
from branchops.workflows.state import AnalysisState

logger = logging.getLogger(__name__)


class IssueBusyError(Exception):
    """Another worker currently holds the per-incident lock."""


class NonRetryableEventError(Exception):
    """The event can never be processed successfully (goes straight to dead-letter)."""


@dataclass(frozen=True)
class ProcessOutcome:
    status: InboxStatus
    outcome: str
    analysis_id: str | None = None
    redacted_payload: dict[str, Any] | None = None


def mask_json(value: Any, masker: SensitiveDataMasker) -> Any:
    if isinstance(value, str):
        return masker.mask(value).text
    if isinstance(value, dict):
        return {k: mask_json(v, masker) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_json(v, masker) for v in value]
    return value


def analysis_input_hash(
    snapshot: IncidentSnapshot, registry: TemplateRegistry, settings: Settings, model_id: str
) -> str:
    content = {
        "summary": snapshot.summary,
        "description": snapshot.description,
        "issue_type": snapshot.issue_type,
        "priority": snapshot.priority,
        "labels": snapshot.labels,
        "components": snapshot.components,
        "mapped": snapshot.mapped_fields,
        "comments": [(c.comment_id, c.body) for c in snapshot.active_comments()],
        "templates": sorted(f"{t.template_id}@{t.version}" for t in registry.latest_published()),
        "policy": settings.scoring.version,
        "prompt": settings.workflow.prompt_version,
        "model": model_id,
    }
    encoded = json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class EventProcessor:
    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        registry: TemplateRegistry,
        workflow: IssueAnalysisWorkflow,
        sources: SourceRegistry | None = None,
        masker: SensitiveDataMasker | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._registry = registry
        self._workflow = workflow
        if sources is None:
            from branchops.infrastructure.sources import build_source_registry

            sources = build_source_registry(settings)
        self._sources = sources
        self._masker = masker or SensitiveDataMasker(settings.security.masking_extra_patterns)
        self._issues = IssueRepository()

    def process(self, item: InboxItem) -> ProcessOutcome:
        incident_key = item.incident_key
        if incident_key is None:
            return ProcessOutcome(InboxStatus.SKIPPED, "event without record key")
        with self._db.engine.connect() as lock_conn:
            lock_id = lock_conn.execute(select(func.hashtextextended(incident_key, 0))).scalar_one()
            acquired = lock_conn.execute(select(func.pg_try_advisory_lock(lock_id))).scalar_one()
            lock_conn.commit()
            if not acquired:
                raise IssueBusyError(incident_key)
            try:
                return self._process_locked(item, incident_key)
            finally:
                lock_conn.execute(select(func.pg_advisory_unlock(lock_id)))
                lock_conn.commit()

    def _redacted(self, item: InboxItem) -> dict[str, Any] | None:
        if not self._settings.retention.redact_inbox_payload_after_processing:
            return None
        return mask_json(item.payload, self._masker)  # type: ignore[no-any-return]

    def _process_locked(self, item: InboxItem, incident_key: str) -> ProcessOutcome:
        event_type = IncidentEventType(item.event_type)
        manual = event_type is IncidentEventType.MANUAL_REANALYSIS

        if manual:
            with self._db.session() as session:
                snapshot = self._issues.get_snapshot(session, incident_key)
            if snapshot is None or snapshot.deleted:
                return ProcessOutcome(InboxStatus.SKIPPED, "manual re-analysis of unknown/deleted issue")
        else:
            try:
                adapter = self._sources.get(item.source)
                event = adapter.normalize(item.payload, transform=lambda t: self._masker.mask(t).text)
            except UnknownSourceError as exc:
                raise NonRetryableEventError(f"source not enabled: {exc}") from exc
            except SourcePayloadError as exc:
                raise NonRetryableEventError(f"invalid payload: {exc}") from exc
            if event.incident_key != incident_key:
                raise NonRetryableEventError(
                    f"payload key {event.incident_key} does not match {incident_key}"
                )
            with self._db.session() as session:
                current = self._issues.lock_snapshot(session, incident_key)
                merged = merge_event(
                    current, event, redact_deleted_comments=self._settings.retention.redact_deleted_comments
                )
                snapshot = merged.snapshot
                # Persist any mutation (incl. tombstones and version bumps), not only content changes.
                if current is None or snapshot.model_dump(mode="json") != current.model_dump(mode="json"):
                    self._issues.save_snapshot(session, snapshot)
                if event_type is IncidentEventType.RECORD_DELETED:
                    return self._apply_deletion(session, item, incident_key)
                if merged.ignored_stale and not merged.applied:
                    logger.info(
                        "stale event ignored",
                        extra={"incident_key": incident_key, "fields": ",".join(merged.ignored_stale)},
                    )

        if snapshot.deleted:
            return ProcessOutcome(
                InboxStatus.SKIPPED, "issue is deleted", redacted_payload=self._redacted(item)
            )
        if not snapshot.summary and not snapshot.description:
            # Partial payload for an issue we have never seen in full: wait for a fuller event.
            return ProcessOutcome(
                InboxStatus.COMPLETED,
                "insufficient issue content for analysis",
                redacted_payload=self._redacted(item),
            )

        input_hash = analysis_input_hash(snapshot, self._registry, self._settings, self._workflow.model_id)
        if not manual:
            with self._db.session() as session:
                latest_hash = self._issues.latest_input_hash(session, incident_key)
            # The hash covers all analysis-relevant content, so a retried event whose snapshot was
            # already merged by an earlier attempt is still analysed.
            if latest_hash == input_hash:
                return ProcessOutcome(
                    InboxStatus.COMPLETED,
                    "no analysis-relevant change",
                    redacted_payload=self._redacted(item),
                )

        analysis_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"branchops:{item.id}:{input_hash}"))
        initial: AnalysisState = {
            "analysis_id": analysis_id,
            "incident_key": incident_key,
            "correlation_id": item.correlation_id,
            "inbox_event_id": str(item.id),
            "input_hash": input_hash,
            "allow_degraded": item.is_last_attempt,
            "worker_id": item.lease_owner.rsplit(":", 1)[0],
            "started_at": datetime.now(UTC).isoformat(),
            "snapshot": snapshot.model_dump(mode="json"),
            "steps": [],
            "errors": [],
            "degraded": [],
        }
        result = self._workflow.run(initial, thread_id=f"inbox:{item.id}")
        return ProcessOutcome(
            InboxStatus.COMPLETED,
            f"analysed: {result.status.value}",
            analysis_id=result.analysis_id,
            redacted_payload=self._redacted(item),
        )

    def _apply_deletion(self, session: Any, item: InboxItem, incident_key: str) -> ProcessOutcome:
        policy = self._settings.retention.on_issue_deleted
        if policy == "purge":
            self._issues.purge(session, incident_key)
        else:
            self._issues.mark_deleted(session, incident_key)
        audit(
            session,
            actor="jira-webhook" if item.source == "jira" else f"source:{item.source}",
            action=f"issue_deleted:{policy}",
            resource_type="issue",
            resource_id=incident_key,
            details={"inbox_event_id": str(item.id)},
        )
        return ProcessOutcome(
            InboxStatus.COMPLETED, f"issue deleted ({policy})", redacted_payload=self._redacted(item)
        )

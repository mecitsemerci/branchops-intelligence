"""Command: persist an authenticated Jira webhook in the durable inbox (no LLM work here)."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from branchops.config.settings import Settings
from branchops.domain.enums import JiraEventType
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.jira.adapter import JiraPayloadError, JiraWebhookAdapter
from branchops.infrastructure.persistence.inbox_repository import InboxRepository, NewInboxEvent


class IngestOutcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"


@dataclass(frozen=True)
class IngestResult:
    outcome: IngestOutcome
    correlation_id: str
    event_id: str | None = None
    event_type: str | None = None
    reason: str | None = None


class InvalidWebhookPayloadError(ValueError):
    pass


def payload_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_payload(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidWebhookPayloadError("body is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise InvalidWebhookPayloadError("body must be a JSON object")
    return payload


class IngestJiraWebhook:
    def __init__(self, db: Database, settings: Settings, adapter: JiraWebhookAdapter) -> None:
        self._db = db
        self._settings = settings
        self._adapter = adapter
        self._inbox = InboxRepository()

    def execute(
        self, payload: dict[str, Any], *, correlation_id: str, delivery_id: str | None = None
    ) -> IngestResult:
        try:
            event = self._adapter.normalize(payload)
        except JiraPayloadError as exc:
            raise InvalidWebhookPayloadError(str(exc)) from exc
        if event.event_type is JiraEventType.UNSUPPORTED:
            return IngestResult(
                IngestOutcome.IGNORED, correlation_id, reason=f"unsupported event {event.raw_event_name}"
            )
        allowed = self._settings.jira.allowed_projects
        if allowed and event.project_key not in allowed:
            return IngestResult(IngestOutcome.IGNORED, correlation_id, reason="project not enabled")

        digest = payload_digest(payload)
        # Jira Cloud sends a stable delivery id on retries; otherwise identical bodies are duplicates.
        idempotency_key = f"delivery:{delivery_id}" if delivery_id else f"sha256:{digest}"
        new = NewInboxEvent(
            idempotency_key=idempotency_key[:200],
            correlation_id=correlation_id,
            event_type=event.event_type.value,
            raw_event_name=event.raw_event_name[:120],
            issue_key=event.issue_key,
            project_key=event.project_key,
            event_timestamp=event.event_timestamp,
            payload=payload,
            payload_sha256=digest,
            max_attempts=self._settings.worker.max_attempts,
        )
        with self._db.session() as session:
            event_id, created = self._inbox.insert(session, new)
        return IngestResult(
            IngestOutcome.ACCEPTED if created else IngestOutcome.DUPLICATE,
            correlation_id,
            event_id=str(event_id) if event_id else None,
            event_type=event.event_type.value,
        )


class RequestReanalysis:
    """Enqueue a manual re-analysis of an issue (analyst action, audited by the caller)."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self._inbox = InboxRepository()

    def execute(self, issue_key: str, *, requested_by: str, correlation_id: str) -> str | None:
        payload = {
            "webhookEvent": "branchops:manual_reanalysis",
            "issue": {"key": issue_key},
            "user": {"name": requested_by},
        }
        new = NewInboxEvent(
            idempotency_key=f"manual:{uuid.uuid4()}",
            correlation_id=correlation_id,
            event_type=JiraEventType.MANUAL_REANALYSIS.value,
            raw_event_name="branchops:manual_reanalysis",
            issue_key=issue_key,
            project_key=issue_key.split("-", maxsplit=1)[0],
            event_timestamp=None,
            payload=payload,
            payload_sha256=payload_digest(payload),
            max_attempts=self._settings.worker.max_attempts,
        )
        with self._db.session() as session:
            event_id, _ = self._inbox.insert(session, new)
        return str(event_id) if event_id else None

"""Commands: persist a source event in the durable inbox (no LLM work here); request a re-analysis."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from branchops.application.interfaces.source_adapter import (
    PushSourceAdapter,
    SourcePayloadError,
    SourceRegistry,
    UnknownSourceError,
    event_envelope,
    payload_digest,
)
from branchops.config.settings import Settings
from branchops.domain.entities.incident import IncidentEvent, parse_incident_key
from branchops.domain.enums import IncidentEventType
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.persistence.inbox_repository import InboxRepository, NewInboxEvent

__all__ = [
    "IngestIncidentEvent",
    "IngestOutcome",
    "IngestResult",
    "InvalidWebhookPayloadError",
    "RequestReanalysis",
    "parse_payload",
    "payload_digest",
]


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


def parse_payload(body: bytes) -> dict[str, Any]:
    if b"\\u0000" in body:  # JSON escape for NUL: PostgreSQL JSONB cannot store it
        raise InvalidWebhookPayloadError("body contains NUL characters")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidWebhookPayloadError("body is not valid UTF-8 JSON") from exc
    except RecursionError as exc:
        raise InvalidWebhookPayloadError("body is nested too deeply") from exc
    if not isinstance(payload, dict):
        raise InvalidWebhookPayloadError("body must be a JSON object")
    return payload


class IngestIncidentEvent:
    """Store one source event in the inbox, idempotently per source.

    Push sources pass the raw ``payload`` (+ ``headers``); the adapter normalises it for validation and
    routing and the *raw* payload is stored (the worker normalises again with masking). Pull sources pass an
    already normalised ``event``, which is stored as an event envelope.
    """

    def __init__(self, db: Database, settings: Settings, registry: SourceRegistry) -> None:
        self._db = db
        self._settings = settings
        self._registry = registry
        self._inbox = InboxRepository()

    def execute(
        self,
        source_id: str,
        payload: dict[str, Any] | None = None,
        *,
        correlation_id: str,
        headers: Mapping[str, str] | None = None,
        event: IncidentEvent | None = None,
        signed: bool = False,
    ) -> IngestResult:
        """``signed``: the request body was authenticated by a signature (HMAC). Its idempotency key is then
        derived from the signed body only, never from unsigned headers: re-sending a captured delivery with a
        new ``X-Atlassian-Webhook-Identifier`` must still be recognised as a duplicate."""
        adapter = self._registry.get(source_id)  # UnknownSourceError for unknown/disabled sources
        if (payload is None) == (event is None):
            raise ValueError("pass exactly one of payload or event")
        if payload is not None:
            if not isinstance(adapter, PushSourceAdapter):
                raise UnknownSourceError(f"{source_id} is not a push source")
            try:
                event = adapter.normalize(payload)
            except SourcePayloadError as exc:
                raise InvalidWebhookPayloadError(str(exc)) from exc
            stored = payload
            key = (
                f"sha256:{payload_digest(payload)}"
                if signed
                else adapter.idempotency_key(payload, headers or {})
            )
        else:
            assert event is not None  # noqa: S101 - narrowed by the exactly-one check above
            if event.source != source_id:
                raise InvalidWebhookPayloadError(f"event source {event.source!r} is not {source_id!r}")
            stored = event_envelope(event)
            # The key includes the event timestamp: a re-import must be able to restore an earlier value
            # (A -> B -> A); unchanged re-imports are cheap no-ops in the worker (same analysis input hash).
            key = f"event:{payload_digest(stored)}"

        if event.event_type is IncidentEventType.UNSUPPORTED:
            return IngestResult(
                IngestOutcome.IGNORED, correlation_id, reason=f"unsupported event {event.raw_event_name}"
            )
        source = self._settings.source(source_id)
        allowed = source.allowed_groups if source else []
        if allowed and event.group_key not in allowed:
            return IngestResult(IngestOutcome.IGNORED, correlation_id, reason="group not enabled")

        new = NewInboxEvent(
            idempotency_key=f"{source_id}:{key}"[:200],
            correlation_id=correlation_id,
            event_type=event.event_type.value,
            raw_event_name=event.raw_event_name[:120],
            source=source_id,
            issue_key=event.external_key,
            group_key=event.group_key,
            event_timestamp=event.event_timestamp,
            payload=stored,
            payload_sha256=payload_digest(stored),
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
    """Enqueue a manual re-analysis of an incident (analyst action, audited by the caller)."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self._inbox = InboxRepository()

    def execute(self, incident_key: str, *, requested_by: str, correlation_id: str) -> str | None:
        source, external_key = parse_incident_key(incident_key)
        payload = {
            "webhookEvent": "branchops:manual_reanalysis",
            "incident_key": f"{source}:{external_key}",
            "user": {"name": requested_by},
        }
        new = NewInboxEvent(
            idempotency_key=f"manual:{uuid.uuid4()}",
            correlation_id=correlation_id,
            event_type=IncidentEventType.MANUAL_REANALYSIS.value,
            raw_event_name="branchops:manual_reanalysis",
            source=source,
            issue_key=external_key,
            group_key=None,
            event_timestamp=None,
            payload=payload,
            payload_sha256=payload_digest(payload),
            max_attempts=self._settings.worker.max_attempts,
        )
        with self._db.session() as session:
            event_id, _ = self._inbox.insert(session, new)
        return str(event_id) if event_id else None

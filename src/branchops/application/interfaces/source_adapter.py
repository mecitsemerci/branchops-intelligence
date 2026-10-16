"""Ports for incident sources (Ports & Adapters).

A *push* source delivers events to ``POST /api/ingest/{source_id}`` (webhooks); a *pull* source is read by
``branchops poll`` / ``branchops import`` (files, databases, APIs). Both normalise their native payloads into
the canonical :class:`~branchops.domain.entities.incident.IncidentEvent`; everything downstream of the inbox
(snapshot merge, workflow, scoring, similarity, dashboard) only sees that canonical shape.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from branchops.domain.entities.incident import IncidentEvent

TextTransform = Callable[[str], str]

# Inbox payload envelope used for events that were normalised before ingestion (pull sources).
EVENT_ENVELOPE_KEY = "incident_event"


class SourceAuthError(Exception):
    """The request could not be authenticated for the source."""


class SourcePayloadError(ValueError):
    """The payload is structurally unusable for the source (never retried)."""


class UnknownSourceError(LookupError):
    """No enabled source is registered under the id."""


@runtime_checkable
class SourceAdapter(Protocol):
    source_id: str

    def normalize(self, payload: Mapping[str, Any], transform: TextTransform | None = None) -> IncidentEvent:
        """Normalise a stored inbox payload. ``transform`` is applied to every free-text value (masking)."""
        ...


@runtime_checkable
class PushSourceAdapter(SourceAdapter, Protocol):
    def authenticate(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
        query: Mapping[str, str],
        client_host: str | None,
    ) -> str:
        """Return the mechanism that authenticated the request or raise :class:`SourceAuthError`."""
        ...

    def idempotency_key(self, payload: Mapping[str, Any], headers: Mapping[str, str]) -> str:
        """Key that is identical for redeliveries of the same event (prefixed with the source id by the
        caller)."""
        ...

    def payload_timestamp(self, payload: Mapping[str, Any]) -> datetime | None:
        """When the source says the event happened, read from the payload body only (never the receive
        time). Used for replay protection; ``None`` means the payload asserts no time."""
        ...


@runtime_checkable
class PullSourceAdapter(SourceAdapter, Protocol):
    poll_interval_seconds: float

    def fetch(self, since: datetime | None, limit: int) -> Iterable[IncidentEvent]:
        """Events changed after ``since`` (the stored checkpoint).

        The poller stores ``max(event_timestamp or source_updated_at)`` of the returned events as the next
        checkpoint, so an adapter must never stop in the middle of a group of events sharing a timestamp:
        ``limit`` is a soft bound applied only at such boundaries (a batch may exceed it).
        """
        ...


@runtime_checkable
class FileImportSourceAdapter(PullSourceAdapter, Protocol):
    """Pull source that can also import a given file on demand (``branchops import``)."""

    def read_file(
        self,
        path: Path,
        *,
        is_known: Callable[[str], bool] | None = None,
        imported_at: datetime | None = None,
    ) -> Iterator[IncidentEvent]: ...


class SourceRegistry:
    """Enabled source adapters by id (built in the composition root from ``settings.sources``)."""

    def __init__(self, adapters: Iterable[SourceAdapter] = ()) -> None:
        self._adapters: dict[str, SourceAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: SourceAdapter) -> None:
        if adapter.source_id in self._adapters:
            raise ValueError(f"source {adapter.source_id!r} registered twice")
        self._adapters[adapter.source_id] = adapter

    def get(self, source_id: str) -> SourceAdapter:
        try:
            return self._adapters[source_id]
        except KeyError:
            raise UnknownSourceError(source_id) from None

    def get_push(self, source_id: str) -> PushSourceAdapter:
        adapter = self.get(source_id)
        if not isinstance(adapter, PushSourceAdapter):
            raise UnknownSourceError(f"{source_id} is not a push source")
        return adapter

    def get_pull(self, source_id: str) -> PullSourceAdapter:
        adapter = self.get(source_id)
        if not isinstance(adapter, PullSourceAdapter):
            raise UnknownSourceError(f"{source_id} is not a pull source")
        return adapter

    def push_sources(self) -> list[PushSourceAdapter]:
        return [a for a in self._adapters.values() if isinstance(a, PushSourceAdapter)]

    def pull_sources(self) -> list[PullSourceAdapter]:
        return [a for a in self._adapters.values() if isinstance(a, PullSourceAdapter)]

    def ids(self) -> list[str]:
        return list(self._adapters)


def payload_digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def event_envelope(event: IncidentEvent) -> dict[str, Any]:
    return {EVENT_ENVELOPE_KEY: event.model_dump(mode="json")}


def mask_event(event: IncidentEvent, transform: TextTransform) -> IncidentEvent:
    """Apply ``transform`` to the free-text values of an already normalised event."""

    def tx(value: Any) -> Any:
        return transform(value) if isinstance(value, str) and value else value

    fields = dict(event.fields)
    for attr in ("summary", "description"):
        if attr in fields:
            fields[attr] = tx(fields[attr])
    if fields.get("comments"):
        fields["comments"] = [c.model_copy(update={"body": tx(c.body)}) for c in fields["comments"]]
    comment = event.comment.model_copy(update={"body": tx(event.comment.body)}) if event.comment else None
    return event.model_copy(
        update={
            "fields": fields,
            "mapped_fields": {k: tx(v) for k, v in event.mapped_fields.items()},
            "comment": comment,
        }
    )


def decode_event_envelope(
    source_id: str, payload: Mapping[str, Any], transform: TextTransform | None = None
) -> IncidentEvent:
    """Inverse of :func:`event_envelope` for pull adapters' ``normalize``."""
    raw = payload.get(EVENT_ENVELOPE_KEY)
    if not isinstance(raw, dict):
        raise SourcePayloadError(f"{source_id}: inbox payload is not an incident event envelope")
    try:
        event = IncidentEvent.model_validate(raw)
    except ValueError as exc:
        raise SourcePayloadError(f"{source_id}: invalid incident event: {exc}") from exc
    if event.source != source_id:
        raise SourcePayloadError(f"event source {event.source!r} does not match {source_id!r}")
    return mask_event(event, transform) if transform else event

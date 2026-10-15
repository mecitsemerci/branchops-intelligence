"""Canonical, source-neutral incident data as received from any source (Jira, ServiceNow, CSV, ...).

These contracts are deliberately *partial*: a field set to ``None`` means "not present in the payload",
which is different from "present and empty". Merging logic relies on that distinction so that partial
update events never erase known values.

Identity is ``incident_key`` = ``"<source>:<external_key>"`` (e.g. ``jira:OPS-123``, ``snow:INC0012345``,
``csv:2026-10-import/row-17``). Legacy Jira-shaped input (``issue_key``, ``project_key``, ``issue_id``,
``issue_fields``) is still accepted so that old snapshots, checkpoints and callers keep working.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from branchops.domain.enums import IncidentEventType

DEFAULT_SOURCE = "jira"
SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
# Printable, no whitespace/quotes/angle brackets; "/" and ":" are allowed (file/row ids, sys ids).
# Keys without a source prefix are only accepted in the Jira issue key format (pre-multi-source URLs).
LEGACY_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,29}-\d{1,9}$")
EXTERNAL_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+#=-]{0,159}$")

# Legacy (Jira-named) attribute -> canonical attribute.
_LEGACY_SNAPSHOT_KEYS = {"issue_id": "external_id", "project_key": "group_key"}
_LEGACY_FIELD_KEYS = {"issue_id": "external_id", "project_key": "group_key"}


def incident_key(source: str, external_key: str) -> str:
    return f"{source}:{external_key}"


def parse_incident_key(value: str, default_source: str = DEFAULT_SOURCE) -> tuple[str, str]:
    """Split ``source:external_key``. A bare key without a valid source prefix is a legacy Jira key."""
    source, sep, external = value.partition(":")
    if sep and SOURCE_ID_RE.match(source) and external:
        return source, external
    return default_source, value


def is_valid_incident_key(value: str) -> bool:
    """``source:external_key``, or a bare legacy Jira issue key (``OPS-123``)."""
    source, external = parse_incident_key(value)
    if external == value:
        return bool(LEGACY_JIRA_KEY_RE.match(value))
    return bool(SOURCE_ID_RE.match(source) and EXTERNAL_KEY_RE.match(external))


class IncidentComment(BaseModel):
    model_config = ConfigDict(frozen=True)

    comment_id: str
    body: str = ""
    author: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted: bool = False


def _rename(data: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    out = dict(data)
    for old, new in mapping.items():
        if old in out:
            value = out.pop(old)
            out.setdefault(new, value)
    return out


class IncidentSnapshot(BaseModel):
    """Best-known state of an incident, built by merging source events."""

    source: str = DEFAULT_SOURCE
    external_key: str
    external_id: str | None = None
    # Jira project / ServiceNow assignment group / mailbox / import batch.
    group_key: str | None = None
    summary: str | None = None
    description: str | None = None
    status: str | None = None
    issue_type: str | None = None
    priority: str | None = None
    labels: list[str] | None = None
    components: list[str] | None = None
    reporter: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Values of source fields mapped to canonical names (e.g. branch_code -> "1234").
    mapped_fields: dict[str, str] = Field(default_factory=dict)
    comments: list[IncidentComment] = Field(default_factory=list)
    # Last source timestamp at which each top-level attribute was written (ISO strings).
    field_versions: dict[str, str] = Field(default_factory=dict)
    deleted: bool = False

    @model_validator(mode="before")
    @classmethod
    def _legacy_shape(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = _rename(data, _LEGACY_SNAPSHOT_KEYS)
        if "issue_key" in out:
            legacy_key = out.pop("issue_key")
            out.setdefault("external_key", legacy_key)
        versions = out.get("field_versions")
        if isinstance(versions, dict):
            out["field_versions"] = _rename(versions, _LEGACY_FIELD_KEYS)
        return out

    @property
    def incident_key(self) -> str:
        return incident_key(self.source, self.external_key)

    # Deprecated Jira-named accessors (one release).
    @property
    def issue_key(self) -> str:
        return self.external_key

    @property
    def project_key(self) -> str | None:
        return self.group_key

    def active_comments(self) -> list[IncidentComment]:
        return sorted(
            (c for c in self.comments if not c.deleted),
            key=lambda c: (c.created_at.timestamp() if c.created_at else 0.0, c.comment_id),
        )


class IncidentEvent(BaseModel):
    """A source payload normalised into a stable, source-neutral shape by a source adapter."""

    source: str = DEFAULT_SOURCE
    event_type: IncidentEventType
    raw_event_name: str
    # None only for UNSUPPORTED events (which are never stored).
    external_key: str | None
    external_id: str | None = None
    group_key: str | None = None
    event_timestamp: datetime | None = None
    source_updated_at: datetime | None = None
    # Only attributes that were *present* in the payload are set.
    fields: dict[str, Any] = Field(default_factory=dict)
    mapped_fields: dict[str, str] = Field(default_factory=dict)
    comment: IncidentComment | None = None
    changed_fields: list[str] = Field(default_factory=list)
    actor: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_shape(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = _rename(data, {"issue_key": "external_key", **_LEGACY_SNAPSHOT_KEYS, "issue_fields": "fields"})
        if isinstance(out.get("fields"), dict):
            out["fields"] = _rename(out["fields"], _LEGACY_FIELD_KEYS)
        return out

    @model_validator(mode="after")
    def _typed_fields(self) -> IncidentEvent:
        # Events re-loaded from JSON (pull-source inbox envelopes) carry strings/dicts: restore the types of
        # the snapshot attributes so that merging behaves exactly as for freshly normalised events.
        self.fields = {
            k: _field_adapter(k).validate_python(v) if k in _snapshot_types() else v
            for k, v in self.fields.items()
        }
        return self

    @property
    def incident_key(self) -> str | None:
        return incident_key(self.source, self.external_key) if self.external_key else None

    # Deprecated Jira-named accessors (one release).
    @property
    def issue_key(self) -> str | None:
        return self.external_key

    @property
    def project_key(self) -> str | None:
        return self.group_key

    @property
    def issue_fields(self) -> dict[str, Any]:
        return self.fields


_FIELD_ADAPTERS: dict[str, TypeAdapter[Any]] = {}


def _snapshot_types() -> dict[str, Any]:
    return {
        name: info.annotation
        for name, info in IncidentSnapshot.model_fields.items()
        if name not in ("source", "external_key", "mapped_fields", "field_versions", "deleted")
    }


def _field_adapter(name: str) -> TypeAdapter[Any]:
    adapter = _FIELD_ADAPTERS.get(name)
    if adapter is None:
        adapter = _FIELD_ADAPTERS[name] = TypeAdapter(_snapshot_types()[name])
    return adapter

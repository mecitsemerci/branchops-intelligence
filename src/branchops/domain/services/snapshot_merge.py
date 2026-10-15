"""Timestamp-guarded merge of source events into the best-known incident snapshot.

Sources (Jira webhooks in particular) do not guarantee delivery order and update events may carry only
part of the record. The merge
therefore follows three rules:

1. An attribute is only overwritten by an event whose source timestamp is *not older* than the
   timestamp at which the attribute was last written (per-attribute version vector).
2. A ``None``/absent value never erases a known value, unless the event changelog explicitly says the
   attribute was changed (i.e. the user really cleared it).
3. Comments are merged by id and guarded by their own ``updated`` timestamp; deletions are tombstones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from branchops.domain.entities.incident import IncidentComment, IncidentEvent, IncidentSnapshot
from branchops.domain.enums import IncidentEventType

SNAPSHOT_ATTRIBUTES: tuple[str, ...] = (
    "external_id",
    "group_key",
    "summary",
    "description",
    "status",
    "issue_type",
    "priority",
    "labels",
    "components",
    "reporter",
    "created_at",
)

# Attributes whose change should trigger a new analysis.
CONTENT_ATTRIBUTES: frozenset[str] = frozenset(
    {"summary", "description", "labels", "components", "issue_type", "priority"}
)


@dataclass
class MergeOutcome:
    snapshot: IncidentSnapshot
    content_changed: bool = False
    applied: list[str] = field(default_factory=list)
    ignored_stale: list[str] = field(default_factory=list)


def _event_version(event: IncidentEvent) -> datetime | None:
    return event.source_updated_at or event.event_timestamp


def _is_newer_or_equal(candidate: datetime | None, current_iso: str | None) -> bool:
    if current_iso is None:
        return True
    if candidate is None:
        return False
    return candidate >= datetime.fromisoformat(current_iso)


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def merge_event(
    snapshot: IncidentSnapshot | None,
    event: IncidentEvent,
    *,
    redact_deleted_comments: bool = True,
) -> MergeOutcome:
    if event.external_key is None:
        raise ValueError("cannot merge an event without external key")
    if snapshot is not None and snapshot.incident_key != event.incident_key:
        raise ValueError(f"event {event.incident_key} cannot be merged into {snapshot.incident_key}")
    base = (
        snapshot.model_copy(deep=True)
        if snapshot
        else IncidentSnapshot(source=event.source, external_key=event.external_key)
    )
    outcome = MergeOutcome(snapshot=base)
    version = _event_version(event)
    version_iso = version.isoformat() if version else None
    explicitly_changed = set(event.changed_fields)

    if event.event_type == IncidentEventType.RECORD_DELETED:
        if not base.deleted:
            base.deleted = True
            outcome.content_changed = True
            outcome.applied.append("deleted")
        return outcome
    if base.deleted:
        # Deletion is terminal: late (out-of-order) updates must not resurrect a deleted issue.
        outcome.ignored_stale.append("deleted-issue")
        return outcome

    for attr in SNAPSHOT_ATTRIBUTES:
        if attr not in event.fields:
            continue
        new_value = event.fields[attr]
        current = getattr(base, attr)
        if _is_empty(new_value) and not _is_empty(current) and attr not in explicitly_changed:
            continue
        if not _is_newer_or_equal(version, base.field_versions.get(attr)):
            outcome.ignored_stale.append(attr)
            continue
        if new_value != current:
            setattr(base, attr, new_value)
            outcome.applied.append(attr)
            if attr in CONTENT_ATTRIBUTES:
                outcome.content_changed = True
        if version_iso:
            base.field_versions[attr] = version_iso

    for name, value in event.mapped_fields.items():
        key = f"mapped:{name}"
        current_mapped = base.mapped_fields.get(name)
        if _is_empty(value) and not _is_empty(current_mapped) and name not in explicitly_changed:
            continue
        if not _is_newer_or_equal(version, base.field_versions.get(key)):
            outcome.ignored_stale.append(key)
            continue
        if _is_empty(value):
            if base.mapped_fields.pop(name, None) is not None:
                outcome.applied.append(key)
                outcome.content_changed = True
        elif current_mapped != value:
            base.mapped_fields[name] = value
            outcome.applied.append(key)
            outcome.content_changed = True
        if version_iso:
            base.field_versions[key] = version_iso

    comments: list[IncidentComment] = list(event.fields.get("comments") or [])
    if event.comment is not None:
        comment = event.comment
        if event.event_type == IncidentEventType.COMMENT_DELETED:
            comment = comment.model_copy(update={"deleted": True})
        comments.append(comment)
    for comment in comments:
        if _merge_comment(base, comment, redact_deleted=redact_deleted_comments):
            outcome.applied.append(f"comment:{comment.comment_id}")
            outcome.content_changed = True

    if version and (base.updated_at is None or version > base.updated_at):
        base.updated_at = version
    return outcome


def _merge_comment(snapshot: IncidentSnapshot, incoming: IncidentComment, *, redact_deleted: bool) -> bool:
    for idx, existing in enumerate(snapshot.comments):
        if existing.comment_id != incoming.comment_id:
            continue
        if incoming.deleted:
            if existing.deleted:
                return False
            body = "" if redact_deleted else existing.body
            snapshot.comments[idx] = existing.model_copy(update={"deleted": True, "body": body})
            return True
        if existing.deleted:
            # Tombstones win: a late "updated" event must not resurrect a deleted comment.
            return False
        incoming_ts = incoming.updated_at or incoming.created_at
        existing_ts = existing.updated_at or existing.created_at
        if existing_ts and incoming_ts and incoming_ts < existing_ts:
            return False
        if incoming.body == existing.body:
            return False
        body = incoming.body if incoming.body else existing.body
        snapshot.comments[idx] = existing.model_copy(
            update={"body": body, "updated_at": incoming_ts or existing.updated_at}
        )
        return True
    if incoming.deleted:
        snapshot.comments.append(
            incoming.model_copy(update={"body": "" if redact_deleted else incoming.body})
        )
        return False
    snapshot.comments.append(incoming)
    return True

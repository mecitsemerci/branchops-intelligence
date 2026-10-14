"""Timestamp-guarded merge of webhook events into the best-known issue snapshot.

Jira does not guarantee delivery order and update webhooks may carry only part of the issue. The merge
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

from branchops.domain.entities.jira import JiraComment, JiraIssueSnapshot, NormalizedJiraEvent
from branchops.domain.enums import JiraEventType

SNAPSHOT_ATTRIBUTES: tuple[str, ...] = (
    "issue_id",
    "project_key",
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
    snapshot: JiraIssueSnapshot
    content_changed: bool = False
    applied: list[str] = field(default_factory=list)
    ignored_stale: list[str] = field(default_factory=list)


def _event_version(event: NormalizedJiraEvent) -> datetime | None:
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
    snapshot: JiraIssueSnapshot | None,
    event: NormalizedJiraEvent,
    *,
    redact_deleted_comments: bool = True,
) -> MergeOutcome:
    if event.issue_key is None:
        raise ValueError("cannot merge an event without issue key")
    base = snapshot.model_copy(deep=True) if snapshot else JiraIssueSnapshot(issue_key=event.issue_key)
    outcome = MergeOutcome(snapshot=base)
    version = _event_version(event)
    version_iso = version.isoformat() if version else None
    explicitly_changed = set(event.changed_fields)

    if event.event_type == JiraEventType.ISSUE_DELETED:
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
        if attr not in event.issue_fields:
            continue
        new_value = event.issue_fields[attr]
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

    comments: list[JiraComment] = list(event.issue_fields.get("comments") or [])
    if event.comment is not None:
        comment = event.comment
        if event.event_type == JiraEventType.COMMENT_DELETED:
            comment = comment.model_copy(update={"deleted": True})
        comments.append(comment)
    for comment in comments:
        if _merge_comment(base, comment, redact_deleted=redact_deleted_comments):
            outcome.applied.append(f"comment:{comment.comment_id}")
            outcome.content_changed = True

    if version and (base.updated_at is None or version > base.updated_at):
        base.updated_at = version
    return outcome


def _merge_comment(snapshot: JiraIssueSnapshot, incoming: JiraComment, *, redact_deleted: bool) -> bool:
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

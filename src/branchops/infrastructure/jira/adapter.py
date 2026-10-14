"""Adapter from Jira webhook payload variants (Data Center/Server and Cloud) to ``NormalizedJiraEvent``.

Variations handled:

* ``webhookEvent`` names (``jira:issue_*``, ``comment_*``) and legacy comment notifications delivered as
  ``jira:issue_updated`` with ``issue_event_type_name`` = ``issue_commented`` / ``issue_comment_edited`` /
  ``issue_comment_deleted``.
* Plain-text descriptions (Server/DC) and Atlassian Document Format (Cloud REST v3).
* Custom field values given as scalars, ``{"value": ...}``, ``{"name": ...}``, cascading selects and lists.
* Partial payloads: only attributes present in the payload are emitted; values that only appear in the
  changelog (``toString``) are used when the field itself is absent.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from branchops.domain.entities.jira import JiraComment, NormalizedJiraEvent
from branchops.domain.enums import JiraEventType

ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,29}-\d{1,9}$")

_EVENT_MAP: dict[str, JiraEventType] = {
    "jira:issue_created": JiraEventType.ISSUE_CREATED,
    "jira:issue_updated": JiraEventType.ISSUE_UPDATED,
    "jira:issue_deleted": JiraEventType.ISSUE_DELETED,
    "comment_created": JiraEventType.COMMENT_CREATED,
    "comment_updated": JiraEventType.COMMENT_UPDATED,
    "comment_deleted": JiraEventType.COMMENT_DELETED,
}
_LEGACY_COMMENT_EVENTS: dict[str, JiraEventType] = {
    "issue_commented": JiraEventType.COMMENT_CREATED,
    "issue_comment_edited": JiraEventType.COMMENT_UPDATED,
    "issue_comment_deleted": JiraEventType.COMMENT_DELETED,
}
# Jira changelog field name -> snapshot attribute
_CHANGELOG_FIELDS: dict[str, str] = {
    "summary": "summary",
    "description": "description",
    "status": "status",
    "priority": "priority",
    "issuetype": "issue_type",
    "labels": "labels",
    "component": "components",
    "components": "components",
}

TextTransform = Callable[[str], str]


class JiraPayloadError(ValueError):
    """Payload is structurally unusable (not a Jira webhook we can process)."""


def parse_jira_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000.0, tz=UTC)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text) / 1000.0, tz=UTC)
    # Jira uses "+0300" offsets; Python 3.12 fromisoformat accepts "+03:00" and "Z".
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def adf_to_text(node: Any) -> str:
    """Flatten an Atlassian Document Format node to plain text, keeping paragraph/list structure."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    ntype = node.get("type")
    if ntype == "text":
        return str(node.get("text", ""))
    if ntype == "hardBreak":
        return "\n"
    if ntype == "mention":
        return str(node.get("attrs", {}).get("text", ""))
    inner = adf_to_text(node.get("content", []))
    if ntype in ("paragraph", "heading", "codeBlock", "blockquote"):
        return inner.rstrip() + "\n"
    if ntype == "listItem":
        return "- " + inner.strip() + "\n"
    return inner


def rich_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict) and value.get("type") == "doc":
        return adf_to_text(value).strip()
    if isinstance(value, str):
        return value
    return str(value)


def custom_field_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str | int | float):
        text = str(value).strip()
        return text or None
    if isinstance(value, list):
        parts = [p for p in (custom_field_text(v) for v in value) if p]
        return ", ".join(parts) or None
    if isinstance(value, dict):
        if value.get("type") == "doc":
            return rich_text(value) or None
        base = value.get("value") or value.get("name") or value.get("displayName") or value.get("key")
        child = value.get("child")
        base_text = custom_field_text(base)
        child_text = custom_field_text(child)
        if base_text and child_text:
            return f"{base_text} / {child_text}"
        return base_text or child_text
    return None


def _user_name(user: Any) -> str | None:
    if not isinstance(user, dict):
        return None
    return user.get("displayName") or user.get("name") or user.get("accountId")


def _named(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("name") or value.get("value")
    return value if isinstance(value, str) else None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def resolve_event_type(payload: Mapping[str, Any]) -> tuple[JiraEventType, str]:
    raw = str(payload.get("webhookEvent") or "")
    event_type = _EVENT_MAP.get(raw, JiraEventType.UNSUPPORTED)
    sub = str(payload.get("issue_event_type_name") or "")
    if event_type == JiraEventType.ISSUE_UPDATED and sub in _LEGACY_COMMENT_EVENTS and payload.get("comment"):
        event_type = _LEGACY_COMMENT_EVENTS[sub]
    return event_type, raw or "<missing>"


def _comment(raw: Any, transform: TextTransform) -> JiraComment | None:
    if not isinstance(raw, dict) or raw.get("id") is None:
        return None
    body = rich_text(raw.get("body")) or ""
    return JiraComment(
        comment_id=str(raw["id"]),
        body=transform(body),
        author=_user_name(raw.get("author") or raw.get("updateAuthor")),
        created_at=parse_jira_datetime(raw.get("created")),
        updated_at=parse_jira_datetime(raw.get("updated")),
    )


class JiraWebhookAdapter:
    def __init__(self, field_mapping: Mapping[str, str] | None = None) -> None:
        self._field_mapping = dict(field_mapping or {})

    def normalize(
        self, payload: Mapping[str, Any], transform: TextTransform | None = None
    ) -> NormalizedJiraEvent:
        """Normalise a webhook payload. ``transform`` is applied to every free-text value (masking)."""
        if not isinstance(payload, Mapping):
            raise JiraPayloadError("payload must be a JSON object")
        tx: TextTransform = transform or (lambda s: s)
        event_type, raw_name = resolve_event_type(payload)
        issue = _as_dict(payload.get("issue"))
        fields = _as_dict(issue.get("fields"))

        issue_key = issue.get("key")
        if issue_key is not None and not ISSUE_KEY_RE.match(str(issue_key)):
            raise JiraPayloadError("issue.key has an invalid format")
        if event_type != JiraEventType.UNSUPPORTED and issue_key is None:
            raise JiraPayloadError("issue.key is required for issue and comment events")

        project = fields.get("project")
        project_key = project.get("key") if isinstance(project, dict) else None
        if project_key is None and issue_key:
            project_key = str(issue_key).split("-")[0]

        changelog_items = (payload.get("changelog") or {}).get("items") or []
        changed: dict[str, Any] = {}
        for item in changelog_items if isinstance(changelog_items, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("fieldId") or item.get("field") or "").lower()
            changed[name] = item.get("toString")

        issue_fields: dict[str, Any] = {}
        if issue.get("id") is not None:
            issue_fields["issue_id"] = str(issue["id"])
        if project_key:
            issue_fields["project_key"] = project_key
        if "summary" in fields:
            issue_fields["summary"] = tx(rich_text(fields["summary"]) or "") or None
        if "description" in fields:
            desc = rich_text(fields["description"])
            issue_fields["description"] = tx(desc) if desc else None
        if "status" in fields:
            issue_fields["status"] = _named(fields["status"])
        if "issuetype" in fields:
            issue_fields["issue_type"] = _named(fields["issuetype"])
        if "priority" in fields:
            issue_fields["priority"] = _named(fields["priority"])
        if "labels" in fields and isinstance(fields["labels"], list):
            issue_fields["labels"] = [str(x) for x in fields["labels"]]
        if "components" in fields and isinstance(fields["components"], list):
            issue_fields["components"] = [c for c in (_named(x) for x in fields["components"]) if c]
        if "reporter" in fields:
            issue_fields["reporter"] = _user_name(fields["reporter"])
        if "created" in fields:
            issue_fields["created_at"] = parse_jira_datetime(fields["created"])
        comment_block = fields.get("comment")
        if isinstance(comment_block, dict) and isinstance(comment_block.get("comments"), list):
            issue_fields["comments"] = [
                c for c in (_comment(raw, tx) for raw in comment_block["comments"]) if c is not None
            ]

        # Changelog fallback for partial payloads.
        for jira_name, attr in _CHANGELOG_FIELDS.items():
            if jira_name in changed and attr not in issue_fields:
                value = changed[jira_name]
                if attr == "labels":
                    issue_fields[attr] = str(value).split() if value else []
                elif attr == "components":
                    issue_fields[attr] = [value] if value else []
                elif attr in ("summary", "description"):
                    issue_fields[attr] = tx(str(value)) if value else None
                else:
                    issue_fields[attr] = value

        mapped: dict[str, str] = {}
        changed_fields = [_CHANGELOG_FIELDS.get(n, n) for n in changed]
        for canonical, field_id in self._field_mapping.items():
            if field_id in fields:
                text = custom_field_text(fields[field_id])
                mapped[canonical] = tx(text) if text else ""
            elif field_id.lower() in changed:
                value = changed[field_id.lower()]
                mapped[canonical] = tx(str(value)) if value else ""
            if field_id.lower() in changed:
                changed_fields.append(canonical)

        comment = _comment(payload.get("comment"), tx)
        if (
            event_type
            in (
                JiraEventType.COMMENT_CREATED,
                JiraEventType.COMMENT_UPDATED,
                JiraEventType.COMMENT_DELETED,
            )
            and comment is None
        ):
            raise JiraPayloadError("comment event without a comment id")

        return NormalizedJiraEvent(
            event_type=event_type,
            raw_event_name=raw_name,
            issue_key=str(issue_key) if issue_key else None,
            issue_id=str(issue["id"]) if issue.get("id") is not None else None,
            project_key=project_key,
            event_timestamp=parse_jira_datetime(payload.get("timestamp")),
            source_updated_at=parse_jira_datetime(fields.get("updated"))
            or (comment.updated_at if comment else None),
            issue_fields=issue_fields,
            mapped_fields=mapped,
            comment=comment,
            changed_fields=changed_fields,
            actor=_user_name(payload.get("user")),
        )

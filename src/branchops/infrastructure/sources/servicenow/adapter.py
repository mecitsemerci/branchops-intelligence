"""Push source adapter for ServiceNow ``incident`` webhooks producing ``IncidentEvent``.

**Example / reference adapter.** ServiceNow has no built-in outbound webhook format: the payload is whatever
the bank's *Business Rule -> outbound REST* script sends. This adapter documents one reasonable shape and
must be adapted (together with ``field_mapping``) to the Business Rule actually deployed.

Expected payload (one JSON object per record change)::

    {
      "event": "insert" | "update" | "delete",
      "table": "incident",                       # optional, defaults to "incident"
      "sys_id": "9d385017c611228701d22104cc95c371",
      "number": "INC0012345",
      "short_description": "...", "description": "...",
      "state": "In Progress", "priority": "2 - High",
      "assignment_group": "Şube Uygulamaları Destek",
      "caller_id": "...", "opened_by": "...",     # reporter (caller_id preferred)
      "category": "...", "subcategory": "...",    # optional -> components
      "opened_at": "2026-10-15 06:05:10",
      "sys_updated_on": "2026-10-15 09:12:44",    # UTC 'YYYY-MM-DD HH:MM:SS' (ISO 8601 also accepted)
      "sys_updated_by": "ayse.yilmaz",
      "comments": "...", "work_notes": "...",     # only the journal entry added by this change
      "changed_fields": ["state", "priority"],    # optional
      "u_branch_code": "1234", "cmdb_ci": {...}   # any columns referenced by field_mapping
    }

Every value may be a plain string or a ``{"value": ..., "display_value": ...}`` object
(``sysparm_display_value=all`` style). ``display_value`` is preferred for text and reference columns; for
date/time columns the internal ``value`` (always UTC) is preferred because display values are rendered in
the user's time zone.

Sample Business Rule (table ``incident``, *after* insert/update/delete, async)::

    (function executeRule(current, previous) {
        var r = new sn_ws.RESTMessageV2();
        r.setEndpoint('https://branchops.bank.local/api/ingest/snow');
        r.setHttpMethod('POST');
        r.setRequestHeader('Content-Type', 'application/json');
        r.setRequestHeader('X-BranchOps-Webhook-Token', gs.getProperty('x_bank.branchops.token'));
        var body = {
            event: current.operation(),
            table: current.getTableName(),
            sys_id: current.getUniqueValue(),
            number: current.getValue('number'),
            short_description: current.getValue('short_description'),
            description: current.getValue('description'),
            state: current.getDisplayValue('state'),
            priority: current.getDisplayValue('priority'),
            assignment_group: current.getDisplayValue('assignment_group'),
            caller_id: current.getDisplayValue('caller_id'),
            category: current.getDisplayValue('category'),
            subcategory: current.getDisplayValue('subcategory'),
            opened_at: current.getValue('opened_at'),
            sys_updated_on: current.getValue('sys_updated_on'),
            sys_updated_by: current.getValue('sys_updated_by'),
            comments: current.comments.changes() ? current.comments.getJournalEntry(1) : '',
            work_notes: current.work_notes.changes() ? current.work_notes.getJournalEntry(1) : '',
            changed_fields: GlideScriptRecordUtil.get(current).getChangedFieldNames().toArray(),
            u_branch_code: current.getValue('u_branch_code'),
            cmdb_ci: {value: current.getValue('cmdb_ci'), display_value: current.getDisplayValue('cmdb_ci')}
        };
        r.setRequestBody(JSON.stringify(body));
        r.executeAsync();
    })(current, previous);

Journal fields: a non-empty ``comments`` / ``work_notes`` value on an insert or update is the entry added by
that change. It is emitted as an ``IncidentComment`` whose id is derived from ``sys_id`` + ``sys_updated_on``
+ journal kind, so redeliveries of the same change deduplicate.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from branchops.application.interfaces.source_adapter import (
    SourcePayloadError,
    TextTransform,
    payload_digest,
)
from branchops.config.settings import SourceAuthSettings, SourceSettings
from branchops.domain.entities.incident import EXTERNAL_KEY_RE, IncidentComment, IncidentEvent
from branchops.domain.enums import IncidentEventType
from branchops.infrastructure.security.webhook_auth import authenticate_webhook

SUPPORTED_TABLE = "incident"
JOURNAL_KINDS: tuple[str, ...] = ("comments", "work_notes")

_EVENT_MAP: dict[str, IncidentEventType] = {
    "insert": IncidentEventType.RECORD_CREATED,
    "update": IncidentEventType.RECORD_UPDATED,
    "delete": IncidentEventType.RECORD_DELETED,
}
# ServiceNow column -> snapshot attribute (used to translate ``changed_fields``).
_COLUMN_ATTRIBUTES: dict[str, str] = {
    "sys_id": "external_id",
    "assignment_group": "group_key",
    "short_description": "summary",
    "description": "description",
    "state": "status",
    "priority": "priority",
    "caller_id": "reporter",
    "opened_by": "reporter",
    "category": "components",
    "subcategory": "components",
    "opened_at": "created_at",
}
_SN_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class ServiceNowPayloadError(SourcePayloadError):
    """Payload is structurally unusable (not a ServiceNow incident webhook we can process)."""


def sn_text(value: Any) -> str | None:
    """Text of a column value; ``{"value", "display_value"}`` objects prefer the display value."""
    if value is None:
        return None
    if isinstance(value, dict):
        display = value.get("display_value")
        if display not in (None, ""):
            return sn_text(display)
        return sn_text(value.get("value"))
    if isinstance(value, list):
        parts = [p for p in (sn_text(v) for v in value) if p]
        return ", ".join(parts)
    return str(value).strip()


def sn_raw(value: Any) -> str | None:
    """Internal value of a column (``value`` preferred): sys ids and UTC date/times."""
    if isinstance(value, dict):
        raw = value.get("value")
        return sn_raw(raw) if raw not in (None, "") else sn_text(value.get("display_value"))
    if value is None:
        return None
    return str(value).strip()


def parse_servicenow_datetime(value: Any) -> datetime | None:
    """Parse ServiceNow ``YYYY-MM-DD HH:MM:SS`` (UTC) or ISO 8601; naive values are UTC."""
    text = sn_raw(value)
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, _SN_DATETIME_FORMAT)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def journal_comment_id(sys_id: str, sys_updated_on: str, kind: str) -> str:
    digest = hashlib.sha256(f"{sys_id}|{sys_updated_on}|{kind}".encode()).hexdigest()
    return f"{kind}-{digest[:24]}"


class ServiceNowWebhookAdapter:
    """``PushSourceAdapter`` for ServiceNow Business Rule webhooks on the ``incident`` table."""

    def __init__(self, settings: SourceSettings) -> None:
        self.source_id = settings.id
        self._field_mapping = dict(settings.field_mapping)
        # The registry always provides auth for push sources; the fallback only serves direct construction.
        self._auth = settings.auth or SourceAuthSettings(
            mode="token",
            secrets_env="SERVICENOW_WEBHOOK_SECRETS",
            query_token_secrets_env="SERVICENOW_WEBHOOK_QUERY_TOKENS",  # noqa: S106 - env var name
        )

    def authenticate(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
        query: Mapping[str, str],
        client_host: str | None,
    ) -> str:
        return authenticate_webhook(
            body=body,
            headers=headers,
            query=query,
            client_host=client_host,
            settings=self._auth,
            secrets=self._auth.secrets(),
            query_tokens=self._auth.query_tokens(),
        )

    def idempotency_key(self, payload: Mapping[str, Any], headers: Mapping[str, str]) -> str:
        sys_id = sn_raw(payload.get("sys_id"))
        updated_on = sn_raw(payload.get("sys_updated_on"))
        if sys_id and updated_on:
            return f"{sys_id}:{updated_on}:{sn_raw(payload.get('event')) or ''}"
        return f"sha256:{payload_digest(payload)}"

    def payload_timestamp(self, payload: Mapping[str, Any]) -> datetime | None:
        """``sys_updated_on`` of the record (the Business Rule sends it with every event)."""
        return parse_servicenow_datetime(sn_raw(payload.get("sys_updated_on")))

    def normalize(self, payload: Mapping[str, Any], transform: TextTransform | None = None) -> IncidentEvent:
        """Normalise a webhook payload. ``transform`` is applied to every free-text value (masking)."""
        if not isinstance(payload, Mapping):
            raise ServiceNowPayloadError("payload must be a JSON object")
        tx: TextTransform = transform or (lambda s: s)

        event_name = (sn_raw(payload.get("event")) or "").lower()
        table = sn_raw(payload.get("table")) or SUPPORTED_TABLE
        raw_name = f"servicenow:{table}.{event_name or '<missing>'}"
        event_type = _EVENT_MAP.get(event_name, IncidentEventType.UNSUPPORTED)
        if table != SUPPORTED_TABLE:
            event_type = IncidentEventType.UNSUPPORTED

        number = sn_raw(payload.get("number"))
        if event_type == IncidentEventType.UNSUPPORTED:
            valid = number if number and EXTERNAL_KEY_RE.match(number) else None
            return IncidentEvent(
                source=self.source_id,
                event_type=event_type,
                raw_event_name=raw_name,
                external_key=valid,
                event_timestamp=datetime.now(UTC),
            )
        if not number:
            raise ServiceNowPayloadError("number is required for incident events")
        if not EXTERNAL_KEY_RE.match(number):
            raise ServiceNowPayloadError("number has an invalid format")

        sys_id = sn_raw(payload.get("sys_id")) or None
        updated_on_raw = sn_raw(payload.get("sys_updated_on")) or ""
        source_updated_at = parse_servicenow_datetime(updated_on_raw)
        actor = sn_text(payload.get("sys_updated_by")) or None

        fields: dict[str, Any] = {}
        if sys_id:
            fields["external_id"] = sys_id
        group_key: str | None = None
        if "assignment_group" in payload:
            group_key = sn_text(payload["assignment_group"]) or None
            fields["group_key"] = group_key
        if "short_description" in payload:
            summary = sn_text(payload["short_description"])
            fields["summary"] = tx(summary) if summary else None
        if "description" in payload:
            desc = sn_text(payload["description"])
            fields["description"] = tx(desc) if desc else None
        if "state" in payload:
            fields["status"] = sn_text(payload["state"]) or None
        if "priority" in payload:
            fields["priority"] = sn_text(payload["priority"]) or None
        if "caller_id" in payload or "opened_by" in payload:
            reporter = sn_text(payload.get("caller_id")) or sn_text(payload.get("opened_by"))
            fields["reporter"] = reporter or None
        if "category" in payload or "subcategory" in payload:
            fields["components"] = [
                c for c in (sn_text(payload.get("category")), sn_text(payload.get("subcategory"))) if c
            ]
        if "opened_at" in payload:
            fields["created_at"] = parse_servicenow_datetime(payload["opened_at"])

        comments: list[IncidentComment] = []
        if event_type != IncidentEventType.RECORD_DELETED:
            for kind in JOURNAL_KINDS:
                body = sn_text(payload.get(kind))
                if not body:
                    continue
                if not sys_id or not updated_on_raw:
                    raise ServiceNowPayloadError(f"{kind} journal entry requires sys_id and sys_updated_on")
                comments.append(
                    IncidentComment(
                        comment_id=journal_comment_id(sys_id, updated_on_raw, kind),
                        body=tx(body),
                        author=actor,
                        created_at=source_updated_at,
                        updated_at=source_updated_at,
                    )
                )
        if comments:
            fields["comments"] = comments

        mapped: dict[str, str] = {}
        for canonical, column in self._field_mapping.items():
            if column in payload:
                text = sn_text(payload[column])
                mapped[canonical] = tx(text) if text else ""

        changed_fields: list[str] = []
        raw_changed = payload.get("changed_fields")
        if isinstance(raw_changed, list):
            column_to_canonical = {column: canonical for canonical, column in self._field_mapping.items()}
            for column in (str(c) for c in raw_changed):
                name = _COLUMN_ATTRIBUTES.get(column) or column_to_canonical.get(column)
                if name and name not in changed_fields:
                    changed_fields.append(name)

        return IncidentEvent(
            source=self.source_id,
            event_type=event_type,
            raw_event_name=raw_name,
            external_key=number,
            external_id=sys_id,
            group_key=group_key,
            # Without sys_updated_on the receive time is the only version available; the merge rejects
            # timestamp-less events for attributes that already carry a version.
            event_timestamp=source_updated_at or datetime.now(UTC),
            source_updated_at=source_updated_at,
            fields=fields,
            mapped_fields=mapped,
            changed_fields=changed_fields,
            actor=actor,
        )

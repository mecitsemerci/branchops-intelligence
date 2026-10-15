"""Commands behind the MCP reporting tools: check a draft, report an incident, add an update.

Reports become ordinary incidents of the ``mcp_report`` source: the command writes a canonical
``IncidentEvent`` envelope to the inbox (and, when a ticketing sink is configured, a ``ticketing_outbox`` row)
in **one transaction**; the worker then merges and analyses it like any other source. No model is called
here: the draft check is deterministic and based on the template the incident would be scored with.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from branchops.application.interfaces.source_adapter import event_envelope, payload_digest
from branchops.config.settings import Settings
from branchops.domain.entities.incident import IncidentComment, IncidentEvent, parse_incident_key
from branchops.domain.entities.issue import FIELD_LABELS_TR
from branchops.domain.entities.report import DraftCheck, FieldGap, IncidentDraft
from branchops.domain.enums import IncidentEventType
from branchops.domain.services.masking import SensitiveDataMasker
from branchops.domain.services.prompt_injection import detect_prompt_injection
from branchops.infrastructure.database.models import Issue, WebhookInbox
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.persistence.inbox_repository import InboxRepository, NewInboxEvent
from branchops.infrastructure.ticketing.handler import TICKETING_OUTBOX, create_key
from branchops.templates.registry import TemplateRegistry
from branchops.templates.validation import is_applicable, is_required, validate_value

# Canonical fields that become mapped (known) fields of the reported incident; summary / description are
# the record text itself.
_KNOWN_FACTS = (
    "branch_code",
    "application",
    "application_version",
    "business_process",
    "business_impact",
    "error_details",
    "occurrence_time",
    "reproduction_steps",
    "frequency",
    "environment",
    "troubleshooting_steps",
    "evidence",
)


# Hooks run inside the reporting transaction: (session, incident_key, created) / (session, key, comment_id).
AfterReport = Callable[[Session, str, bool], "dict[str, Any] | None"]
AfterUpdate = Callable[[Session, str, str], None]


class ReportError(ValueError):
    """The report cannot be accepted (reporting disabled, unknown incident, wrong source …)."""


def check_draft(
    draft: IncidentDraft, registry: TemplateRegistry, known: dict[str, str] | None = None
) -> DraftCheck:
    """Which fields of the applicable template are missing or invalid (deterministic, no model).

    ``known`` are facts the server adds itself (e.g. the application title from the ownership registry).
    """
    values = {**draft.canonical_values(), **(known or {})}
    template = registry.select(values.get("category"), values.get("subcategory"), values)
    missing_required: list[FieldGap] = []
    missing_optional: list[FieldGap] = []
    invalid: list[FieldGap] = []
    for f in template.fields:
        if f.name in ("category", "subcategory") or not is_applicable(f, values):
            continue  # classification is BranchOps' job, not the reporter's
        required = is_required(f, values)
        value = values.get(f.name)
        gap = FieldGap(
            field=f.name,
            label=f.label or FIELD_LABELS_TR.get(f.name, f.name),
            required=required,
            recommendation=f.recommendation,
        )
        if not value:
            (missing_required if required else missing_optional).append(gap)
            continue
        problems = validate_value(f, value)
        if problems:
            invalid.append(gap.model_copy(update={"problems": problems}))
    return DraftCheck(
        ready=not missing_required and not invalid,
        template_id=template.template_id,
        template_version=template.version,
        template_name=template.name,
        missing_required=missing_required,
        missing_optional=missing_optional,
        invalid=invalid,
        security_flags=detect_prompt_injection(draft.texts()),
    )


@dataclass(frozen=True)
class ReportResult:
    incident_key: str
    external_key: str
    created: bool
    event_id: str | None
    ticketing: str  # "queued" | "disabled"
    check: DraftCheck
    extra: dict[str, Any] = field(default_factory=dict)


def _external_key(prefix: str, reporter: str, idempotency_key: str | None) -> str:
    if idempotency_key:
        digest = hashlib.sha256(f"{reporter}\x00{idempotency_key}".encode()).digest()
    else:
        digest = secrets.token_bytes(8)
    return f"{prefix}-{base64.b32encode(digest).decode()[:10]}"


class ReportIncident:
    def __init__(self, db: Database, settings: Settings, registry: TemplateRegistry) -> None:
        if not settings.reporting.enabled:
            raise ReportError("incident reporting is disabled (reporting.enabled)")
        self._db = db
        self._settings = settings
        self._registry = registry
        self._inbox = InboxRepository()
        self._masker = SensitiveDataMasker(settings.security.masking_extra_patterns)

    @property
    def source_id(self) -> str:
        return self._settings.reporting.source_id

    def build_event(
        self,
        draft: IncidentDraft,
        *,
        external_key: str,
        reporter: str,
        group_key: str | None,
        known: dict[str, str] | None = None,
        now: datetime | None = None,
    ) -> IncidentEvent:
        now = now or datetime.now(UTC)
        values = draft.canonical_values()
        mapped = {k: values[k] for k in _KNOWN_FACTS if values.get(k)}
        mapped.update({k: v for k, v in (known or {}).items() if v})
        if draft.category:
            mapped["reported_category"] = draft.category
        if draft.subcategory:
            mapped["reported_subcategory"] = draft.subcategory
        return IncidentEvent(
            source=self.source_id,
            event_type=IncidentEventType.RECORD_CREATED,
            raw_event_name="mcp:report_incident",
            external_key=external_key,
            group_key=group_key or self._settings.reporting.default_group_key,
            event_timestamp=now,
            source_updated_at=now,
            fields={
                "summary": draft.title,
                "description": draft.description[: self._settings.reporting.max_description_chars],
                "status": "Reported",
                "reporter": reporter,
                "created_at": now,
                "updated_at": now,
            },
            mapped_fields=mapped,
            actor=reporter,
        )

    def execute(
        self,
        draft: IncidentDraft,
        *,
        reporter: str,
        correlation_id: str,
        idempotency_key: str | None = None,
        group_key: str | None = None,
        known: dict[str, str] | None = None,
        project_key: str | None = None,
        after_insert: AfterReport | None = None,
    ) -> ReportResult:
        """Insert the event (+ ticket request) atomically. ``after_insert(session, incident_key, created)``
        runs in the same transaction (used for attachment binding, routing and notifications)."""
        check = check_draft(draft, self._registry, known)
        external_key = _external_key(self._settings.reporting.key_prefix, reporter, idempotency_key)
        incident_key = f"{self.source_id}:{external_key}"
        event = self.build_event(
            draft, external_key=external_key, reporter=reporter, group_key=group_key, known=known
        )
        envelope = event_envelope(event)
        new = NewInboxEvent(
            idempotency_key=f"{self.source_id}:report:{external_key}",
            correlation_id=correlation_id,
            event_type=event.event_type.value,
            raw_event_name=event.raw_event_name,
            source=self.source_id,
            issue_key=external_key,
            group_key=event.group_key,
            event_timestamp=event.event_timestamp,
            payload=envelope,
            payload_sha256=payload_digest(envelope),
            max_attempts=self._settings.worker.max_attempts,
        )
        ticketing = "disabled"
        extra: dict[str, Any] = {}
        with self._db.session() as s:
            event_id, created = self._inbox.insert(s, new)
            if created and self._settings.ticketing.sink != "none":
                self._enqueue_ticket(s, incident_key, draft, project_key)
                ticketing = "queued"
            elif not created and self._settings.ticketing.sink != "none":
                ticketing = "queued"
            if after_insert is not None:
                extra = after_insert(s, incident_key, created) or {}
        return ReportResult(
            incident_key=incident_key,
            external_key=external_key,
            created=created,
            event_id=str(event_id) if event_id else None,
            ticketing=ticketing,
            check=check,
            extra=extra,
        )

    def _enqueue_ticket(
        self, s: Session, incident_key: str, draft: IncidentDraft, project_key: str | None
    ) -> None:
        cfg = self._settings.ticketing
        link = (
            f"{self._settings.dashboard.public_url.rstrip('/')}/issues/{incident_key}"
            if self._settings.dashboard.public_url
            else None
        )
        description = "\n\n".join(
            x
            for x in (
                self._masker.mask_text(draft.description),
                f"BranchOps kaydı: {incident_key}" + (f" — {link}" if link else ""),
            )
            if x
        )
        TICKETING_OUTBOX.enqueue(
            s,
            idempotency_key=create_key(incident_key),
            kind="create_ticket",
            incident_key=incident_key,
            payload={
                "project_key": project_key or cfg.default_project_key,
                "summary": self._masker.mask_text(draft.title) or draft.title,
                "description": description,
                "labels": list(cfg.labels),
            },
            max_attempts=self._settings.outbox.max_attempts,
        )


class AddIncidentUpdate:
    """Append a comment to a *reported* incident (other sources are owned by their source systems)."""

    def __init__(self, db: Database, settings: Settings) -> None:
        if not settings.reporting.enabled:
            raise ReportError("incident reporting is disabled (reporting.enabled)")
        self._db = db
        self._settings = settings
        self._inbox = InboxRepository()
        self._masker = SensitiveDataMasker(settings.security.masking_extra_patterns)

    def execute(
        self,
        incident_key: str,
        body: str,
        *,
        author: str,
        correlation_id: str,
        after_insert: AfterUpdate | None = None,
    ) -> tuple[str, str | None]:
        source, external_key = parse_incident_key(incident_key)
        if source != self._settings.reporting.source_id:
            raise ReportError("updates can only be added to incidents reported through BranchOps")
        key = f"{source}:{external_key}"
        now = datetime.now(UTC)
        comment_id = uuid.uuid4().hex[:16]
        event = IncidentEvent(
            source=source,
            event_type=IncidentEventType.COMMENT_CREATED,
            raw_event_name="mcp:add_incident_update",
            external_key=external_key,
            event_timestamp=now,
            comment=IncidentComment(
                comment_id=comment_id,
                body=body[: self._settings.reporting.max_update_chars],
                author=author,
                created_at=now,
                updated_at=now,
            ),
            actor=author,
        )
        envelope = event_envelope(event)
        with self._db.session() as s:
            if not self._known(s, key, external_key):
                raise ReportError("incident not found")
            event_id, _ = self._inbox.insert(
                s,
                NewInboxEvent(
                    idempotency_key=f"{source}:update:{external_key}:{comment_id}",
                    correlation_id=correlation_id,
                    event_type=event.event_type.value,
                    raw_event_name=event.raw_event_name,
                    source=source,
                    issue_key=external_key,
                    group_key=None,
                    event_timestamp=now,
                    payload=envelope,
                    payload_sha256=payload_digest(envelope),
                    max_attempts=self._settings.worker.max_attempts,
                ),
            )
            if self._settings.ticketing.sink != "none":
                TICKETING_OUTBOX.enqueue(
                    s,
                    idempotency_key=f"ticket:{key}:comment:{comment_id}",
                    kind="add_comment",
                    incident_key=key,
                    payload={"body": f"{author}: {self._masker.mask_text(body) or ''}"},
                    max_attempts=self._settings.outbox.max_attempts,
                )
            if after_insert is not None:
                after_insert(s, key, comment_id)
        return comment_id, str(event_id) if event_id else None

    @staticmethod
    def _known(s: Session, key: str, external_key: str) -> bool:
        """The incident exists (analysed) or its report is still waiting in the inbox."""
        if s.get(Issue, key) is not None:
            return not bool(s.get(Issue, key).is_deleted)  # type: ignore[union-attr]
        source = key.split(":", 1)[0]
        pending = s.execute(
            select(WebhookInbox.id)
            .where(WebhookInbox.source == source, WebhookInbox.issue_key == external_key)
            .limit(1)
        ).scalar_one_or_none()
        return pending is not None

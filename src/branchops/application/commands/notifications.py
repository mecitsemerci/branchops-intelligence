"""Notification composition and enqueueing (``notification_outbox``); delivery happens in the worker.

Messages are rendered from Turkish Jinja templates (``templates/notifications``) with **masked** data only,
at enqueue time, so the outbox row is an exact record of what was sent. One row per (incident, event,
channel, recipient): repeating an event never notifies twice.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from branchops.application.interfaces.notifications import NotificationMessage
from branchops.config.settings import Settings
from branchops.domain.entities.issue import FIELD_LABELS_TR
from branchops.domain.entities.ownership import MatchedLevel, OwnershipRegistry, Team
from branchops.domain.services.masking import SensitiveDataMasker
from branchops.domain.services.working_hours import within_working_hours
from branchops.infrastructure.database.models import (
    Analysis,
    Attachment,
    IncidentAppContext,
    IncidentRouting,
    Issue,
    NotificationOutbox,
    WebhookInbox,
)
from branchops.infrastructure.persistence.outbox_repository import OutboxRepository

NOTIFICATION_OUTBOX = OutboxRepository(
    NotificationOutbox, extra_columns=("event", "channel", "recipient", "role")
)
EVENTS = ("incident_reported", "analysis_ready", "ownership_unresolved", "update_added", "test")

LEVEL_TR = {
    MatchedLevel.SCREEN_KEY: "ekran (anahtar)",
    MatchedLevel.SCREEN_ROUTE: "ekran (rota)",
    MatchedLevel.APPLICATION_DEFAULT_SCREEN_OWNER: "uygulamanın varsayılan ekran sahibi",
    MatchedLevel.APPLICATION_OWNER: "uygulama sahibi",
    MatchedLevel.ROUTING_RULE: "yönlendirme kuralı",
    MatchedLevel.BRANCH_OPS_FALLBACK: "sahip bulunamadı — şube operasyon",
}
SUBJECTS = {
    "incident_reported": "[BranchOps] Yeni bildirim {incident_key}: {summary}",
    "analysis_ready": "[BranchOps] Analiz tamamlandı {incident_key}: {summary}",
    "ownership_unresolved": "[BranchOps] Sahipsiz ekran {incident_key}: {summary}",
    "update_added": "[BranchOps] Güncelleme {incident_key}: {summary}",
    "test": "[BranchOps] Test bildirimi — {team_name}",
}


@dataclass
class IncidentFacts:
    """Masked, template-ready facts about an incident."""

    incident_key: str
    summary: str = ""
    description_excerpt: str = ""
    application: str | None = None
    application_key: str | None = None
    screen: str | None = None
    screen_key: str | None = None
    route: str | None = None
    branch_code: str | None = None
    error_code: str | None = None
    category: str | None = None
    completeness: float | None = None
    quality: float | None = None
    missing_fields: list[str] = field(default_factory=list)
    # Required fields the analysis could not determine (UNKNOWN), listed apart from confirmed gaps.
    unknown_required_fields: list[str] = field(default_factory=list)
    similar: list[str] = field(default_factory=list)
    attachment_count: int = 0
    update_text: str | None = None
    update_author: str | None = None
    added_teams: list[str] = field(default_factory=list)
    rerouted: bool = False


def facts_from_db(s: Session, incident_key: str, masker: SensitiveDataMasker) -> IncidentFacts:
    """Facts from the stored incident, its latest analysis, routing and app context.

    Every free-text value is masked here, whatever its source: stored incident text is already masked, but a
    report still waiting in the inbox is raw until the worker merges it.
    """
    facts = IncidentFacts(incident_key=incident_key)
    issue = s.get(Issue, incident_key)
    if issue is not None:
        facts.summary = issue.summary or ""
        facts.description_excerpt = (issue.description or "")[:600]
        facts.branch_code = issue.branch_code
        facts.category = issue.category
        facts.application = issue.application
        if issue.latest_analysis_id is not None:
            analysis = s.get(Analysis, issue.latest_analysis_id)
            if analysis is not None:
                c, q = analysis.completeness_score, analysis.quality_score
                facts.completeness = round(c, 1) if c is not None else None
                facts.quality = round(q, 1) if q is not None else None
                facts.missing_fields = [FIELD_LABELS_TR.get(f, f) for f in analysis.missing_fields or []]
                unknown = set(analysis.unknown_fields or [])
                facts.unknown_required_fields = [
                    FIELD_LABELS_TR.get(f, f) for f in analysis.missing_required_fields or [] if f in unknown
                ]
                facts.similar = [
                    str(x.get("incident_key") or x.get("issue_key"))
                    for x in (analysis.similar_issues or [])[:5]
                    if x.get("relationship") in ("DUPLICATE_CANDIDATE", "RELATED")
                ]
    else:  # still in the inbox (report not yet merged): use the queued event, masked by the caller
        payload = s.execute(
            select(WebhookInbox.payload)
            .where(
                WebhookInbox.source == incident_key.split(":", 1)[0],
                WebhookInbox.issue_key == incident_key.split(":", 1)[1],
            )
            .order_by(WebhookInbox.received_at)
            .limit(1)
        ).scalar_one_or_none()
        event = (payload or {}).get("incident_event") or {}
        facts.summary = str((event.get("fields") or {}).get("summary") or "")
    facts.summary = masker.mask_text(facts.summary) or ""
    facts.description_excerpt = masker.mask_text(facts.description_excerpt) or ""
    ctx_row = s.get(IncidentAppContext, incident_key)
    if ctx_row is not None:
        ctx = ctx_row.context
        facts.error_code = ctx.get("error_code")
        facts.route = ctx.get("route")
        facts.branch_code = facts.branch_code or (ctx.get("user_context") or {}).get("branch_code")
    routing = s.get(IncidentRouting, incident_key)
    if routing is not None:
        facts.application_key = routing.application_key
        facts.screen_key = routing.screen_key
    if issue is not None:
        mapped = (issue.snapshot or {}).get("mapped_fields") or {}
        facts.application = mapped.get("application") or facts.application
        facts.screen = mapped.get("screen")
    facts.attachment_count = int(
        s.execute(
            select(func.count()).select_from(Attachment).where(Attachment.incident_key == incident_key)
        ).scalar_one()
    )
    return facts


def _template_dir(settings: Settings) -> Path:
    return Path(
        settings.notifications.template_directory or Path(settings.templates.directory) / "notifications"
    )


class NotificationComposer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._env = Environment(
            loader=FileSystemLoader(str(_template_dir(settings))),
            autoescape=select_autoescape(enabled_extensions=("html.j2",), default=False),
            undefined=StrictUndefined,
            trim_blocks=False,
            keep_trailing_newline=True,
        )

    def link(self, incident_key: str) -> str | None:
        base = self._settings.dashboard.public_url
        return f"{base.rstrip('/')}/issues/{incident_key}" if base else None

    def render(self, event: str, context: dict[str, Any]) -> tuple[str, str, str]:
        subject = SUBJECTS[event].format(
            **{**context, "summary": _short(str(context.get("summary") or ""), 80)}
        )
        if context.get("off_hours"):
            subject = "[Mesai dışı] " + subject
        ctx = {**context, "subject": subject}
        text = self._env.get_template(f"{event}.txt.j2").render(**ctx)
        html = self._env.get_template(f"{event}.html.j2").render(**ctx)
        return subject, text, html

    @staticmethod
    def zoom_card(event: str, subject: str, ctx: dict[str, Any]) -> dict[str, Any]:
        """Zoom ``format=full`` card: key facts only — no description, no PII, never the screenshot."""
        rows = [
            ("Kayıt", ctx.get("incident_key")),
            ("Özet", _short(str(ctx.get("summary") or ""), 160)),
            ("Ekran", ctx.get("screen") or ctx.get("screen_key")),
            ("Sahip ekip", ctx.get("owner")),
            ("Eksik bilgiler", ", ".join(ctx.get("missing_fields") or []) or None),
            ("Belirlenemeyen", ", ".join(ctx.get("unknown_required_fields") or []) or None),
            ("Ekler", f"{ctx['attachment_count']} dosya" if ctx.get("attachment_count") else None),
            ("Nöbetçi", ctx.get("oncall_contact") if ctx.get("off_hours") else None),
        ]
        body: list[dict[str, Any]] = [
            {"type": "message", "text": f"{label}: {value}"} for label, value in rows if value
        ]
        if ctx.get("link"):
            body.append({"type": "message", "text": "Panelde aç", "link": ctx["link"]})
        return {"head": {"text": subject}, "body": body}


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class Recipient:
    team: Team
    role: str  # "to" | "cc"


class NotificationService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.composer = NotificationComposer(settings)
        self._masker = SensitiveDataMasker(settings.security.masking_extra_patterns)

    def _masked(self, facts: IncidentFacts) -> dict[str, Any]:
        """Every text fact is masked here, whatever the caller did (single choke point before rendering)."""

        def mask(value: Any) -> Any:
            if isinstance(value, str):
                return self._masker.mask_text(value) or ""
            if isinstance(value, list):
                return [mask(v) for v in value]
            return value

        return {k: (v if k == "incident_key" else mask(v)) for k, v in asdict(facts).items()}

    def enqueue(
        self,
        s: Session,
        *,
        event: str,
        facts: IncidentFacts,
        recipients: list[Recipient],
        channels: tuple[str, ...] | list[str],
        registry: OwnershipRegistry,
        owner_team_id: int | None = None,
        suffix: str = "",
        now: datetime | None = None,
        extra: dict[str, Any] | None = None,
    ) -> int:
        """Render and enqueue one row per (recipient team, channel); returns the number of new rows."""
        if not self._settings.notifications.enabled:
            return 0
        now = now or datetime.now(UTC)
        owner = registry.team(owner_team_id)
        to_teams = [r.team for r in recipients if r.role == "to"]
        cc_teams = [r.team for r in recipients if r.role == "cc"]
        to_emails = [t.email for t in to_teams if t.email]
        cc_emails = [t.email for t in cc_teams if t.email]
        created = 0
        for rcpt in recipients:
            team = rcpt.team
            off_hours = not within_working_hours(team.working_hours, now)
            ctx: dict[str, Any] = {
                **self._masked(facts),
                **(extra or {}),
                "event": event,
                "owner": owner.name if owner else None,
                "cc_teams": [t.name for t in cc_teams],
                "matched_level_tr": (extra or {}).get("matched_level_tr", ""),
                "link": self.composer.link(facts.incident_key),
                "off_hours": off_hours,
                "oncall_contact": team.oncall_contact,
                "recipient_role_tr": "doğrudan alıcı" if rcpt.role == "to" else "bilgi (CC)",
                "team_name": team.name,
            }
            subject, text, html = self.composer.render(event, ctx)
            for channel in channels:
                if channel == "email" and team.email:
                    address, secret_ref, card = team.email, None, None
                elif channel == "zoom" and team.zoom_channel_id:
                    address, secret_ref = team.zoom_channel_id, team.zoom_webhook_secret_ref
                    card = self.composer.zoom_card(event, subject, ctx)
                else:
                    continue
                message = NotificationMessage(
                    event=event,
                    channel=channel,
                    recipient=address,
                    subject=subject,
                    text=text,
                    html=html if channel == "email" else None,
                    card=card,
                    to=to_emails,
                    cc=cc_emails,
                    incident_key=facts.incident_key,
                    secret_ref=secret_ref,
                )
                new_id = NOTIFICATION_OUTBOX.enqueue(
                    s,
                    idempotency_key=f"{facts.incident_key}:{event}{suffix}:{channel}:{address}",
                    kind=event,
                    incident_key=facts.incident_key,
                    payload={**asdict(message), "off_hours": off_hours},
                    max_attempts=self._settings.outbox.max_attempts,
                    event=event,
                    channel=channel,
                    recipient=address,
                    team_id=team.id,
                    role=rcpt.role,
                )
                created += int(new_id is not None)
        return created


def recipients_for(registry: OwnershipRegistry, to_ids: list[int], cc_ids: list[int]) -> list[Recipient]:
    seen: set[int] = set()
    out: list[Recipient] = []
    for role, ids in (("to", to_ids), ("cc", cc_ids)):
        for team_id in ids:
            team = registry.active_team(team_id)
            if team is not None and team.id not in seen:
                seen.add(team.id)
                out.append(Recipient(team, role))
    return out


def resend(s: Session, notification_id: str, actor: str) -> bool:
    """Requeue a delivered or dead-lettered notification (ADMIN action, audited)."""
    import uuid

    from branchops.infrastructure.persistence.audit import audit

    try:
        item_id = uuid.UUID(notification_id)
    except ValueError:
        return False
    ok = NOTIFICATION_OUTBOX.replay(s, item_id)
    if ok:
        audit(
            s,
            actor=actor,
            action="notification_resent",
            resource_type="notification",
            resource_id=notification_id,
        )
    return ok


def send_test(
    s: Session, service: NotificationService, registry: OwnershipRegistry, team_id: int, actor: str
) -> int:
    """Enqueue a test message to every configured channel of a team (dashboard "Test bildirimi")."""
    team = registry.team(team_id)
    if team is None:
        return 0
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    total = 0
    for channel, label in (("email", "e-posta"), ("zoom", "Zoom")):
        total += service.enqueue(
            s,
            event="test",
            facts=IncidentFacts(incident_key=f"test:{team.key}"),
            recipients=[Recipient(team, "to")],
            channels=(channel,),
            registry=registry,
            suffix=f":{stamp}",
            extra={"requested_by": actor, "channel_tr": label},
        )
    return total

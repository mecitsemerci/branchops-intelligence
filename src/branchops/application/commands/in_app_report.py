"""In-app reporting flow used by the MCP tools: draft check (+ draft token), report with app context and
attachments, upload. Combines the report command, the ownership registry and the attachment service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from branchops.application.commands.attachments import AttachmentKind, AttachmentService, UploadResult
from branchops.application.commands.notifications import IncidentFacts
from branchops.application.commands.ownership import OwnershipService
from branchops.application.commands.report_incident import (
    AddIncidentUpdate,
    ReportIncident,
    ReportResult,
    check_draft,
)
from branchops.application.commands.routing import IncidentRouter
from branchops.config.settings import Settings
from branchops.domain.entities.ownership import OwnershipRegistry
from branchops.domain.entities.report import AppContext, DraftCheck, IncidentDraft
from branchops.domain.services.app_context import routing_input, sanitize_app_context, strip_query
from branchops.domain.services.masking import SensitiveDataMasker
from branchops.infrastructure.attachments import build_attachment_store
from branchops.infrastructure.database.models import Attachment, IncidentAppContext
from branchops.infrastructure.database.session import Database
from branchops.templates.registry import TemplateRegistry


@dataclass(frozen=True)
class KnownFacts:
    """Facts the server adds from the ownership registry (pre-filled fields for analysis and the check)."""

    facts: dict[str, str]
    group_key: str | None


def known_facts(app_context: AppContext | None, registry: OwnershipRegistry) -> KnownFacts:
    if app_context is None:
        return KnownFacts({}, None)
    facts: dict[str, str] = {}
    application = registry.application(app_context.app_id)
    if application is not None:
        facts["application"] = application.name
        screen = next(
            (s for s in registry.screens_of(application.id) if s.key == app_context.screen_id), None
        )
        if screen is not None:
            facts["screen"] = screen.title
    if "screen" not in facts and app_context.screen_title:
        facts["screen"] = app_context.screen_title
    group = application.key.upper() if application is not None else None
    return KnownFacts(facts, group)


class InAppReporting:
    def __init__(self, db: Database, settings: Settings, templates: TemplateRegistry) -> None:
        self._db = db
        self._settings = settings
        self._templates = templates
        self.ownership = OwnershipService(db, settings)
        self.router = IncidentRouter(db, settings)
        self.attachments = AttachmentService(settings, build_attachment_store(settings))
        self._masker = SensitiveDataMasker(settings.security.masking_extra_patterns)
        self._pseudonym_key = settings.security.pseudonym_key()

    def check(self, draft: IncidentDraft, *, actor: str, issue_token: bool = True) -> DraftCheck:
        facts = known_facts(draft.app_context, self.ownership.registry())
        result = check_draft(draft, self._templates, facts.facts)
        if draft.app_context is not None:
            flags = sanitize_app_context(draft.app_context, self._masker, self._pseudonym_key).security_flags
            result = result.model_copy(
                update={"security_flags": sorted(set(result.security_flags) | set(flags))}
            )
        if not issue_token:
            return result
        with self._db.session() as s:
            token, expires = self.attachments.issue_draft_token(s, actor)
        return result.model_copy(update={"draft_token": token, "draft_token_expires_at": expires})

    def report(
        self,
        draft: IncidentDraft,
        *,
        actor: str,
        correlation_id: str,
        idempotency_key: str | None = None,
        draft_token: str | None = None,
    ) -> ReportResult:
        registry = self.ownership.registry()
        facts = known_facts(draft.app_context, registry)
        decision = self.router.decide(
            draft.app_context,
            branch_code=draft.branch_code,
            category=draft.category,
            application=draft.application,
            registry=registry,
        )
        owner = registry.team(decision.owner_team_id)
        sanitized = (
            sanitize_app_context(draft.app_context, self._masker, self._pseudonym_key)
            if draft.app_context
            else None
        )

        def after_insert(s: Session, incident_key: str, created: bool) -> dict[str, Any]:
            out: dict[str, Any] = {}
            if created and sanitized is not None:
                s.add(
                    IncidentAppContext(
                        incident_key=incident_key,
                        context=sanitized.context,
                        security_flags=sanitized.security_flags,
                    )
                )
                out["app_context_flags"] = sanitized.security_flags
            if created and draft_token:
                out["attachments_bound"] = self.attachments.bind_draft(s, draft_token, actor, incident_key)
            if created:
                out["routing"] = self.router.on_reported(
                    s, incident_key, decision, registry, self._facts(s, incident_key, draft, facts.facts)
                )
            return out

        command = ReportIncident(self._db, self._settings, self._templates)
        return command.execute(
            draft,
            reporter=actor,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            group_key=facts.group_key,
            known=facts.facts,
            project_key=owner.jira_project_key if owner else None,
            after_insert=after_insert,
        )

    def _facts(
        self, s: Session, incident_key: str, draft: IncidentDraft, known: dict[str, str]
    ) -> IncidentFacts:
        ctx = draft.app_context
        return IncidentFacts(
            incident_key=incident_key,
            summary=self._masker.mask_text(draft.title) or "",
            description_excerpt=(self._masker.mask_text(draft.description) or "")[:600],
            application=known.get("application") or draft.application,
            screen=known.get("screen"),
            route=strip_query(ctx.route) if ctx and ctx.route else None,
            branch_code=draft.branch_code
            or (ctx.user_context.branch_code if ctx and ctx.user_context else None),
            error_code=ctx.error_code if ctx else None,
            attachment_count=int(
                s.execute(
                    select(func.count())
                    .select_from(Attachment)
                    .where(Attachment.incident_key == incident_key)
                ).scalar_one()
            ),
        )

    def update(
        self, incident_key: str, text: str, *, actor: str, correlation_id: str
    ) -> tuple[str, str | None]:
        def notify(s: Session, key: str, comment_id: str) -> None:
            self.router.on_update(s, key, comment_id, text, actor)

        return AddIncidentUpdate(self._db, self._settings).execute(
            incident_key, text, author=actor, correlation_id=correlation_id, after_insert=notify
        )

    def upload(
        self,
        *,
        actor: str,
        filename: str,
        content_type: str,
        data_base64: str,
        kind: AttachmentKind,
        incident_key: str | None,
        draft_token: str | None,
        blur_regions: list[list[int]] | None,
    ) -> UploadResult:
        with self._db.session() as s:
            return self.attachments.upload(
                s,
                actor=actor,
                filename=filename,
                content_type=content_type,
                data_base64=data_base64,
                kind=kind,
                incident_key=incident_key,
                draft_token=draft_token,
                blur_regions=blur_regions,
            )

    def routing_preview(self, app_context: AppContext | None, branch_code: str | None) -> dict[str, Any]:
        registry = self.ownership.registry()
        return self.ownership.describe(
            self.ownership.resolve(routing_input(app_context, branch_code), None, registry), registry
        )

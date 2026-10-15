"""Ownership registry use cases shared by the dashboard (/yonetim), the REST API and the MCP server."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from branchops.config.settings import Settings
from branchops.domain.entities.ownership import (
    ApplicationInput,
    OwnershipRegistry,
    RoutingClassification,
    RoutingDecision,
    RoutingInput,
    RoutingRule,
    RuleInput,
    ScreenInput,
    TeamInput,
    resolve_owner,
    rule_matches,
)
from branchops.infrastructure.database.models import Issue
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.persistence.ownership_repository import ImportReport, OwnershipRepository

T = TypeVar("T")
PREVIEW_ROW_LIMIT = 20_000


def error_code_of(error_signature: str | None) -> str | None:
    """``"eft uygulamasi|EFT-504"`` -> ``"EFT-504"`` (signatures are ``<application>|<first error code>``)."""
    if not error_signature or "|" not in error_signature:
        return None
    return error_signature.rsplit("|", 1)[1] or None


@dataclass(frozen=True)
class RulePreview:
    days: int
    matched: int
    scanned: int
    sample_keys: list[str]


class OwnershipService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self.repo = OwnershipRepository()

    def _tx(self, fn: Callable[[Session], T]) -> T:
        with self._db.session() as s:
            return fn(s)

    # ------------------------------------------------------------------ resolution
    def registry(self) -> OwnershipRegistry:
        return self._tx(self.repo.load_registry)

    def resolve(
        self,
        app_context: RoutingInput | None,
        classification: RoutingClassification | None = None,
        registry: OwnershipRegistry | None = None,
    ) -> RoutingDecision:
        return resolve_owner(
            app_context,
            classification,
            registry or self.registry(),
            branch_ops_team_key=self._settings.ownership.branch_ops_team_key,
        )

    def describe(
        self, decision: RoutingDecision, registry: OwnershipRegistry | None = None
    ) -> dict[str, Any]:
        """Decision with team names and contacts (no secrets), for UIs and MCP clients."""
        reg = registry or self.registry()

        def team(team_id: int | None) -> dict[str, Any] | None:
            t = reg.team(team_id)
            if t is None:
                return None
            return {
                "key": t.key,
                "name": t.name,
                "kind": t.kind.value,
                "email": t.email,
                "oncall_contact": t.oncall_contact,
                "working_hours": t.working_hours,
                "jira_project_key": t.jira_project_key,
            }

        app = next((a for a in reg.applications if a.id == decision.application_id), None)
        screen = next((s for s in reg.screens if s.id == decision.screen_id), None)
        return {
            **decision.to_json(),
            "owner": team(decision.owner_team_id),
            "to": [team(t) for t in decision.to_team_ids],
            "cc": [team(t) for t in decision.cc_team_ids],
            "application_name": app.name if app else None,
            "screen_title": screen.title if screen else None,
        }

    # ------------------------------------------------------------------ registry writes
    def save_team(self, data: TeamInput, actor: str, team_id: int | None = None) -> dict[str, Any]:
        return self._tx(lambda s: self.repo.save_team(s, data, actor, team_id))

    def save_application(
        self, data: ApplicationInput, actor: str, application_id: int | None = None
    ) -> dict[str, Any]:
        return self._tx(lambda s: self.repo.save_application(s, data, actor, application_id))

    def save_screen(self, data: ScreenInput, actor: str, screen_id: int | None = None) -> dict[str, Any]:
        return self._tx(lambda s: self.repo.save_screen(s, data, actor, screen_id))

    def save_rule(self, data: RuleInput, actor: str, rule_id: int | None = None) -> dict[str, Any]:
        return self._tx(lambda s: self.repo.save_rule(s, data, actor, rule_id))

    def set_active(self, entity: str, entity_id: int, active: bool, actor: str) -> dict[str, Any]:
        return self._tx(lambda s: self.repo.set_active(s, entity, entity_id, active, actor))

    def delete(self, entity: str, entity_id: int, actor: str) -> None:
        self._tx(lambda s: self.repo.delete(s, entity, entity_id, actor))

    def reorder_rules(self, ordered_ids: list[int], actor: str) -> None:
        self._tx(lambda s: self.repo.reorder_rules(s, ordered_ids, actor))

    def bulk_set_screen_owner(self, screen_ids: list[int], team_id: int, actor: str) -> int:
        return self._tx(lambda s: self.repo.bulk_set_screen_owner(s, screen_ids, team_id, actor))

    def import_csv(self, content: str, actor: str, *, dry_run: bool = False) -> ImportReport:
        return self._tx(lambda s: self.repo.import_csv(s, content, actor, dry_run=dry_run))

    def export_csv(self) -> str:
        return self._tx(self.repo.export_csv)

    # ------------------------------------------------------------------ reads
    def teams(self) -> list[dict[str, Any]]:
        return self._tx(self.repo.list_teams)

    def tree(self) -> list[dict[str, Any]]:
        return self._tx(self.repo.tree)

    def rules(self) -> list[dict[str, Any]]:
        return self._tx(self.repo.list_rules)

    def changes(self, *, limit: int = 200, entity: str | None = None) -> list[dict[str, Any]]:
        return self._tx(lambda s: self.repo.changes(s, limit=limit, entity=entity))

    def who_owns(self, query: str) -> list[dict[str, Any]]:
        return self._tx(lambda s: self.repo.who_owns(s, query))

    def check(self, *, preview_days: int = 90) -> list[Finding]:
        """Registry validation plus active rules that matched no incident in ``preview_days``."""
        registry = self.registry()
        findings = registry_findings(registry, self._settings.ownership.branch_ops_team_key)
        if self._settings.notifications.enabled:
            production = self._settings.app.environment == "production"
            findings += [
                f if production else Finding("warning", f.code, f.message)
                for f in notification_findings(registry, self._settings)
                if f.code != "branch_ops_missing"
            ]
        for rule in registry.rules:
            if not rule.active:
                continue
            data = RuleInput(
                name=rule.name,
                priority=rule.priority,
                match=rule.match,
                notify_to_team_ids=list(rule.notify_to_team_ids),
                cc_team_ids=list(rule.cc_team_ids),
                channels=list(rule.channels),
            )
            if self.preview_rule(data, days=preview_days).matched == 0:
                findings.append(
                    Finding(
                        "warning",
                        "rule_matches_nothing",
                        f"rule {rule.name!r}: no match in {preview_days} days",
                    )
                )
        return findings

    def preview_rule(self, data: RuleInput, *, days: int = 30, now: datetime | None = None) -> RulePreview:
        """How many incidents of the last ``days`` the rule would have matched (category, application,
        error code and branch code of the analysed incidents)."""
        since = (now or datetime.now(UTC)) - timedelta(days=days)
        rule = RoutingRule(id=0, name=data.name, priority=data.priority, match=data.match)
        reg = self.registry()

        def scan(s: Session) -> RulePreview:
            rows = s.execute(
                select(
                    Issue.incident_key,
                    Issue.category,
                    Issue.application,
                    Issue.branch_code,
                    Issue.error_signature,
                )
                .where(Issue.is_deleted.is_(False), Issue.created_at >= since)
                .order_by(Issue.created_at.desc())
                .limit(PREVIEW_ROW_LIMIT)
            ).all()
            keys = [
                r.incident_key
                for r in rows
                if rule_matches(
                    rule,
                    category=r.category,
                    application=reg.application(r.application),
                    application_text=r.application,
                    screen_key=None,
                    error_code=error_code_of(r.error_signature),
                    branch_code=r.branch_code,
                )
            ]
            return RulePreview(days=days, matched=len(keys), scanned=len(rows), sample_keys=keys[:10])

        return self._tx(scan)


@dataclass(frozen=True)
class Finding:
    level: str  # "error" | "warning"
    code: str
    message: str


def registry_findings(registry: OwnershipRegistry, branch_ops_team_key: str) -> list[Finding]:
    """Static validation of the registry (orphans, inactive owners, unreachable teams)."""
    out: list[Finding] = []
    ops = registry.team_by_key(branch_ops_team_key)
    if ops is None or not ops.active:
        out.append(
            Finding(
                "error",
                "branch_ops_missing",
                f"branch operations team {branch_ops_team_key!r} "
                "must exist and be active (fallback owner and CC of every notification)",
            )
        )
    for t in registry.teams:
        if t.active and not (t.email or t.zoom_channel_id):
            out.append(Finding("warning", "team_unreachable", f"team {t.key}: no e-mail and no Zoom channel"))
    apps = {a.id: a for a in registry.applications}
    for a in registry.applications:
        if not a.active:
            continue
        owner = registry.team(a.owner_team_id)
        if owner is None or not owner.active:
            out.append(Finding("error", "inactive_owner", f"application {a.key}: owner team is inactive"))
        if (
            a.default_screen_owner_team_id is not None
            and registry.active_team(a.default_screen_owner_team_id) is None
        ):
            out.append(
                Finding(
                    "warning", "inactive_owner", f"application {a.key}: default screen owner team is inactive"
                )
            )
        if not registry.screens_of(a.id):
            out.append(Finding("warning", "no_screens", f"application {a.key}: no active screens registered"))
    for sc in registry.screens:
        app = apps.get(sc.application_id)
        if not sc.active or app is None:
            continue
        if not app.active:
            out.append(
                Finding("warning", "orphan_screen", f"screen {app.key}/{sc.key}: application is inactive")
            )
        elif registry.active_team(sc.owner_team_id) is None:
            out.append(
                Finding("error", "inactive_owner", f"screen {app.key}/{sc.key}: owner team is inactive")
            )
    for r in registry.rules:
        if not r.active:
            continue
        dead = [t for t in (*r.notify_to_team_ids, *r.cc_team_ids) if registry.active_team(t) is None]
        if dead:
            out.append(
                Finding("warning", "rule_inactive_team", f"rule {r.name!r}: inactive/unknown teams {dead}")
            )
        if r.match.application_key and registry.application(r.match.application_key) is None:
            out.append(
                Finding("warning", "rule_unknown_application", f"rule {r.name!r}: unknown application key")
            )
    return out


def notification_findings(registry: OwnershipRegistry, settings: Settings) -> list[Finding]:
    """The branch operations team needs a channel that can actually deliver (production guard)."""
    ops = registry.team_by_key(settings.ownership.branch_ops_team_key)
    if ops is None or not ops.active:
        return [Finding("error", "branch_ops_missing", "branch operations team is missing or inactive")]
    cfg = settings.notifications
    email_ok = bool(ops.email) and cfg.email.backend == "smtp"
    zoom_ok = (
        bool(ops.zoom_channel_id and ops.zoom_webhook_secret_ref)
        and cfg.zoom.backend == "webhook"
        and bool(os.environ.get(ops.zoom_webhook_secret_ref or ""))
    )
    if not (email_ok or zoom_ok):
        return [
            Finding(
                "error",
                "branch_ops_unreachable",
                f"branch operations team {ops.key!r} has no working channel "
                "(e-mail with the smtp backend, or Zoom with the webhook backend and its token set)",
            )
        ]
    return []

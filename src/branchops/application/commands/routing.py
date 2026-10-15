"""Routing of reported incidents into the notification flow.

* On ``report_incident``: resolve the owner from ``app_context`` (no analysis needed), persist the decision in
  ``incident_routing``, enqueue ``incident_reported`` (owner To, rule teams To/CC, branch-ops CC) and, for an
  unowned screen, ``ownership_unresolved`` to branch-ops and the platform team.
* After each analysis: resolve again with the classification (category, application); teams added by routing
  rules are merged in (nobody is ever removed, branch-ops stays CC) and ``analysis_ready`` goes to everyone.
* ``add_incident_update``: ``update_added`` to the routed teams.
* Re-route (analyst action): resolve with the current registry, replace the decision, notify the new teams.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from branchops.application.commands.notifications import (
    LEVEL_TR,
    IncidentFacts,
    NotificationService,
    facts_from_db,
    recipients_for,
)
from branchops.application.commands.ownership import OwnershipService
from branchops.config.settings import Settings
from branchops.domain.entities.ownership import (
    MatchedLevel,
    OwnershipRegistry,
    RoutingClassification,
    RoutingDecision,
    RoutingInput,
)
from branchops.domain.entities.report import AppContext
from branchops.domain.services.app_context import routing_input
from branchops.domain.services.masking import SensitiveDataMasker
from branchops.infrastructure.database.models import IncidentAppContext, IncidentRouting, Issue
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.persistence.audit import audit

logger = logging.getLogger(__name__)


def routing_dict(row: IncidentRouting) -> dict[str, Any]:
    return {
        "incident_key": row.incident_key,
        "owner_team_id": row.owner_team_id,
        "to_team_ids": list(row.to_team_ids or []),
        "cc_team_ids": list(row.cc_team_ids or []),
        "channels": list(row.channels or []),
        "matched_level": row.matched_level,
        "unowned_screen": row.unowned_screen,
        "screen_unregistered": row.screen_unregistered,
        "application_key": row.application_key,
        "screen_key": row.screen_key,
        "reasons": list(row.reasons or []),
        "decided_by": row.decided_by,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "reported_at": row.reported_at.isoformat() if row.reported_at else None,
        "first_notified_at": row.first_notified_at.isoformat() if row.first_notified_at else None,
    }


class IncidentRouter:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self.ownership = OwnershipService(db, settings)
        self.notifications = NotificationService(settings)
        self._masker = SensitiveDataMasker(settings.security.masking_extra_patterns)

    # ------------------------------------------------------------------ decisions
    def decide(
        self,
        app_context: AppContext | RoutingInput | None,
        *,
        branch_code: str | None = None,
        category: str | None = None,
        application: str | None = None,
        registry: OwnershipRegistry | None = None,
    ) -> RoutingDecision:
        ctx = (
            app_context if isinstance(app_context, RoutingInput) else routing_input(app_context, branch_code)
        )
        return self.ownership.resolve(ctx, RoutingClassification(category, application), registry)

    @staticmethod
    def _persist(
        s: Session, incident_key: str, d: RoutingDecision, decided_by: str, *, route: str | None = None
    ) -> IncidentRouting:
        row = s.get(IncidentRouting, incident_key)
        if row is None:
            row = IncidentRouting(incident_key=incident_key)
            s.add(row)
        row.owner_team_id = d.owner_team_id
        row.to_team_ids = list(d.to_team_ids)
        row.cc_team_ids = list(d.cc_team_ids)
        row.channels = list(d.channels)
        row.matched_level = d.matched_level.value
        row.unowned_screen = d.unowned_screen
        row.screen_unregistered = d.screen_unregistered
        row.application_key = d.application_key or row.application_key
        row.screen_key = d.screen_key or row.screen_key
        row.matched_rule_ids = list(d.matched_rule_ids)
        row.reasons = list(d.reasons) + ([f"route {route}"] if route else [])
        row.decided_by = decided_by[:200]
        row.decided_at = datetime.now(UTC)
        s.flush()
        return row

    def _team_names(self, registry: OwnershipRegistry, ids: list[int]) -> list[str]:
        return [t.name for t in (registry.team(i) for i in ids) if t]

    # ------------------------------------------------------------------ events
    def on_reported(
        self,
        s: Session,
        incident_key: str,
        decision: RoutingDecision,
        registry: OwnershipRegistry,
        facts: IncidentFacts,
        *,
        decided_by: str = "report",
        suffix: str = "",
    ) -> dict[str, Any]:
        """Persist the decision and enqueue ``incident_reported`` (+ ``ownership_unresolved``)."""
        self._persist(s, incident_key, decision, decided_by)
        facts.application_key = facts.application_key or decision.application_key
        facts.screen_key = facts.screen_key or decision.screen_key
        level = {"matched_level_tr": LEVEL_TR[decision.matched_level]}
        sent = self.notifications.enqueue(
            s,
            event="incident_reported",
            facts=facts,
            recipients=recipients_for(registry, list(decision.to_team_ids), list(decision.cc_team_ids)),
            channels=decision.channels,
            registry=registry,
            owner_team_id=decision.owner_team_id,
            suffix=suffix,
            extra=level,
        )
        if decision.unowned_screen:
            ops = registry.team_by_key(self._settings.ownership.branch_ops_team_key)
            platform = (
                registry.team_by_key(self._settings.ownership.platform_team_key)
                if self._settings.ownership.platform_team_key
                else None
            )
            sent += self.notifications.enqueue(
                s,
                event="ownership_unresolved",
                facts=facts,
                recipients=recipients_for(registry, [t.id for t in (ops, platform) if t], []),
                channels=("email", "zoom"),
                registry=registry,
                owner_team_id=decision.owner_team_id,
                suffix=suffix,
                extra=level,
            )
        owner = registry.team(decision.owner_team_id)
        return {
            "owner_team": owner.key if owner else None,
            "owner_team_name": owner.name if owner else None,
            "cc_teams": [t.key for t in (registry.team(i) for i in decision.cc_team_ids) if t],
            "matched_level": decision.matched_level.value,
            "unowned_screen": decision.unowned_screen,
            "notifications_queued": sent,
        }

    def on_update(self, s: Session, incident_key: str, comment_id: str, text: str, author: str) -> int:
        row = s.get(IncidentRouting, incident_key)
        if row is None:
            return 0
        registry = self.ownership.repo.load_registry(s)
        facts = facts_from_db(s, incident_key, self._masker)
        facts.update_text = (self._masker.mask_text(text) or "")[:1000]
        facts.update_author = author
        return self.notifications.enqueue(
            s,
            event="update_added",
            facts=facts,
            recipients=recipients_for(registry, list(row.to_team_ids), list(row.cc_team_ids)),
            channels=row.channels or ("email", "zoom"),
            registry=registry,
            owner_team_id=row.owner_team_id,
            suffix=f":{comment_id}",
        )

    def on_analysis_complete(self, incident_key: str, analysis_id: str | None) -> int:
        """Merge rule teams added by the classification and send ``analysis_ready`` (worker hook)."""
        with self._db.session() as s:
            row = s.get(IncidentRouting, incident_key)
            if row is None:
                return 0  # only routed (reported) incidents are notified
            issue = s.get(Issue, incident_key)
            registry = self.ownership.repo.load_registry(s)
            ctx_row = s.get(IncidentAppContext, incident_key)
            ctx = ctx_row.context if ctx_row else {}
            decision = self.decide(
                RoutingInput(
                    app_key=row.application_key,
                    screen_key=row.screen_key,
                    route=ctx.get("route"),
                    error_code=ctx.get("error_code"),
                    branch_code=(issue.branch_code if issue else None)
                    or (ctx.get("user_context") or {}).get("branch_code"),
                ),
                category=issue.category if issue else None,
                application=issue.application if issue else None,
                registry=registry,
            )
            old_to, old_cc = list(row.to_team_ids or []), list(row.cc_team_ids or [])
            to_ids = list(dict.fromkeys([*old_to, *decision.to_team_ids]))
            cc_ids = [t for t in dict.fromkeys([*old_cc, *decision.cc_team_ids]) if t not in to_ids]
            added = [t for t in [*to_ids, *cc_ids] if t not in old_to and t not in old_cc]
            if added:
                row.to_team_ids, row.cc_team_ids = to_ids, cc_ids
                row.channels = list(dict.fromkeys([*(row.channels or []), *decision.channels]))
                row.reasons = [*(row.reasons or []), f"analysis added teams {added}"]
                row.decided_by = "analysis"
                row.decided_at = datetime.now(UTC)
            facts = facts_from_db(s, incident_key, self._masker)
            facts.added_teams = self._team_names(registry, added)
            return self.notifications.enqueue(
                s,
                event="analysis_ready",
                facts=facts,
                recipients=recipients_for(registry, to_ids, cc_ids),
                channels=row.channels or ("email", "zoom"),
                registry=registry,
                owner_team_id=row.owner_team_id,
                suffix=f":{(analysis_id or 'latest')[:36]}",
            )

    def reroute(self, incident_key: str, actor: str) -> dict[str, Any]:
        """Resolve again with the current registry (e.g. after an owner was assigned) and notify."""
        with self._db.session() as s:
            row = s.get(IncidentRouting, incident_key)
            ctx_row = s.get(IncidentAppContext, incident_key)
            issue = s.get(Issue, incident_key)
            ctx = ctx_row.context if ctx_row else {}
            registry = self.ownership.repo.load_registry(s)
            decision = self.decide(
                RoutingInput(
                    app_key=ctx.get("app_id") or (row.application_key if row else None),
                    screen_key=ctx.get("screen_id") or (row.screen_key if row else None),
                    route=ctx.get("route"),
                    error_code=ctx.get("error_code"),
                    branch_code=(issue.branch_code if issue else None)
                    or (ctx.get("user_context") or {}).get("branch_code"),
                ),
                category=issue.category if issue else None,
                application=issue.application if issue else None,
                registry=registry,
            )
            previous = routing_dict(row) if row else None
            facts = facts_from_db(s, incident_key, self._masker)
            facts.rerouted = True
            stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
            result = self.on_reported(
                s,
                incident_key,
                decision,
                registry,
                facts,
                decided_by=f"reroute:{actor}",
                suffix=f":reroute:{stamp}",
            )
            audit(
                s,
                actor=actor,
                action="incident_rerouted",
                resource_type="issue",
                resource_id=incident_key,
                details={
                    "before": {k: previous[k] for k in ("owner_team_id", "matched_level")}
                    if previous
                    else None,
                    "after": {
                        "owner_team_id": decision.owner_team_id,
                        "matched_level": decision.matched_level.value,
                    },
                },
            )
            return result

    def routing_of(self, incident_key: str) -> dict[str, Any] | None:
        with self._db.session() as s:
            row = s.get(IncidentRouting, incident_key)
            if row is None:
                return None
            registry = self.ownership.repo.load_registry(s)
            out = routing_dict(row)
            owner = registry.team(row.owner_team_id)
            out["owner_team"] = owner.name if owner else None
            out["to_teams"] = self._team_names(registry, out["to_team_ids"])
            out["cc_teams"] = self._team_names(registry, out["cc_team_ids"])
            out["matched_level_tr"] = LEVEL_TR.get(MatchedLevel(row.matched_level), row.matched_level)
            return out

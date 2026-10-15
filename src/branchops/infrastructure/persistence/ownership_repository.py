"""Ownership registry persistence. Every write goes through :meth:`OwnershipRepository._log`, which appends
an ``ownership_changes`` row (who, when, before/after) in the same transaction."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from branchops.domain.entities.ownership import (
    Application,
    ApplicationInput,
    OwnershipRegistry,
    RoutingRule,
    RuleInput,
    RuleMatch,
    Screen,
    ScreenInput,
    Team,
    TeamInput,
    TeamKind,
)
from branchops.infrastructure.database.models import (
    OwnedApplication,
    OwnedScreen,
    OwnershipChange,
    OwnershipTeam,
    RoutingRuleRow,
)
from branchops.infrastructure.observability.logging import correlation_id_var

CSV_COLUMNS = ("application_key", "screen_key", "title", "route_pattern", "owner_team_key")


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: str) -> str:
    """Neutralise spreadsheet formulas (CSV injection): ``=HYPERLINK(...)`` -> ``'=HYPERLINK(...)``."""
    return "'" + value if value.startswith(_FORMULA_PREFIXES) else value


def csv_unsafe(value: str) -> str:
    """Inverse of :func:`csv_safe` for re-imports of exported files."""
    return value[1:] if value.startswith("'") and value[1:].startswith(_FORMULA_PREFIXES) else value


class OwnershipError(ValueError):
    """Invalid or conflicting registry change (duplicate key, unknown team, entity still in use …)."""


@dataclass
class ImportReport:
    created_applications: int = 0
    created_screens: int = 0
    updated_screens: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _team_dict(t: OwnershipTeam) -> dict[str, Any]:
    return {
        "id": t.id,
        "key": t.key,
        "name": t.name,
        "kind": t.kind,
        "email": t.email,
        "zoom_channel_id": t.zoom_channel_id,
        "zoom_webhook_secret_ref": t.zoom_webhook_secret_ref,
        "jira_project_key": t.jira_project_key,
        "oncall_contact": t.oncall_contact,
        "working_hours": t.working_hours,
        "active": t.active,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "updated_by": t.updated_by,
    }


def _app_dict(a: OwnedApplication) -> dict[str, Any]:
    return {
        "id": a.id,
        "key": a.key,
        "name": a.name,
        "owner_team_id": a.owner_team_id,
        "default_screen_owner_team_id": a.default_screen_owner_team_id,
        "active": a.active,
    }


def _screen_dict(sc: OwnedScreen) -> dict[str, Any]:
    return {
        "id": sc.id,
        "application_id": sc.application_id,
        "key": sc.key,
        "title": sc.title,
        "route_pattern": sc.route_pattern,
        "owner_team_id": sc.owner_team_id,
        "tags": list(sc.tags or []),
        "active": sc.active,
    }


def _rule_dict(r: RoutingRuleRow) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "priority": r.priority,
        "match": dict(r.match),
        "notify_to_team_ids": list(r.notify_to_team_ids or []),
        "cc_team_ids": list(r.cc_team_ids or []),
        "channels": list(r.channels or []),
        "active": r.active,
    }


class OwnershipRepository:
    # ------------------------------------------------------------------ reads
    def load_registry(self, s: Session) -> OwnershipRegistry:
        teams = s.execute(select(OwnershipTeam)).scalars().all()
        apps = s.execute(select(OwnedApplication)).scalars().all()
        screens = s.execute(select(OwnedScreen)).scalars().all()
        rules = s.execute(select(RoutingRuleRow)).scalars().all()
        return OwnershipRegistry(
            teams=tuple(
                Team(
                    id=t.id,
                    key=t.key,
                    name=t.name,
                    kind=TeamKind(t.kind),
                    email=t.email,
                    zoom_channel_id=t.zoom_channel_id,
                    zoom_webhook_secret_ref=t.zoom_webhook_secret_ref,
                    jira_project_key=t.jira_project_key,
                    oncall_contact=t.oncall_contact,
                    working_hours=t.working_hours,
                    active=t.active,
                )
                for t in teams
            ),
            applications=tuple(
                Application(a.id, a.key, a.name, a.owner_team_id, a.default_screen_owner_team_id, a.active)
                for a in apps
            ),
            screens=tuple(
                Screen(
                    id=sc.id,
                    application_id=sc.application_id,
                    key=sc.key,
                    title=sc.title,
                    owner_team_id=sc.owner_team_id,
                    route_pattern=sc.route_pattern,
                    tags=tuple(sc.tags or ()),
                    active=sc.active,
                )
                for sc in screens
            ),
            rules=tuple(
                RoutingRule(
                    id=r.id,
                    name=r.name,
                    priority=r.priority,
                    match=RuleMatch.model_validate(r.match),
                    notify_to_team_ids=tuple(int(x) for x in r.notify_to_team_ids or ()),
                    cc_team_ids=tuple(int(x) for x in r.cc_team_ids or ()),
                    channels=tuple(r.channels or ()),
                    active=r.active,
                )
                for r in rules
            ),
        )

    def list_teams(self, s: Session) -> list[dict[str, Any]]:
        return [_team_dict(t) for t in s.execute(select(OwnershipTeam).order_by(OwnershipTeam.key)).scalars()]

    def team_by_key(self, s: Session, key: str) -> dict[str, Any] | None:
        t = s.execute(select(OwnershipTeam).where(OwnershipTeam.key == key)).scalar_one_or_none()
        return _team_dict(t) if t else None

    def list_applications(self, s: Session) -> list[dict[str, Any]]:
        rows = s.execute(select(OwnedApplication).order_by(OwnedApplication.key)).scalars()
        return [_app_dict(a) for a in rows]

    def list_screens(self, s: Session, application_id: int | None = None) -> list[dict[str, Any]]:
        q = select(OwnedScreen).order_by(OwnedScreen.application_id, OwnedScreen.key)
        if application_id is not None:
            q = q.where(OwnedScreen.application_id == application_id)
        return [_screen_dict(sc) for sc in s.execute(q).scalars()]

    def list_rules(self, s: Session) -> list[dict[str, Any]]:
        rows = s.execute(
            select(RoutingRuleRow).order_by(RoutingRuleRow.priority, RoutingRuleRow.id)
        ).scalars()
        return [_rule_dict(r) for r in rows]

    def tree(self, s: Session) -> list[dict[str, Any]]:
        """Applications with their screens and owner team keys (for the dashboard tree and exports)."""
        teams = {t["id"]: t["key"] for t in self.list_teams(s)}
        screens: dict[int, list[dict[str, Any]]] = {}
        for sc in self.list_screens(s):
            screens.setdefault(sc["application_id"], []).append(
                {**sc, "owner_team_key": teams.get(sc["owner_team_id"])}
            )
        return [
            {
                **a,
                "owner_team_key": teams.get(a["owner_team_id"]),
                "default_screen_owner_team_key": teams.get(a["default_screen_owner_team_id"]),
                "screens": screens.get(a["id"], []),
            }
            for a in self.list_applications(s)
        ]

    def changes(self, s: Session, *, limit: int = 200, entity: str | None = None) -> list[dict[str, Any]]:
        q = select(OwnershipChange).order_by(OwnershipChange.occurred_at.desc(), OwnershipChange.id.desc())
        if entity:
            q = q.where(OwnershipChange.entity == entity)
        return [
            {
                "id": c.id,
                "occurred_at": c.occurred_at.isoformat(),
                "actor": c.actor,
                "entity": c.entity,
                "entity_id": c.entity_id,
                "action": c.action,
                "before": c.before,
                "after": c.after,
            }
            for c in s.execute(q.limit(max(1, min(limit, 1000)))).scalars()
        ]

    def who_owns(self, s: Session, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Screens and applications whose key / name / title / route contains ``query`` (+ owner contacts)."""
        term = f"%{query.strip()[:100]}%"
        teams = {t["id"]: t for t in self.list_teams(s)}
        apps = {a["id"]: a for a in self.list_applications(s)}
        out: list[dict[str, Any]] = []
        screens = s.execute(
            select(OwnedScreen)
            .join(OwnedApplication, OwnedApplication.id == OwnedScreen.application_id)
            .where(
                or_(
                    OwnedScreen.key.ilike(term),
                    OwnedScreen.title.ilike(term),
                    OwnedScreen.route_pattern.ilike(term),
                    OwnedApplication.key.ilike(term),
                    OwnedApplication.name.ilike(term),
                )
            )
            .order_by(OwnedApplication.key, OwnedScreen.key)
            .limit(limit)
        ).scalars()
        for sc in screens:
            app = apps[sc.application_id]
            out.append(
                {
                    "application_key": app["key"],
                    "application_name": app["name"],
                    "screen_key": sc.key,
                    "screen_title": sc.title,
                    "route_pattern": sc.route_pattern,
                    "owner": teams.get(sc.owner_team_id),
                    "active": sc.active and app["active"],
                }
            )
        for app in apps.values():
            if len(out) >= limit:
                break
            q = query.strip().casefold()
            if q and (q in app["key"] or q in app["name"].casefold()):
                out.append(
                    {
                        "application_key": app["key"],
                        "application_name": app["name"],
                        "screen_key": None,
                        "screen_title": None,
                        "route_pattern": None,
                        "owner": teams.get(app["default_screen_owner_team_id"] or app["owner_team_id"]),
                        "active": app["active"],
                    }
                )
        return out[:limit]

    # ------------------------------------------------------------------ writes
    @staticmethod
    def _log(
        s: Session,
        actor: str,
        entity: str,
        entity_id: int | None,
        action: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        s.add(
            OwnershipChange(
                actor=actor[:200],
                entity=entity,
                entity_id=entity_id,
                action=action,
                before=before,
                after=after,
                correlation_id=correlation_id_var.get(),
            )
        )

    @staticmethod
    def _require_team(s: Session, team_id: int | None, *, what: str) -> None:
        if team_id is not None and s.get(OwnershipTeam, team_id) is None:
            raise OwnershipError(f"{what}: unknown team id {team_id}")

    def _unique_key(self, s: Session, model: Any, key: str, own_id: int | None, **scope: Any) -> None:
        q = select(model.id).where(model.key == key, *(getattr(model, k) == v for k, v in scope.items()))
        found = s.execute(q).scalar_one_or_none()
        if found is not None and found != own_id:
            raise OwnershipError(f"key {key!r} already exists")

    def save_team(
        self, s: Session, data: TeamInput, actor: str, team_id: int | None = None
    ) -> dict[str, Any]:
        self._unique_key(s, OwnershipTeam, data.key, team_id)
        row = s.get(OwnershipTeam, team_id) if team_id is not None else OwnershipTeam(created_by=actor)
        if row is None:
            raise OwnershipError(f"unknown team id {team_id}")
        before = _team_dict(row) if team_id is not None else None
        for k, v in data.model_dump().items():
            setattr(row, k, v.value if isinstance(v, TeamKind) else v)
        row.updated_by = actor
        s.add(row)
        s.flush()
        s.refresh(row)
        after = _team_dict(row)
        self._log(s, actor, "team", row.id, "update" if before else "create", _clean(before), _clean(after))
        return after

    def save_application(
        self, s: Session, data: ApplicationInput, actor: str, application_id: int | None = None
    ) -> dict[str, Any]:
        self._require_team(s, data.owner_team_id, what="owner_team_id")
        self._require_team(s, data.default_screen_owner_team_id, what="default_screen_owner_team_id")
        self._unique_key(s, OwnedApplication, data.key, application_id)
        row = (
            s.get(OwnedApplication, application_id)
            if application_id is not None
            else OwnedApplication(created_by=actor)
        )
        if row is None:
            raise OwnershipError(f"unknown application id {application_id}")
        before = _app_dict(row) if application_id is not None else None
        for k, v in data.model_dump().items():
            setattr(row, k, v)
        row.updated_by = actor
        s.add(row)
        s.flush()
        after = _app_dict(row)
        self._log(s, actor, "application", row.id, "update" if before else "create", before, after)
        return after

    def save_screen(
        self, s: Session, data: ScreenInput, actor: str, screen_id: int | None = None
    ) -> dict[str, Any]:
        if s.get(OwnedApplication, data.application_id) is None:
            raise OwnershipError(f"unknown application id {data.application_id}")
        self._require_team(s, data.owner_team_id, what="owner_team_id")
        self._unique_key(s, OwnedScreen, data.key, screen_id, application_id=data.application_id)
        row = s.get(OwnedScreen, screen_id) if screen_id is not None else OwnedScreen(created_by=actor)
        if row is None:
            raise OwnershipError(f"unknown screen id {screen_id}")
        before = _screen_dict(row) if screen_id is not None else None
        for k, v in data.model_dump().items():
            setattr(row, k, v)
        row.updated_by = actor
        s.add(row)
        s.flush()
        after = _screen_dict(row)
        self._log(s, actor, "screen", row.id, "update" if before else "create", before, after)
        return after

    def save_rule(
        self, s: Session, data: RuleInput, actor: str, rule_id: int | None = None
    ) -> dict[str, Any]:
        for team_id in (*data.notify_to_team_ids, *data.cc_team_ids):
            self._require_team(s, team_id, what="routing rule")
        row = s.get(RoutingRuleRow, rule_id) if rule_id is not None else RoutingRuleRow(created_by=actor)
        if row is None:
            raise OwnershipError(f"unknown rule id {rule_id}")
        before = _rule_dict(row) if rule_id is not None else None
        dumped = data.model_dump(mode="json")
        row.name = dumped["name"]
        row.priority = dumped["priority"]
        row.match = {k: v for k, v in dumped["match"].items() if v is not None}
        row.notify_to_team_ids = dumped["notify_to_team_ids"]
        row.cc_team_ids = dumped["cc_team_ids"]
        row.channels = dumped["channels"]
        row.active = dumped["active"]
        row.updated_by = actor
        s.add(row)
        s.flush()
        after = _rule_dict(row)
        self._log(s, actor, "rule", row.id, "update" if before else "create", before, after)
        return after

    def set_active(self, s: Session, entity: str, entity_id: int, active: bool, actor: str) -> dict[str, Any]:
        model, to_dict = _ENTITIES[entity]
        row = s.get(model, entity_id)
        if row is None:
            raise OwnershipError(f"unknown {entity} id {entity_id}")
        before = to_dict(row)
        row.active = active
        row.updated_by = actor
        s.flush()
        after: dict[str, Any] = to_dict(row)
        self._log(
            s, actor, entity, entity_id, "activate" if active else "deactivate", _clean(before), _clean(after)
        )
        return after

    def reorder_rules(self, s: Session, ordered_ids: list[int], actor: str) -> None:
        """Assign priorities 10, 20, 30 … in the given order (drag & drop in the dashboard)."""
        for n, rule_id in enumerate(ordered_ids, start=1):
            row = s.get(RoutingRuleRow, rule_id)
            if row is None:
                raise OwnershipError(f"unknown rule id {rule_id}")
            if row.priority != n * 10:
                before = _rule_dict(row)
                row.priority = n * 10
                row.updated_by = actor
                s.flush()
                self._log(s, actor, "rule", rule_id, "reorder", before, _rule_dict(row))

    def delete(self, s: Session, entity: str, entity_id: int, actor: str) -> None:
        model, to_dict = _ENTITIES[entity]
        row = s.get(model, entity_id)
        if row is None:
            raise OwnershipError(f"unknown {entity} id {entity_id}")
        if entity == "team":
            in_use = (
                s.execute(
                    select(func.count())
                    .select_from(OwnedApplication)
                    .where(
                        or_(
                            OwnedApplication.owner_team_id == entity_id,
                            OwnedApplication.default_screen_owner_team_id == entity_id,
                        )
                    )
                ).scalar_one()
                + s.execute(
                    select(func.count())
                    .select_from(OwnedScreen)
                    .where(OwnedScreen.owner_team_id == entity_id)
                ).scalar_one()
            )
            if in_use:
                raise OwnershipError("team still owns applications or screens; reassign or deactivate it")
            for rule in s.execute(select(RoutingRuleRow)).scalars():
                if entity_id in (rule.notify_to_team_ids or []) or entity_id in (rule.cc_team_ids or []):
                    raise OwnershipError(f"team is used by routing rule {rule.name!r}")
        before = to_dict(row)
        s.delete(row)
        s.flush()
        self._log(s, actor, entity, entity_id, "delete", _clean(before), None)

    def bulk_set_screen_owner(self, s: Session, screen_ids: list[int], team_id: int, actor: str) -> int:
        self._require_team(s, team_id, what="owner_team_id")
        changed = 0
        for screen_id in screen_ids:
            row = s.get(OwnedScreen, screen_id)
            if row is None:
                raise OwnershipError(f"unknown screen id {screen_id}")
            if row.owner_team_id != team_id:
                before = _screen_dict(row)
                row.owner_team_id = team_id
                row.updated_by = actor
                s.flush()
                self._log(s, actor, "screen", screen_id, "owner_change", before, _screen_dict(row))
                changed += 1
        return changed

    # ------------------------------------------------------------------ CSV
    def export_csv(self, s: Session) -> str:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for app in self.tree(s):
            for sc in app["screens"]:
                writer.writerow(
                    {
                        "application_key": csv_safe(app["key"]),
                        "screen_key": csv_safe(sc["key"]),
                        "title": csv_safe(sc["title"]),
                        "route_pattern": csv_safe(sc["route_pattern"] or ""),
                        "owner_team_key": csv_safe(sc["owner_team_key"] or ""),
                    }
                )
        return buf.getvalue()

    def import_csv(self, s: Session, content: str, actor: str, *, dry_run: bool = False) -> ImportReport:
        """Create/update screens (and unknown applications, owned by the row's team).

        All or nothing: the rows are applied in a savepoint that is rolled back when any row has an error or
        for ``dry_run``; the report shows what would happen either way.
        """
        report = ImportReport(dry_run=dry_run)
        savepoint = s.begin_nested()
        try:
            self._import_rows(s, content, actor, report)
        except BaseException:
            savepoint.rollback()
            raise
        if report.errors or dry_run:
            savepoint.rollback()
        else:
            savepoint.commit()
        return report

    def _import_rows(self, s: Session, content: str, actor: str, report: ImportReport) -> None:
        reader = csv.DictReader(io.StringIO(content.lstrip("﻿")))
        missing = set(CSV_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise OwnershipError(f"CSV is missing columns: {sorted(missing)}")
        teams = {t["key"]: t["id"] for t in self.list_teams(s)}
        apps = {a["key"]: a for a in self.list_applications(s)}
        for line, row in enumerate(reader, start=2):
            team_id = teams.get((row["owner_team_key"] or "").strip())
            if team_id is None:
                report.errors.append(f"line {line}: unknown owner_team_key {row['owner_team_key']!r}")
                continue
            app_key = (row["application_key"] or "").strip()
            try:
                app = apps.get(app_key)
                if app is None:
                    app = self.save_application(
                        s, ApplicationInput(key=app_key, name=app_key, owner_team_id=team_id), actor
                    )
                    apps[app_key] = app
                    report.created_applications += 1
                data = ScreenInput(
                    application_id=app["id"],
                    key=(row["screen_key"] or "").strip(),
                    title=csv_unsafe((row["title"] or "").strip()),
                    route_pattern=(row["route_pattern"] or "").strip() or None,
                    owner_team_id=team_id,
                )
            except (ValidationError, OwnershipError) as exc:
                report.errors.append(f"line {line}: {_short(exc)}")
                continue
            existing = s.execute(
                select(OwnedScreen).where(
                    OwnedScreen.application_id == app["id"], OwnedScreen.key == data.key
                )
            ).scalar_one_or_none()
            if existing is None:
                self.save_screen(s, data, actor)
                report.created_screens += 1
            elif (existing.title, existing.route_pattern, existing.owner_team_id) == (
                data.title,
                data.route_pattern,
                data.owner_team_id,
            ):
                report.unchanged += 1
            else:
                self.save_screen(
                    s,
                    data.model_copy(update={"tags": list(existing.tags or []), "active": existing.active}),
                    actor,
                    existing.id,
                )
                report.updated_screens += 1


def _short(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors())
    return str(exc)


def _clean(d: dict[str, Any] | None) -> dict[str, Any] | None:
    """Change-log copies drop volatile fields."""
    if d is None:
        return None
    return {k: v for k, v in d.items() if k not in ("updated_at", "updated_by")}


_ENTITIES: dict[str, tuple[Any, Any]] = {
    "team": (OwnershipTeam, _team_dict),
    "application": (OwnedApplication, _app_dict),
    "screen": (OwnedScreen, _screen_dict),
    "rule": (RoutingRuleRow, _rule_dict),
}

"""Ownership of applications and screens, routing rules and the deterministic owner resolution.

Ownership is *data* (managed in the dashboard under /yonetim), not code. :func:`resolve_owner` is a pure
function of the registry snapshot and the incident's application context / classification; no model is
involved. Resolution order:

1. screen by exact key, then by route pattern (most specific pattern first)
2. ``application.default_screen_owner_team``
3. ``application.owner_team``
4. routing rules (by priority) matching category / application / screen / error code / branch code
5. the branch operations team (fallback)

The branch operations team is CC on every decision (unless it is the owner) and matching routing rules
always add their recipients, whatever level decided the owner.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

KEY_RE = r"^[a-z0-9][a-z0-9._-]{0,63}$"
CHANNELS = ("email", "zoom")


class TeamKind(StrEnum):
    BRANCH_OPS = "branch_ops"
    APP_OWNER = "app_owner"
    PLATFORM = "platform"
    SECURITY = "security"
    OTHER = "other"


class MatchedLevel(StrEnum):
    SCREEN_KEY = "SCREEN_KEY"
    SCREEN_ROUTE = "SCREEN_ROUTE"
    APPLICATION_DEFAULT_SCREEN_OWNER = "APPLICATION_DEFAULT_SCREEN_OWNER"
    APPLICATION_OWNER = "APPLICATION_OWNER"
    ROUTING_RULE = "ROUTING_RULE"
    BRANCH_OPS_FALLBACK = "BRANCH_OPS_FALLBACK"


# --------------------------------------------------------------------------------------------- inputs
class TeamInput(BaseModel):
    """Create/update payload of a team (dashboard, REST, CSV)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(pattern=KEY_RE)
    name: str = Field(min_length=2, max_length=200)
    kind: TeamKind = TeamKind.APP_OWNER
    email: str | None = Field(default=None, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    zoom_channel_id: str | None = Field(default=None, max_length=200)
    # Name of the environment variable / Secret key holding the channel's incoming-webhook token.
    zoom_webhook_secret_ref: str | None = Field(default=None, max_length=120, pattern=r"^[A-Z][A-Z0-9_]*$")
    jira_project_key: str | None = Field(default=None, max_length=30, pattern=r"^[A-Z][A-Z0-9_]*$")
    oncall_contact: str | None = Field(default=None, max_length=300)
    # e.g. "Mon-Fri 08:30-17:30" (Europe/Istanbul unless a zone is appended: "Mon-Sat 09:00-18:00 UTC")
    working_hours: str | None = Field(default=None, max_length=120)
    active: bool = True

    @field_validator("working_hours")
    @classmethod
    def _hours(cls, v: str | None) -> str | None:
        if v:
            from branchops.domain.services.working_hours import parse_working_hours

            parse_working_hours(v)  # raises ValueError on a bad spec
        return v or None


class ApplicationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(pattern=KEY_RE)
    name: str = Field(min_length=2, max_length=200)
    owner_team_id: int
    default_screen_owner_team_id: int | None = None
    active: bool = True


_SEGMENT_RE = re.compile(r"^(?:\{[A-Za-z_][A-Za-z0-9_]{0,40}\}|[A-Za-z0-9._~!$&'()+,;=:@%-]*)$")


def validate_route_pattern(pattern: str) -> str:
    """Route patterns are ``/``-separated segments; a segment is literal text or a whole ``{param}``, and
    only the last segment may be ``*`` (the rest of the path). This keeps matching linear (no ReDoS)."""
    if not pattern.startswith("/") or len(pattern) > 300:
        raise ValueError("route pattern must start with '/' and be at most 300 characters")
    segments = pattern.rstrip("/").split("/")[1:]
    for n, segment in enumerate(segments):
        if segment == "*" and n == len(segments) - 1:
            continue
        if not _SEGMENT_RE.match(segment):
            raise ValueError(
                f"invalid route segment {segment!r}: "
                "use literal text, a whole '{param}' segment or a final '*'"
            )
    return pattern


class ScreenInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    application_id: int
    key: str = Field(pattern=KEY_RE)
    title: str = Field(min_length=2, max_length=200)
    route_pattern: str | None = Field(default=None, max_length=300, pattern=r"^/\S*$")
    owner_team_id: int
    tags: list[str] = Field(default_factory=list, max_length=20)
    active: bool = True

    @field_validator("route_pattern")
    @classmethod
    def _route(cls, v: str | None) -> str | None:
        return validate_route_pattern(v) if v else None


class RuleMatch(BaseModel):
    """All present conditions must hold. Globs use ``*`` / ``?``; comparisons are case-insensitive."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    category: str | None = Field(default=None, max_length=64)
    application_key: str | None = Field(default=None, max_length=64)
    screen_key: str | None = Field(default=None, max_length=64)
    error_code_glob: str | None = Field(default=None, max_length=100)
    branch_code_glob: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def _not_empty(self) -> RuleMatch:
        if not any(self.model_dump().values()):
            raise ValueError("a routing rule needs at least one condition")
        return self


class RuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=200)
    priority: int = Field(default=100, ge=0, le=100_000)
    match: RuleMatch
    notify_to_team_ids: list[int] = Field(default_factory=list, max_length=20)
    cc_team_ids: list[int] = Field(default_factory=list, max_length=20)
    channels: list[str] = Field(default_factory=lambda: list(CHANNELS))
    active: bool = True

    @field_validator("channels")
    @classmethod
    def _channels(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - set(CHANNELS))
        if unknown:
            raise ValueError(f"unknown channels {unknown}; allowed: {list(CHANNELS)}")
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _recipients(self) -> RuleInput:
        if not self.notify_to_team_ids and not self.cc_team_ids:
            raise ValueError("a routing rule needs at least one recipient team")
        return self


# --------------------------------------------------------------------------------------------- snapshot
@dataclass(frozen=True)
class Team:
    id: int
    key: str
    name: str
    kind: TeamKind = TeamKind.APP_OWNER
    email: str | None = None
    zoom_channel_id: str | None = None
    zoom_webhook_secret_ref: str | None = None
    jira_project_key: str | None = None
    oncall_contact: str | None = None
    working_hours: str | None = None
    active: bool = True


@dataclass(frozen=True)
class Application:
    id: int
    key: str
    name: str
    owner_team_id: int
    default_screen_owner_team_id: int | None = None
    active: bool = True


@dataclass(frozen=True)
class Screen:
    id: int
    application_id: int
    key: str
    title: str
    owner_team_id: int
    route_pattern: str | None = None
    tags: tuple[str, ...] = ()
    active: bool = True


@dataclass(frozen=True)
class RoutingRule:
    id: int
    name: str
    priority: int
    match: RuleMatch
    notify_to_team_ids: tuple[int, ...] = ()
    cc_team_ids: tuple[int, ...] = ()
    channels: tuple[str, ...] = CHANNELS
    active: bool = True


@dataclass(frozen=True)
class OwnershipRegistry:
    teams: tuple[Team, ...] = ()
    applications: tuple[Application, ...] = ()
    screens: tuple[Screen, ...] = ()
    rules: tuple[RoutingRule, ...] = ()

    def team(self, team_id: int | None) -> Team | None:
        return next((t for t in self.teams if t.id == team_id), None) if team_id is not None else None

    def active_team(self, team_id: int | None) -> Team | None:
        team = self.team(team_id)
        return team if team is not None and team.active else None

    def team_by_key(self, key: str) -> Team | None:
        return next((t for t in self.teams if t.key == key), None)

    def application(self, ref: str | None) -> Application | None:
        """Active application by key, or by name (case-insensitive) as a convenience for free text."""
        if not ref:
            return None
        norm = ref.strip().casefold()
        active = [a for a in self.applications if a.active]
        return next((a for a in active if a.key == norm), None) or next(
            (a for a in active if a.name.casefold() == norm), None
        )

    def screens_of(self, application_id: int) -> list[Screen]:
        return [s for s in self.screens if s.application_id == application_id and s.active]


@dataclass(frozen=True)
class RoutingInput:
    """What the reporting client knows about where the problem happened (subset of ``app_context``)."""

    app_key: str | None = None
    screen_key: str | None = None
    route: str | None = None
    error_code: str | None = None
    branch_code: str | None = None


@dataclass(frozen=True)
class RoutingClassification:
    category: str | None = None
    application: str | None = None  # free text from the analysis (matched against keys and names)


@dataclass(frozen=True)
class RoutingDecision:
    owner_team_id: int | None
    owner_team_key: str | None
    to_team_ids: tuple[int, ...]
    cc_team_ids: tuple[int, ...]
    channels: tuple[str, ...]
    matched_level: MatchedLevel
    unowned_screen: bool
    # The client named a screen (key or route) that is not in the registry, whoever ends up owning it.
    screen_unregistered: bool = False
    application_id: int | None = None
    application_key: str | None = None
    screen_id: int | None = None
    screen_key: str | None = None
    matched_rule_ids: tuple[int, ...] = ()
    reasons: tuple[str, ...] = field(default=())

    @property
    def all_team_ids(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys((*self.to_team_ids, *self.cc_team_ids)))

    def to_json(self) -> dict[str, object]:
        return {
            "owner_team_id": self.owner_team_id,
            "owner_team_key": self.owner_team_key,
            "to_team_ids": list(self.to_team_ids),
            "cc_team_ids": list(self.cc_team_ids),
            "channels": list(self.channels),
            "matched_level": self.matched_level.value,
            "unowned_screen": self.unowned_screen,
            "screen_unregistered": self.screen_unregistered,
            "application_id": self.application_id,
            "application_key": self.application_key,
            "screen_id": self.screen_id,
            "screen_key": self.screen_key,
            "matched_rule_ids": list(self.matched_rule_ids),
            "reasons": list(self.reasons),
        }


# --------------------------------------------------------------------------------------------- matching
_NEVER = re.compile(r"(?!)")


@lru_cache(maxsize=1024)
def route_regex(pattern: str) -> re.Pattern[str]:
    """``/customers/{id}/accounts`` and ``/eft/*`` style patterns -> anchored, linear-time regex.

    Patterns that do not pass :func:`validate_route_pattern` (e.g. stored before validation existed)
    never match instead of risking catastrophic backtracking.
    """
    try:
        validate_route_pattern(pattern)
    except ValueError:
        return _NEVER
    out = []
    segments = pattern.rstrip("/").split("/")[1:]
    for n, segment in enumerate(segments):
        if segment == "*" and n == len(segments) - 1:
            out.append("(?:/.*)?")
        elif segment.startswith("{"):
            out.append("/[^/]+")
        else:
            out.append("/" + re.escape(segment))
    return re.compile("^" + ("".join(out) or "") + "/?$", re.IGNORECASE)


def route_specificity(pattern: str) -> tuple[int, int]:
    """Higher = more specific: more literal characters, fewer wildcards."""
    literal = len(re.sub(r"\{[^/{}]+\}|\*", "", pattern))
    wildcards = pattern.count("*") * 2 + len(re.findall(r"\{[^/{}]+\}", pattern))
    return literal, -wildcards


def route_path(route: str) -> str:
    """Path of an internal URL or route (query and fragment removed)."""
    parts = urlsplit(route.strip())
    path = parts.path if (parts.scheme or parts.netloc) else route.split("?", 1)[0].split("#", 1)[0]
    return path or "/"


def _glob(pattern: str | None, value: str | None) -> bool:
    if pattern is None:
        return True
    return value is not None and fnmatch.fnmatchcase(value.casefold(), pattern.casefold())


def rule_matches(
    rule: RoutingRule,
    *,
    category: str | None,
    application: Application | None,
    application_text: str | None,
    screen_key: str | None,
    error_code: str | None,
    branch_code: str | None,
) -> bool:
    m = rule.match
    if m.category is not None and (category or "").casefold() != m.category.casefold():
        return False
    if m.application_key is not None:
        want = m.application_key.casefold()
        names = {application.key, application.name.casefold()} if application else set()
        if application_text:
            names.add(application_text.strip().casefold())
        if want not in names:
            return False
    if m.screen_key is not None and (screen_key or "").casefold() != m.screen_key.casefold():
        return False
    return _glob(m.error_code_glob, error_code) and _glob(m.branch_code_glob, branch_code)


def match_screen(
    registry: OwnershipRegistry, application: Application, screen_key: str | None, route: str | None
) -> tuple[Screen | None, MatchedLevel | None]:
    screens = registry.screens_of(application.id)
    if screen_key:
        norm = screen_key.strip().casefold()
        exact = next((s for s in screens if s.key == norm), None)
        if exact is not None:
            return exact, MatchedLevel.SCREEN_KEY
    if route:
        path = route_path(route)
        candidates = [s for s in screens if s.route_pattern and route_regex(s.route_pattern).match(path)]
        if candidates:
            best = max(candidates, key=lambda s: (route_specificity(s.route_pattern or ""), s.key))
            return best, MatchedLevel.SCREEN_ROUTE
    return None, None


def resolve_owner(
    app_context: RoutingInput | None,
    classification: RoutingClassification | None,
    registry: OwnershipRegistry,
    *,
    branch_ops_team_key: str,
) -> RoutingDecision:
    """Deterministic owner resolution (see module docstring for the order)."""
    ctx = app_context or RoutingInput()
    cls = classification or RoutingClassification()
    reasons: list[str] = []
    application = registry.application(ctx.app_key) or registry.application(cls.application)
    screen: Screen | None = None
    owner: Team | None = None
    level: MatchedLevel | None = None

    if application is not None:
        screen, screen_level = match_screen(registry, application, ctx.screen_key, ctx.route)
        if screen is not None and (owner := registry.active_team(screen.owner_team_id)) is not None:
            level = screen_level
            reasons.append(f"screen {application.key}/{screen.key} -> {owner.key}")
        elif screen is not None:
            reasons.append(f"screen {application.key}/{screen.key}: owner team inactive")
        if owner is None and (team := registry.active_team(application.default_screen_owner_team_id)):
            owner, level = team, MatchedLevel.APPLICATION_DEFAULT_SCREEN_OWNER
            reasons.append(f"application {application.key} default screen owner -> {team.key}")
        if owner is None and (team := registry.active_team(application.owner_team_id)):
            owner, level = team, MatchedLevel.APPLICATION_OWNER
            reasons.append(f"application {application.key} owner -> {team.key}")
    elif ctx.app_key:
        reasons.append(f"unknown application {ctx.app_key!r}")

    matched = [
        r
        for r in sorted(registry.rules, key=lambda r: (r.priority, r.id))
        if r.active
        and rule_matches(
            r,
            category=cls.category,
            application=application,
            application_text=cls.application,
            screen_key=screen.key if screen else ctx.screen_key,
            error_code=ctx.error_code,
            branch_code=ctx.branch_code,
        )
    ]
    rule_to = [t for r in matched for t in r.notify_to_team_ids if registry.active_team(t)]
    rule_cc = [t for r in matched for t in r.cc_team_ids if registry.active_team(t)]
    if owner is None and rule_to:
        owner, level = registry.active_team(rule_to[0]), MatchedLevel.ROUTING_RULE
        reasons.append(f"routing rule {matched[0].name!r} -> {owner.key if owner else '?'}")

    branch_ops = registry.team_by_key(branch_ops_team_key)
    if owner is None:
        owner, level = branch_ops, MatchedLevel.BRANCH_OPS_FALLBACK
        reasons.append("no owner found: branch operations team (fallback)")
    if matched:
        reasons.append("matched rules: " + ", ".join(r.name for r in matched))

    to_ids = tuple(dict.fromkeys([t for t in [owner.id if owner else None, *rule_to] if t is not None]))
    cc = [*rule_cc]
    if branch_ops is not None:
        cc.append(branch_ops.id)  # always in the loop
    cc_ids = tuple(dict.fromkeys(t for t in cc if t not in to_ids))
    channels = tuple(dict.fromkeys(c for r in matched for c in r.channels)) or CHANNELS
    assert level is not None  # noqa: S101 - every branch above sets the level
    return RoutingDecision(
        owner_team_id=owner.id if owner else None,
        owner_team_key=owner.key if owner else None,
        to_team_ids=to_ids,
        cc_team_ids=cc_ids,
        channels=channels,
        matched_level=level,
        unowned_screen=level is MatchedLevel.BRANCH_OPS_FALLBACK
        and bool(ctx.app_key or ctx.screen_key or ctx.route),
        screen_unregistered=screen is None and bool(ctx.screen_key or ctx.route),
        application_id=application.id if application else None,
        application_key=application.key if application else None,
        screen_id=screen.id if screen else None,
        screen_key=screen.key if screen else (ctx.screen_key or None),
        matched_rule_ids=tuple(r.id for r in matched),
        reasons=tuple(reasons),
    )

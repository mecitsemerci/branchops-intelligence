"""Caller authentication, team resolution, tool authorisation and rate limiting for the MCP server.

Identity comes from the oauth2-proxy sidecar exactly as for the dashboard: :func:`resolve_principal` trusts
the forwarded user/groups headers only when the TCP peer is a trusted proxy host (loopback). The caller's
IdP groups select one or more configured teams (``mcp.teams``); a team defines which incidents are visible
(sources / group keys / branch codes, enforced in SQL), whether free text is masked and which tools exist
for the caller at all.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from branchops.application.queries.scope import WILDCARD, QueryScope, ScopeGrant
from branchops.config.settings import McpFieldMode, McpSettings, McpTeamSettings, SecuritySettings, Settings
from branchops.domain.enums import Role
from branchops.infrastructure.observability.telemetry import get_instruments
from branchops.infrastructure.security.auth import AuthenticationError, Principal, resolve_principal

logger = logging.getLogger(__name__)

CALLER_STATE_KEY = "mcp_caller"

# Tools that additionally need a role (or, for operational tools, a team with ``ops_access``).
ANALYST_TOOLS: frozenset[str] = frozenset({"request_reanalysis"})
OPS_TOOLS: frozenset[str] = frozenset({"get_inbox_health"})


class AccessDeniedError(Exception):
    def __init__(self, status: int, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Team:
    id: str
    grant: ScopeGrant
    fields: McpFieldMode
    tools: frozenset[str] | None  # None = every tool
    ops_access: bool = False

    @classmethod
    def from_settings(cls, t: McpTeamSettings) -> Team:
        return cls(
            id=t.id,
            grant=ScopeGrant.of(sources=t.sources, group_keys=t.group_keys, branch_codes=t.branch_codes),
            fields=t.fields,
            tools=None if WILDCARD in t.tools else frozenset(t.tools),
            ops_access=t.ops_access,
        )

    def lists_tool(self, name: str) -> bool:
        return self.tools is None or name in self.tools


# Development only (``auth_mode: dev`` without any team configured): one unrestricted team.
DEV_TEAM = Team(id="dev", grant=ScopeGrant(), fields="full", tools=None, ops_access=True)


@dataclass(frozen=True)
class Caller:
    principal: Principal
    teams: tuple[Team, ...]

    @property
    def username(self) -> str:
        return self.principal.username

    @property
    def team_label(self) -> str:
        return "+".join(sorted(t.id for t in self.teams))

    @property
    def scope(self) -> QueryScope:
        return QueryScope.any_of(t.grant for t in self.teams)

    def allows_tool(self, name: str) -> bool:
        """The tool is in a team allow-list and the caller meets its role requirement in that team."""
        return any(self._team_allows(t, name) for t in self.teams)

    def for_tool(self, name: str) -> Caller:
        """The caller restricted to the teams that grant ``name``.

        A tool call only gets the data scope and field mode of the teams that allow that tool: membership in
        a broad read-only team must not widen what a narrower team's write tool (or vice versa) can reach.
        """
        return Caller(self.principal, tuple(t for t in self.teams if self._team_allows(t, name)))

    def _team_allows(self, team: Team, name: str) -> bool:
        if not team.lists_tool(name):
            return False
        if name in ANALYST_TOOLS and not self.principal.has(Role.ANALYST):
            return False
        return not (name in OPS_TOOLS and not (team.ops_access or self.principal.has(Role.ADMIN)))

    @property
    def full_text_everywhere(self) -> bool:
        """Every team of the caller returns full text (so nothing the caller can reach is masked)."""
        return bool(self.teams) and all(t.fields == "full" for t in self.teams)

    def full_text_for(self, source: str | None, group_key: str | None, branch_code: str | None) -> bool:
        """Free text is returned unmasked only if a ``fields: full`` team grants this incident."""
        return any(t.fields == "full" and t.grant.matches(source, group_key, branch_code) for t in self.teams)


def resolve_teams(principal: Principal, settings: Settings) -> tuple[Team, ...]:
    teams = tuple(Team.from_settings(t) for t in settings.mcp.teams if principal.groups & set(t.groups))
    if not teams and settings.security.auth_mode == "dev" and not settings.mcp.teams:
        return (DEV_TEAM,)
    return teams


def mcp_security(settings: Settings) -> SecuritySettings:
    """``security`` with each team's groups added to the team's role, for the MCP process only."""
    role_groups = {role: list(groups) for role, groups in settings.security.role_groups.items()}
    for team in settings.mcp.teams:
        role_groups.setdefault(team.role, []).extend(team.groups)
    return settings.security.model_copy(update={"role_groups": role_groups})


def authenticate(headers: Mapping[str, str], client_host: str | None, settings: Settings) -> Caller:
    try:
        principal = resolve_principal(headers, client_host, mcp_security(settings))
    except AuthenticationError as exc:
        raise AccessDeniedError(401, "unauthenticated", str(exc)) from exc
    teams = resolve_teams(principal, settings)
    if not teams:
        raise AccessDeniedError(403, "no_team", "caller is not a member of any MCP team")
    return Caller(principal, teams)


def validate_mcp_settings(settings: Settings, tool_names: frozenset[str]) -> None:
    """Start-up checks: production guard and known tool names in team allow-lists."""
    mcp: McpSettings = settings.mcp
    if settings.app.environment == "production":
        if settings.security.auth_mode == "dev":
            raise ValueError("mcp: security.auth_mode 'dev' is not allowed in production")
        if not mcp.teams:
            raise ValueError("mcp: at least one team must be configured in production (mcp.teams)")
    for team in mcp.teams:
        unknown = sorted(set(team.tools) - tool_names - {WILDCARD})
        if unknown:
            raise ValueError(f"mcp team {team.id!r}: unknown tools {unknown}")


class RateLimiter:
    """Sliding one-minute window per principal, in memory (per replica)."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = per_minute
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, float]:
        """Returns ``(allowed, retry_after_seconds)``."""
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= 60.0:
                hits.popleft()
            if len(hits) >= self._limit:
                return False, max(0.0, 60.0 - (now - hits[0]))
            hits.append(now)
            if len(self._hits) > 10_000:  # bound memory: drop idle principals
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= 60.0]:
                    del self._hits[k]
            return True, 0.0


class McpAuthMiddleware:
    """Pure ASGI middleware guarding the MCP endpoint (401 / 403 / 429 before any MCP processing).

    Health and metrics routes are outside ``path`` and stay unauthenticated (like the worker's probes).
    """

    def __init__(self, app: ASGIApp, *, settings: Settings, path: str, limiter: RateLimiter) -> None:
        self.app = app
        self._settings = settings
        self._path = path.rstrip("/")
        self._limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(self._path):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        client = scope.get("client")
        retry_after = 0.0
        try:
            caller = authenticate(headers, client[0] if client else None, self._settings)
            allowed, retry_after = self._limiter.allow(caller.username)
            if not allowed:
                raise AccessDeniedError(429, "rate_limited", "rate limit exceeded")
        except AccessDeniedError as exc:
            get_instruments().mcp_auth_failures.add(1, {"reason": exc.reason})
            logger.warning("mcp request rejected", extra={"status": exc.status, "reason": exc.reason})
            extra = [(b"retry-after", b"%d" % (int(retry_after) + 1))] if exc.status == 429 else []
            await _json(send, exc.status, {"error": exc.reason, "detail": exc.detail}, extra)
            return
        scope.setdefault("state", {})[CALLER_STATE_KEY] = caller
        limit = self._settings.mcp.max_request_bytes
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            await _json(send, 413, {"error": "too_large", "detail": f"request body over {limit} bytes"}, [])
            return
        await self.app(scope, _limited(receive, limit), send)


class RequestTooLargeError(Exception):
    pass


def _limited(receive: Receive, limit: int) -> Receive:
    """Count streamed (e.g. chunked) body bytes; abort the request once ``limit`` is exceeded."""
    seen = 0

    async def wrapped() -> Message:
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > limit:
                raise RequestTooLargeError(f"request body over {limit} bytes")
        return message

    return wrapped


async def _json(send: Send, status: int, body: dict[str, Any], extra: list[tuple[bytes, bytes]]) -> None:
    data = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(data)).encode()),
                (b"cache-control", b"no-store"),
                *([(b"www-authenticate", b'Bearer realm="branchops"')] if status == 401 else []),
                *extra,
            ],
        }
    )
    await send({"type": "http.response.body", "body": data})

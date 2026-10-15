"""MCP server: team resolution, scope -> SQL, tool allow-lists, masking, truncation, rate limiting."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy.dialects import postgresql

from branchops.application.queries.scope import QueryScope, ScopeGrant
from branchops.config.settings import Settings
from branchops.container import Container
from branchops.domain.enums import Role
from branchops.domain.services.masking import SensitiveDataMasker
from branchops.infrastructure.security.auth import Principal
from branchops.mcp.auth import (
    DEV_TEAM,
    AccessDeniedError,
    Caller,
    RateLimiter,
    Team,
    authenticate,
    resolve_teams,
    validate_mcp_settings,
)
from branchops.mcp.middleware import audit_arguments
from branchops.mcp.output import TextRenderer
from branchops.mcp.server import build_server, http_app
from branchops.mcp.tools import TOOL_NAMES
from branchops.templates.registry import TemplateRegistry
from tests.helpers import TEMPLATES, make_settings

TEAMS: list[dict[str, Any]] = [
    {
        "id": "branch-ops",
        "groups": ["branchops-branch-ops"],
        "fields": "full",
        "tools": ["*"],
        "role": "ANALYST",
    },
    {
        "id": "branch-ops-viewers",
        "groups": ["branchops-branch-ops-viewers"],
        "fields": "full",
        "tools": ["*"],
    },
    {
        "id": "app-support",
        "groups": ["branchops-app-support"],
        "sources": ["jira", "snow"],
        "group_keys": ["OPS", "APP"],
        "fields": "masked",
        "tools": [
            "search_incidents",
            "get_incident",
            "get_incident_analysis",
            "find_similar_incidents",
            "list_recurring_errors",
            "get_template",
            "list_templates",
        ],
    },
    {"id": "audit", "groups": ["branchops-audit"], "fields": "full", "tools": ["*"], "ops_access": True},
]
# Default global role groups: MCP team groups get their roles from ``mcp.teams[].role`` only.
ROLE_GROUPS = {"security": {"auth_mode": "oauth_proxy"}}


def mcp_settings(**overrides: Any) -> Settings:
    return make_settings(**{"mcp": {"teams": TEAMS}, **ROLE_GROUPS, **overrides})


def proxy_headers(user: str, *groups: str) -> dict[str, str]:
    return {"X-Forwarded-User": user, "X-Forwarded-Groups": ",".join(groups)}


def caller_for(settings: Settings, *groups: str, roles: frozenset[Role] = frozenset({Role.VIEWER})) -> Caller:
    principal = Principal("u", roles, frozenset(groups))
    return Caller(principal, resolve_teams(principal, settings))


class TestTeamResolution:
    def test_groups_select_teams(self) -> None:
        settings = mcp_settings()
        caller = authenticate(proxy_headers("ayse", "branchops-app-support"), "127.0.0.1", settings)
        assert caller.username == "ayse"
        assert [t.id for t in caller.teams] == ["app-support"]
        both = authenticate(
            proxy_headers("ali", "branchops-app-support", "branchops-audit"), "127.0.0.1", settings
        )
        assert both.team_label == "app-support+audit"

    def test_team_roles_apply_to_mcp_only(self) -> None:
        from branchops.infrastructure.security.auth import AuthenticationError, resolve_principal

        settings = mcp_settings()
        caller = authenticate(proxy_headers("ali", "branchops-branch-ops"), "127.0.0.1", settings)
        assert caller.principal.has(Role.ANALYST)
        viewer = authenticate(proxy_headers("veli", "branchops-app-support"), "127.0.0.1", settings)
        assert viewer.principal.roles == frozenset({Role.VIEWER})
        # The API/dashboard role mapping is untouched: an MCP-only group gets no API role at all.
        with pytest.raises(AuthenticationError):
            resolve_principal(proxy_headers("ali", "branchops-branch-ops"), "127.0.0.1", settings.security)

    def test_unauthenticated_is_401_and_teamless_is_403(self) -> None:
        settings = mcp_settings()
        with pytest.raises(AccessDeniedError) as untrusted:
            authenticate(proxy_headers("x", "branchops-audit"), "10.0.0.8", settings)
        assert untrusted.value.status == 401
        with pytest.raises(AccessDeniedError) as teamless:
            authenticate(proxy_headers("x", "branchops-viewers"), "127.0.0.1", settings)
        assert teamless.value.status == 403

    def test_dev_mode(self) -> None:
        # No teams configured: one unrestricted development team.
        dev = authenticate({}, None, make_settings())
        assert dev.teams == (DEV_TEAM,) and dev.username == "developer"
        # Teams configured: the dev principal needs matching dev_groups like everybody else.
        with pytest.raises(AccessDeniedError):
            authenticate({}, None, make_settings(mcp={"teams": TEAMS}))
        scoped = authenticate(
            {}, None, make_settings(mcp={"teams": TEAMS}, security={"dev_groups": ["branchops-app-support"]})
        )
        assert [t.id for t in scoped.teams] == ["app-support"]

    def test_production_guard_and_unknown_tools(self) -> None:
        prod = make_settings(**ROLE_GROUPS)
        prod.app.environment = "production"  # the other production guards are tested elsewhere
        with pytest.raises(ValueError, match="at least one team"):
            validate_mcp_settings(prod, TOOL_NAMES)
        prod.security.auth_mode = "dev"
        with pytest.raises(ValueError, match="dev"):
            validate_mcp_settings(prod, TOOL_NAMES)
        validate_mcp_settings(mcp_settings(), TOOL_NAMES)
        bad = make_settings(mcp={"teams": [{"id": "x", "groups": ["g"], "tools": ["run_sql"]}]})
        with pytest.raises(ValueError, match="unknown tools"):
            validate_mcp_settings(bad, TOOL_NAMES)
        with pytest.raises(ValueError, match="unique"):
            make_settings(mcp={"teams": [{"id": "x", "groups": ["g"]}, {"id": "x", "groups": ["h"]}]})


class TestScope:
    @staticmethod
    def compiled(scope: QueryScope) -> str:
        predicate = scope.predicate()
        assert predicate is not None
        return str(predicate.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))

    def test_unrestricted_adds_nothing(self) -> None:
        scope = QueryScope.unrestricted()
        assert scope.predicate() is None
        assert scope.sql("i") == ("", {})
        # A wildcard team makes the union unrestricted.
        assert QueryScope.any_of(
            [ScopeGrant.of(sources=["jira"]), ScopeGrant.of(sources=["*"])]
        ).is_unrestricted

    def test_grant_becomes_and_of_in_lists(self) -> None:
        scope = QueryScope.any_of(
            [ScopeGrant.of(sources=["snow", "jira"], group_keys=["OPS", "APP"], branch_codes=["*"])]
        )
        sql = self.compiled(scope)
        assert "issues.source IN ('jira', 'snow')" in sql
        assert "issues.group_key IN ('APP', 'OPS')" in sql
        assert "branch_code" not in sql

    def test_union_of_teams_is_or_of_grants(self) -> None:
        scope = QueryScope.any_of([ScopeGrant.of(sources=["jira"]), ScopeGrant.of(branch_codes=["1001"])])
        sql = self.compiled(scope)
        assert (
            " OR " in sql and "issues.source IN ('jira')" in sql and "issues.branch_code IN ('1001')" in sql
        )
        fragment, params = scope.sql("i")
        assert (
            fragment
            == " AND ((i.source = ANY(:scope_0_source)) OR (i.branch_code = ANY(:scope_1_branch_code)))"
        )
        assert params == {"scope_0_source": ["jira"], "scope_1_branch_code": ["1001"]}

    def test_empty_grant_list_matches_nothing(self) -> None:
        scope = QueryScope(())
        assert "false" in self.compiled(scope).lower()
        assert scope.sql()[0] == " AND (FALSE)"
        assert not scope.matches("jira", "OPS", None)

    def test_matches_mirrors_sql(self) -> None:
        grant = ScopeGrant.of(sources=["jira"], group_keys=["OPS"])
        assert grant.matches("jira", "OPS", None)
        assert not grant.matches("jira", None, None)  # unknown group is not in an explicit allow-list
        assert not grant.matches("csv", "OPS", "1")


class TestToolPermissions:
    def test_allow_list_and_role_requirements(self) -> None:
        settings = mcp_settings()
        support = caller_for(settings, "branchops-app-support")
        assert support.allows_tool("search_incidents")
        assert not support.allows_tool("get_quality_overview")
        assert not support.allows_tool("request_reanalysis")
        viewer_in_full_team = caller_for(settings, "branchops-branch-ops")
        assert not viewer_in_full_team.allows_tool("request_reanalysis")  # VIEWER role
        analyst = caller_for(settings, "branchops-branch-ops", roles=frozenset({Role.ANALYST}))
        assert analyst.allows_tool("request_reanalysis")
        assert not analyst.allows_tool("get_inbox_health")  # neither ADMIN nor an ops team
        assert caller_for(settings, "branchops-audit").allows_tool("get_inbox_health")
        assert caller_for(settings, "branchops-branch-ops", roles=frozenset({Role.ADMIN})).allows_tool(
            "get_inbox_health"
        )

    def test_tool_calls_only_get_the_scope_of_teams_granting_the_tool(self) -> None:
        settings = mcp_settings()
        writer = Team(
            id="ops-writers",
            grant=ScopeGrant.of(group_keys=["OPS"]),
            fields="masked",
            tools=frozenset({"request_reanalysis"}),
        )
        reader = Team.from_settings(settings.mcp.teams[3])  # audit: everything, read tools
        reader = Team(reader.id, reader.grant, reader.fields, frozenset({"get_incident"}), reader.ops_access)
        caller = Caller(Principal("u", frozenset({Role.ANALYST}), frozenset()), (writer, reader))
        assert caller.scope.is_unrestricted
        reanalysis = caller.for_tool("request_reanalysis")
        assert [t.id for t in reanalysis.teams] == ["ops-writers"]
        assert not reanalysis.scope.matches("jira", "FIN", None)  # not widened by the read-only team
        assert caller.for_tool("get_incident").scope.is_unrestricted
        assert caller.for_tool("get_inbox_health").teams == ()

    def test_full_text_only_where_a_full_team_grants_the_row(self) -> None:
        settings = mcp_settings()
        support = caller_for(settings, "branchops-app-support")
        assert not support.full_text_for("jira", "OPS", "1001")
        full = Team.from_settings(settings.mcp.teams[0])
        restricted_full = Team(
            id="ops-full", grant=ScopeGrant.of(group_keys=["OPS"]), fields="full", tools=None
        )
        mixed = Caller(support.principal, (*support.teams, restricted_full))
        assert mixed.full_text_for("jira", "OPS", None)
        assert not mixed.full_text_for("jira", "APP", None)
        assert Caller(support.principal, (full,)).full_text_for("csv", None, None)


class FakeSession:
    def __init__(self, sink: list[Any]) -> None:
        self._sink = sink

    def add(self, obj: Any) -> None:
        self._sink.append(obj)


class FakeDatabase:
    def __init__(self) -> None:
        self.audit: list[Any] = []

    @contextmanager
    def session(self) -> Iterator[FakeSession]:
        yield FakeSession(self.audit)

    def ping(self) -> bool:
        return True


def fake_container(settings: Settings) -> tuple[Container, FakeDatabase]:
    db = FakeDatabase()
    container = Container.__new__(Container)
    container.settings = settings
    container.db = db  # type: ignore[assignment]
    container.registry = TemplateRegistry.from_directory(TEMPLATES)
    return container, db


class TestServer:
    def test_hidden_tools_are_not_listed_and_cannot_be_called(self) -> None:
        settings = mcp_settings(security={"auth_mode": "dev", "dev_groups": ["branchops-app-support"]})
        container, db = fake_container(settings)
        server, _ = build_server(settings, container, stdio=True)

        async def scenario() -> tuple[set[str], str, Any]:
            async with Client(server) as client:
                names = {t.name for t in await client.list_tools()}
                with pytest.raises(ToolError) as denied:
                    await client.call_tool("get_inbox_health", {})
                ok = await client.call_tool("get_template", {"template_id": "transaction-incident"})
                with pytest.raises(ToolError):
                    await client.call_tool("x" * 5000, {})
                return names, str(denied.value), ok.structured_content

        names, denied, template = asyncio.run(scenario())
        assert names == {
            "search_incidents",
            "get_incident",
            "get_incident_analysis",
            "find_similar_incidents",
            "list_recurring_errors",
            "get_template",
            "list_templates",
        }
        assert "Unknown tool" in denied
        assert template["template_id"] == "transaction-incident" and template["fields"]
        outcomes = [(a.resource_id, a.details["outcome"], a.details["team"]) for a in db.audit]
        assert outcomes == [
            ("get_inbox_health", "denied", "app-support"),
            ("get_template", "ok", "app-support"),
            ("<unknown>", "denied", "app-support"),
        ]
        assert len(db.audit[2].details["requested_tool"]) == 200
        assert all(a.action == "mcp_tool_call" and a.actor == "developer" for a in db.audit)
        assert db.audit[1].details["rows"] == 1 and db.audit[1].details["duration_ms"] >= 0

    def test_stdio_is_refused_outside_dev_auth(self) -> None:
        container, _ = fake_container(mcp_settings())
        with pytest.raises(ValueError, match="stdio"):
            build_server(mcp_settings(), container, stdio=True)

    def test_http_auth_rejections_and_probes(self) -> None:
        settings = mcp_settings(mcp={"teams": TEAMS, "rate_limit_per_minute": 1})
        container, _ = fake_container(settings)
        server, _ = build_server(settings, container)
        app = http_app(server, settings)
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        headers = {"accept": "application/json, text/event-stream"}

        async def scenario() -> list[int]:
            statuses = []
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)), base_url="http://t"
                ) as http,
            ):
                statuses.append((await http.get("/healthz")).status_code)
                statuses.append((await http.get("/readyz")).status_code)
                statuses.append((await http.post("/mcp", json=body, headers=headers)).status_code)
                teamless = {**headers, **proxy_headers("x", "branchops-viewers")}
                statuses.append((await http.post("/mcp", json=body, headers=teamless)).status_code)
                member = {**headers, **proxy_headers("y", "branchops-audit")}
                await http.post("/mcp", json=body, headers=member)
                limited = await http.post("/mcp", json=body, headers=member)
                statuses.append(limited.status_code)
                assert limited.headers["retry-after"]
                huge = {**headers, **proxy_headers("z", "branchops-audit")}
                statuses.append((await http.post("/mcp", content=b"x" * 70_000, headers=huge)).status_code)
            return statuses

        assert asyncio.run(scenario()) == [200, 200, 401, 403, 429, 413]


class TestOutput:
    def test_masked_mode_masks_pii(self) -> None:
        text = TextRenderer(SensitiveDataMasker(), max_chars=4000, mask=True)
        out = text("Müşteri ayse@example.com, IBAN TR33 0006 1005 1978 6457 8413 26", "description")
        assert out is not None
        assert "ayse@example.com" not in out and "TR33 0006" not in out
        full = TextRenderer(SensitiveDataMasker(), max_chars=4000, mask=False)
        assert full("ayse@example.com", "description") == "ayse@example.com"

    def test_truncation_is_marked(self) -> None:
        text = TextRenderer(SensitiveDataMasker(), max_chars=200, mask=False)
        assert text("kısa", "summary") == "kısa"
        out = text("x" * 250, "comments[0].body")
        assert out == "x" * 200 + "…[truncated: 200 of 250 chars]"
        assert text("y" * 120, "summary", limit=100) == "y" * 100 + "…[truncated: 100 of 120 chars]"
        assert text.truncated == ["comments[0].body", "summary"]

    def test_audit_arguments_drop_secrets_and_mask_pii(self) -> None:
        args = audit_arguments(
            {"text": "TCKN 10000000146 hatası", "api_token": "abc", "limit": 5, "q": "z" * 500},
            SensitiveDataMasker(),
        )
        assert "10000000146" not in args["text"]
        assert args["api_token"] == "***" and args["limit"] == 5
        assert len(args["q"]) == 201

    def test_audit_arguments_are_bounded_before_masking(self) -> None:
        deep: Any = "x"
        for _ in range(5000):  # far deeper than the recursion limit
            deep = [deep]
        args = audit_arguments(
            {"nested": deep, "big": "5" * 5_000_000, **{f"k{n}": n for n in range(1000)}},
            SensitiveDataMasker(),
        )
        assert len(args) == 20
        assert args["nested"] == [[["…"]]]
        assert len(args["big"]) == 201

    def test_masked_teams_search_with_masked_input(self) -> None:
        settings = mcp_settings()
        container, _ = fake_container(settings)
        _, runtime = build_server(settings, container)
        probe = "ayse@example.com"
        assert runtime.search_text(caller_for(settings, "branchops-app-support"), probe) != probe
        assert runtime.search_text(caller_for(settings, "branchops-branch-ops"), probe) == probe
        # One full and one masked team: rows reachable only through the masked team must not be probed.
        mixed = caller_for(settings, "branchops-branch-ops", "branchops-app-support")
        assert runtime.search_text(mixed, probe) != probe
        support = caller_for(settings, "branchops-app-support")
        # Probes that evade the masker but would match PostgreSQL tokens are dropped; keys and words stay.
        assert runtime.search_text(support, "EFT 4111 1111 ayse.yilmaz@x jira:OPS-123 OPS-7 hata") == (
            "EFT jira:OPS-123 OPS-7 hata"
        )


class TestRateLimiter:
    def test_sliding_window_per_principal(self) -> None:
        now = [0.0]
        limiter = RateLimiter(2, clock=lambda: now[0])
        assert limiter.allow("a")[0] and limiter.allow("a")[0]
        allowed, retry = limiter.allow("a")
        assert not allowed and retry == pytest.approx(60.0)
        assert limiter.allow("b")[0]  # per principal
        now[0] = 30.0
        assert not limiter.allow("a")[0]
        now[0] = 60.0
        assert limiter.allow("a")[0]

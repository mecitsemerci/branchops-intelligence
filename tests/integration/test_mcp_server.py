"""MCP server end to end over streamable HTTP (in-process ASGI) against PostgreSQL.

Two incidents from different group keys; team ``app-support`` (jira/snow, OPS/APP, masked) sees one, team
``branch-ops`` (everything, full text) sees both. Re-analysis is refused for a VIEWER and audited for an
ANALYST; hidden tools are absent from ``tools/list``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from sqlalchemy import select

from branchops.container import Container
from branchops.infrastructure.database.models import Analysis, AuditLog, Issue, IssueComment, WebhookInbox
from branchops.infrastructure.database.session import Database
from branchops.mcp.server import build_server, http_app
from branchops.templates.registry import TemplateRegistry
from tests.unit.test_mcp import mcp_settings, proxy_headers

pytestmark = pytest.mark.postgres
NOW = datetime.now(UTC)


def seed(db: Database) -> None:
    analysis_id = uuid.uuid4()
    with db.session() as s:
        for key, group, branch, desc in (
            ("OPS-1", "OPS", "1001", "EFT işlemi zaman aşımı. Müşteri e-posta: ayse.yilmaz@example.com"),
            ("FIN-7", "FIN", "2002", "EFT işlemi zaman aşımı, muhasebe fişi oluşmadı."),
        ):
            s.add(
                Issue(
                    incident_key=f"jira:{key}",
                    source="jira",
                    external_key=key,
                    issue_key=key,
                    group_key=group,
                    summary=f"EFT zaman aşımı {key}",
                    description=desc,
                    status="Open",
                    snapshot={},
                    branch_code=branch,
                    category="TRANSACTION",
                    error_signature="EFT_TIMEOUT",
                    source_created_at=NOW - timedelta(days=1),
                    source_updated_at=NOW - timedelta(hours=1),
                    is_deleted=False,
                )
            )
        s.flush()
        s.add(
            IssueComment(
                incident_key="jira:OPS-1", comment_id="1", body="Şube 1001 tekrar denedi", author="mehmet"
            )
        )
        s.add(
            Analysis(
                id=analysis_id,
                incident_key="jira:OPS-1",
                correlation_id="c",
                status="COMPLETED",
                input_hash="h",
                template_id="transaction-incident",
                template_version="1.0.0",
                scoring_policy_version="2026.10-1",
                completeness_score=62.5,
                quality_score=48.0,
                classification={"category": "TRANSACTION", "confidence": "HIGH", "evidence": []},
                field_scores=[
                    {
                        "field": "error_details",
                        "weight": 20,
                        "state": "INSUFFICIENT",
                        "applicable": True,
                        "required": True,
                        "completeness_credit": 1.0,
                        "quality_credit": 0.25,
                        "rubric_level": "POOR",
                    }
                ],
                assessments=[
                    {
                        "field": "error_details",
                        "level": "POOR",
                        "reason": "Hata kodu yok",
                        "evidence": [
                            {"source": "description", "quote": "EFT işlemi zaman aşımı", "verified": True}
                        ],
                    }
                ],
                missing_fields=["reproduction_steps"],
                missing_required_fields=[],
                recommendations=[
                    {"field": "error_details", "priority": "HIGH", "message": "Hata kodunu ekleyin"}
                ],
                started_at=NOW - timedelta(minutes=30),
                completed_at=NOW - timedelta(minutes=29),
            )
        )
        s.flush()
        issue = s.get(Issue, "jira:OPS-1")
        assert issue is not None
        issue.latest_analysis_id = analysis_id
        issue.latest_quality = 48.0
        issue.latest_completeness = 62.5
        issue.latest_status = "COMPLETED"


def run_as[T](
    db: Database, user: str, groups: list[str], scenario: Callable[[Client[Any]], Awaitable[T]]
) -> T:
    settings = mcp_settings(database={"url": db.engine.url.render_as_string(hide_password=False)})
    container = Container.__new__(Container)
    container.settings = settings
    container.db = db
    container.registry = TemplateRegistry.from_directory(Path(settings.templates.directory))
    server, _ = build_server(settings, container)
    app = http_app(server, settings)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        # The oauth2-proxy sidecar is on loopback: the ASGI peer is 127.0.0.1.
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40000)), **kwargs
        )

    async def main() -> T:
        async with app.router.lifespan_context(app):
            transport = StreamableHttpTransport(
                "http://mcp.test/mcp", headers=proxy_headers(user, *groups), httpx_client_factory=factory
            )
            async with Client(transport) as client:
                return await scenario(client)

    return asyncio.run(main())


SUPPORT = ["branchops-app-support"]
BRANCH_OPS = ["branchops-branch-ops"]


def test_team_scopes_are_enforced_in_queries(db: Database) -> None:
    seed(db)

    async def support(client: Client[Any]) -> dict[str, Any]:
        tools = {t.name for t in await client.list_tools()}
        page = (await client.call_tool("search_incidents", {"query": "EFT"})).structured_content
        detail = (await client.call_tool("get_incident", {"incident_key": "jira:OPS-1"})).structured_content
        with pytest.raises(ToolError, match="not found"):
            await client.call_tool("get_incident", {"incident_key": "jira:FIN-7"})
        similar = (
            await client.call_tool("find_similar_incidents", {"incident_key": "OPS-1"})
        ).structured_content
        recurring = (await client.call_tool("list_recurring_errors", {"min_count": 2})).structured_content
        analysis = (
            await client.call_tool("get_incident_analysis", {"incident_key": "jira:OPS-1"})
        ).structured_content
        return {
            "tools": tools,
            "page": page,
            "detail": detail,
            "similar": similar,
            "recurring": recurring,
            "analysis": analysis,
        }

    out = run_as(db, "ayse", SUPPORT, support)
    assert out["tools"] == {
        "search_incidents",
        "get_incident",
        "get_incident_analysis",
        "find_similar_incidents",
        "list_recurring_errors",
        "get_template",
        "list_templates",
    }
    assert [i["incident_key"] for i in out["page"]["items"]] == ["jira:OPS-1"]
    assert out["page"]["total"] == 1
    detail = out["detail"]
    assert detail["fields_mode"] == "masked"
    assert "ayse.yilmaz@example.com" not in detail["description"]
    assert detail["comments"][0]["author"] is None and detail["latest"]["quality"] == 48.0
    assert out["similar"]["items"] == []  # FIN-7 shares the error signature but is out of scope
    assert out["recurring"]["items"] == []  # only one EFT_TIMEOUT incident is visible to app-support
    analysis = out["analysis"]
    assert analysis["analysed"] and analysis["template_id"] == "transaction-incident"
    assert analysis["field_assessments"][0]["level"] == "POOR"
    assert analysis["evidence"][0]["verified"] is True

    async def branch_ops(client: Client[Any]) -> dict[str, Any]:
        page = (await client.call_tool("search_incidents", {"sort": "incident_key"})).structured_content
        detail = (await client.call_tool("get_incident", {"incident_key": "jira:OPS-1"})).structured_content
        similar = (
            await client.call_tool("find_similar_incidents", {"incident_key": "jira:OPS-1"})
        ).structured_content
        overview = (await client.call_tool("get_quality_overview", {})).structured_content
        recurring = (await client.call_tool("list_recurring_errors", {"min_count": 2})).structured_content
        return {
            "page": page,
            "detail": detail,
            "similar": similar,
            "overview": overview,
            "recurring": recurring,
        }

    out = run_as(db, "ali", BRANCH_OPS, branch_ops)
    assert sorted(i["incident_key"] for i in out["page"]["items"]) == ["jira:FIN-7", "jira:OPS-1"]
    assert out["detail"]["fields_mode"] == "full"
    assert "ayse.yilmaz@example.com" in out["detail"]["description"]
    assert out["detail"]["comments"][0]["author"] == "mehmet"
    assert [(i["incident_key"], i["relationship"]) for i in out["similar"]["items"]] == [
        ("jira:FIN-7", "RECURRING")
    ]
    assert out["overview"]["total_incidents"] == 2
    groups = out["recurring"]["items"]
    assert [(g["error_signature"], g["count"], g["branch_codes"]) for g in groups] == [
        ("EFT_TIMEOUT", 2, ["1001", "2002"])
    ]

    with db.session() as s:
        calls = s.execute(
            select(AuditLog.actor, AuditLog.resource_id, AuditLog.details)
            .where(AuditLog.action == "mcp_tool_call")
            .order_by(AuditLog.id)
        ).all()
    assert len(calls) == 11
    actor, tool, details = calls[0]
    assert (actor, tool, details["team"], details["outcome"], details["rows"]) == (
        "ayse",
        "search_incidents",
        "app-support",
        "ok",
        1,
    )
    assert [c.details["outcome"] for c in calls if c.resource_id == "get_incident"][:2] == ["ok", "rejected"]


def test_reanalysis_is_refused_for_viewers_and_audited_for_analysts(db: Database) -> None:
    seed(db)

    async def viewer(client: Client[Any]) -> set[str]:
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool(
                "request_reanalysis", {"incident_key": "jira:OPS-1", "reason": "şablon değişti"}
            )
        return {t.name for t in await client.list_tools()}

    # A branch-ops team member whose IdP groups only map to the VIEWER role.
    viewer_tools = run_as(db, "veli", ["branchops-branch-ops-viewers"], viewer)
    assert "request_reanalysis" not in viewer_tools and "get_inbox_health" not in viewer_tools

    async def analyst(client: Client[Any]) -> dict[str, Any]:
        result = await client.call_tool(
            "request_reanalysis", {"incident_key": "OPS-1", "reason": "Yeni şablon sürümü yayınlandı"}
        )
        return dict(result.structured_content or {})

    queued = run_as(db, "ali", BRANCH_OPS, analyst)
    assert queued["status"] == "queued" and queued["incident_key"] == "jira:OPS-1"
    with db.session() as s:
        event = s.execute(select(WebhookInbox)).scalar_one()
        assert (event.event_type, event.source, event.issue_key) == ("MANUAL_REANALYSIS", "jira", "OPS-1")
        rows = s.execute(
            select(AuditLog.actor, AuditLog.action, AuditLog.resource_id, AuditLog.details)
        ).all()
    calls = {(r.actor, r.details["outcome"]) for r in rows if r.resource_id == "request_reanalysis"}
    assert calls == {("veli", "denied"), ("ali", "ok")}
    requested = [r for r in rows if r.action == "reanalysis_requested"]
    assert [(r.actor, r.resource_id, r.details["via"]) for r in requested] == [("ali", "jira:OPS-1", "mcp")]


def test_scoped_read_models_produce_valid_sql(db: Database) -> None:
    """Every scoped query (ORM predicates and raw-SQL fragments) runs and narrows the result."""
    from branchops.application.queries import QueryScope, ReadModels, ScopeGrant
    from branchops.application.queries.filters import IssueFilter

    seed(db)
    rm = ReadModels(50.0)
    ops = QueryScope.any_of([ScopeGrant.of(sources=["jira"], group_keys=["OPS"])])
    nothing = QueryScope(())
    with db.session() as s:
        assert rm.overview(s, scope=ops).total_issues == 1
        assert rm.overview(s, scope=nothing).total_issues == 0
        assert rm.overview(s).total_issues == 2
        assert [r["branch_code"] for r in rm.quality_analytics(s, scope=ops)["by_branch"]] == ["1001"]
        assert rm.quality_analytics(s, scope=nothing)["by_category"] == []
        assert rm.list_issues(s, IssueFilter(since_days=7), scope=ops).total == 1
        assert rm.issue_detail(s, "jira:FIN-7", scope=ops) is None
        assert rm.similar_to_text(s, "EFT zaman aşımı", scope=ops)[0]["incident_key"] == "jira:OPS-1"
        assert len(rm.similar_to_text(s, "EFT zaman aşımı")) == 2
        assert rm.recurring_errors(s, min_count=2, scope=ops) == []
        health = rm.inbox_health(s)
        assert health["pending"] == 0 and health["active_workers"] == 0

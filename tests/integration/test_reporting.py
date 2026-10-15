"""Reporting via MCP end to end: report -> inbox (+ ticket request) -> worker -> update -> ticketing drain."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import select

from branchops.config.settings import Settings
from branchops.container import Container
from branchops.infrastructure.database.models import Issue, TicketingOutbox, WebhookInbox
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.ticketing.handler import TICKETING_OUTBOX, TicketingHandler
from branchops.infrastructure.ticketing.jira_sink import JiraTicketingSink
from branchops.workers.outbox import OutboxDrainer
from tests.integration.test_end_to_end import drain, make_dispatcher
from tests.integration.test_mcp_server import run_as
from tests.unit.test_mcp import mcp_settings

pytestmark = pytest.mark.postgres

DRAFT: dict[str, Any] = {
    "title": "EFT ekranında gönder butonu zaman aşımı veriyor",
    "description": "Şubede müşteri EFT işlemi yaparken gönder butonuna basınca ekran donuyor ve 30 saniye "
    "sonra zaman aşımı hatası veriyor, işlem gerçekleşmiyor. Müşteri tel 0532 123 45 67.",
    "branch_code": "1001",
    "application": "EFT Uygulaması",
    "error_details": "EFT-504 Gateway timeout",
    "occurred_at": "2026-10-15T09:12:00+03:00",
}
BRANCH_OPS = ["branchops-branch-ops"]


def reporting_settings(db: Database, **overrides: Any) -> Settings:
    return mcp_settings(
        database={"url": db.engine.url.render_as_string(hide_password=False)},
        reporting={"enabled": True},
        ticketing={"sink": "jira", "base_url": "https://jira.bank.internal", "default_project_key": "OPS"},
        worker={"concurrency": 1, "batch_size": 1},
        **overrides,
    )


def test_report_update_and_ticket(db: Database) -> None:
    settings = reporting_settings(db)

    async def report(client: Client[Any]) -> dict[str, Any]:
        tools = {t.name for t in await client.list_tools()}
        weak = (
            await client.call_tool("check_incident_draft", {"draft": {**DRAFT, "branch_code": None}})
        ).structured_content
        first = (
            await client.call_tool("report_incident", {"draft": DRAFT, "idempotency_key": "ui-42"})
        ).structured_content
        again = (
            await client.call_tool("report_incident", {"draft": DRAFT, "idempotency_key": "ui-42"})
        ).structured_content
        return {"tools": tools, "weak": weak, "first": first, "again": again}

    out = run_as(db, "ali", BRANCH_OPS, report, settings)
    assert {"check_incident_draft", "report_incident", "add_incident_update"} <= out["tools"]
    assert out["weak"]["ready"] is False
    assert [g["field"] for g in out["weak"]["missing_required"]] == ["branch_code"]
    key = out["first"]["incident_key"]
    assert key.startswith("app:RPT-") and out["first"]["status"] == "accepted"
    assert out["first"]["ticketing"] == "queued" and out["first"]["check"]["ready"] is True
    assert out["again"]["incident_key"] == key and out["again"]["status"] == "duplicate"

    with db.session() as s:
        inbox = s.execute(select(WebhookInbox)).scalars().all()
        assert [(r.source, r.event_type) for r in inbox] == [("app", "RECORD_CREATED")]
        ticket = s.execute(select(TicketingOutbox)).scalar_one()
        assert ticket.kind == "create_ticket" and ticket.payload["project_key"] == "OPS"
        assert "0532 123 45 67" not in ticket.payload["description"]  # masked before leaving BranchOps

    container = Container.build(settings)
    try:
        drain(make_dispatcher(container, settings), db)
    finally:
        container.close()
    with db.session() as s:
        issue = s.get(Issue, key)
        assert issue is not None and issue.source == "app" and issue.latest_analysis_id is not None
        assert issue.snapshot["reporter"] == "ali"
        assert issue.snapshot["mapped_fields"]["application"] == "EFT Uygulaması"
        assert "0532 123 45 67" not in (issue.description or "")

    async def update(client: Client[Any]) -> Any:
        return (
            await client.call_tool(
                "add_incident_update", {"incident_key": key, "text": "Tekrar denendi, düzeldi."}
            )
        ).structured_content

    assert run_as(db, "ali", BRANCH_OPS, update, settings)["incident_key"] == key

    async def outsider(client: Client[Any]) -> None:
        with pytest.raises(ToolError):
            await client.call_tool("add_incident_update", {"incident_key": key, "text": "başkasının kaydı"})

    run_as(db, "veli", ["branchops-app-support"], outsider, settings)  # source "app" is out of its scope

    calls: list[str] = []

    def jira(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/rest/api/2/issue":
            return httpx.Response(201, json={"key": "OPS-501"})
        return httpx.Response(201, json={})

    sink = JiraTicketingSink(settings.ticketing, settings.network, transport=httpx.MockTransport(jira))
    drainer = OutboxDrainer(
        "ticketing", db, TICKETING_OUTBOX, TicketingHandler(db, sink), settings.outbox, "w"
    )
    for _ in range(3):
        drainer.drain_once()
    assert calls == ["/rest/api/2/issue", "/rest/api/2/issue/OPS-501/comment"]
    with db.session() as s:
        rows = s.execute(select(TicketingOutbox).order_by(TicketingOutbox.created_at)).scalars().all()
        assert [(r.kind, r.status) for r in rows] == [("create_ticket", "DONE"), ("add_comment", "DONE")]
        assert rows[0].result is not None and rows[0].result["ticket_key"] == "OPS-501"


def test_comment_before_ticket_is_deferred_without_consuming_attempts(db: Database) -> None:
    settings = reporting_settings(db)
    with db.session() as s:
        TICKETING_OUTBOX.enqueue(
            s,
            idempotency_key="ticket:app:RPT-X:comment:1",
            kind="add_comment",
            incident_key="app:RPT-X",
            payload={"body": "x"},
        )
    sink = JiraTicketingSink(
        settings.ticketing, settings.network, transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    drainer = OutboxDrainer(
        "ticketing", db, TICKETING_OUTBOX, TicketingHandler(db, sink), settings.outbox, "w"
    )
    assert drainer.drain_once() == 1
    with db.session() as s:
        row = s.execute(select(TicketingOutbox)).scalar_one()
        assert (row.status, row.attempts, row.last_error) == ("PENDING", 0, "ticket not created yet")

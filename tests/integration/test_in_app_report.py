"""In-app report end to end: app_context + screenshot via MCP -> bound attachments -> ticket attachment."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import select

from branchops.api.app import create_app
from branchops.application.commands.ownership import OwnershipService
from branchops.container import Container
from branchops.domain.entities.ownership import ApplicationInput, ScreenInput, TeamInput, TeamKind
from branchops.infrastructure.database.models import (
    Attachment,
    AuditLog,
    IncidentAppContext,
    TicketingOutbox,
    WebhookInbox,
)
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.ticketing.handler import TICKETING_OUTBOX, TicketingHandler
from branchops.infrastructure.ticketing.jira_sink import JiraTicketingSink
from branchops.workers.outbox import OutboxDrainer
from tests.integration.test_mcp_server import run_as
from tests.integration.test_reporting import DRAFT, reporting_settings
from tests.unit.test_attachments import png

pytestmark = pytest.mark.postgres
APP_CONTEXT: dict[str, Any] = {
    "app_id": "eft",
    "screen_id": "eft-new",
    "route": "/eft/new?musteri=10000000146",
    "build_version": "4.12.0",
    "environment": "prod",
    "error_code": "EFT-504",
    "error_message": "Gateway timeout (müşteri ayse@example.com)",
    "last_requests": [
        {"method": "POST", "path": "/api/eft/send?iban=TR1", "status": 504, "duration_ms": 30012}
    ],
    "log_excerpt": "2026-10-15 09:12 ERROR EFT-504 timeout",
    "user_context": {"branch_code": "1001", "username": "ayse.yilmaz", "role": "gişe"},
    "device": {"os": "Windows 11", "browser": "Edge 128", "resolution": "1920x1080"},
}


def seed_registry(db: Database, settings: Any) -> None:
    own = OwnershipService(db, settings)
    own.save_team(TeamInput(key="branch-ops", name="Şube Operasyon", kind=TeamKind.BRANCH_OPS), "admin")
    eft = own.save_team(TeamInput(key="eft-team", name="EFT Ekibi", jira_project_key="EFT"), "admin")
    app = own.save_application(
        ApplicationInput(key="eft", name="EFT Uygulaması", owner_team_id=eft["id"]), "a"
    )
    own.save_screen(
        ScreenInput(
            application_id=app["id"], key="eft-new", title="Yeni EFT Gönderimi", owner_team_id=eft["id"]
        ),
        "admin",
    )


def test_screenshot_flow(db: Database) -> None:
    settings = reporting_settings(db)
    seed_registry(db, settings)
    shot = base64.b64encode(png(320, 200, exif=True, fmt="JPEG")).decode()
    draft = {k: v for k, v in DRAFT.items() if k != "application"} | {"app_context": APP_CONTEXT}

    async def flow(client: Client[Any]) -> dict[str, Any]:
        check = (await client.call_tool("check_incident_draft", {"draft": draft})).structured_content
        token = check["draft_token"]
        upload = (
            await client.call_tool(
                "upload_attachment",
                {
                    "draft_token": token,
                    "filename": "ekran.jpg",
                    "content_type": "image/jpeg",
                    "data_base64": shot,
                    "blur_regions": [[0, 0, 100, 40]],
                },
            )
        ).structured_content
        with pytest.raises(ToolError, match=r"content type|not a valid"):
            await client.call_tool(
                "upload_attachment",
                {"draft_token": token, "filename": "x.png", "content_type": "image/png", "data_base64": shot},
            )
        report = (
            await client.call_tool("report_incident", {"draft": draft, "draft_token": token})
        ).structured_content
        with pytest.raises(ToolError, match="already used"):
            await client.call_tool(
                "upload_attachment",
                {
                    "draft_token": token,
                    "filename": "y.txt",
                    "content_type": "text/plain",
                    "data_base64": "eA==",
                },
            )
        return {"check": check, "upload": upload, "report": report}

    out = run_as(db, "ali", ["branchops-branch-ops"], flow, settings)
    # The application is known from the registry, so it is not asked for.
    optional = {g["field"] for g in out["check"]["missing_optional"]}
    assert "application" not in optional and out["check"]["ready"] is True
    assert out["upload"]["incident_key"] is None and out["upload"]["blurred_regions"] == 1
    assert out["upload"]["ticketing"] == "pending_report"
    key = out["report"]["incident_key"]
    assert out["report"]["extra"]["attachments_bound"] == 1

    with db.session() as s:
        att = s.execute(select(Attachment)).scalar_one()
        assert (att.incident_key, att.draft_id, att.content_type) == (key, None, "image/jpeg")
        assert att.data is not None and "Ayşe".encode() not in att.data
        ctx = s.execute(select(IncidentAppContext)).scalar_one()
        stored = json.dumps(ctx.context, ensure_ascii=False)
        assert (
            "10000000146" not in stored and "ayse@example.com" not in stored and "ayse.yilmaz" not in stored
        )
        assert ctx.context["route"] == "/eft/new"
        envelope = s.execute(select(WebhookInbox.payload)).scalar_one()["incident_event"]
        assert envelope["group_key"] == "EFT"
        assert envelope["mapped_fields"]["application"] == "EFT Uygulaması"
        assert envelope["mapped_fields"]["screen"] == "Yeni EFT Gönderimi"
        assert "app_context" not in json.dumps(envelope)  # never part of the analysed record
        kinds = [
            r.kind for r in s.execute(select(TicketingOutbox).order_by(TicketingOutbox.created_at)).scalars()
        ]
        assert kinds == ["create_ticket", "add_attachment"]

    uploads: list[httpx.Request] = []

    def jira(request: httpx.Request) -> httpx.Response:
        uploads.append(request)
        if request.url.path == "/rest/api/2/issue":
            return httpx.Response(201, json={"key": "OPS-9"})
        return httpx.Response(200, json=[])

    from branchops.application.commands.attachments import AttachmentService
    from branchops.infrastructure.attachments import build_attachment_store

    attachments = AttachmentService(settings, build_attachment_store(settings))

    def loader(attachment_id: str) -> Any:
        with db.session() as s:
            return attachments.load(s, attachment_id)

    sink = JiraTicketingSink(settings.ticketing, settings.network, transport=httpx.MockTransport(jira))
    drainer = OutboxDrainer(
        "ticketing", db, TICKETING_OUTBOX, TicketingHandler(db, sink, loader), settings.outbox, "w"
    )
    for _ in range(3):
        drainer.drain_once()
    assert [r.url.path for r in uploads] == ["/rest/api/2/issue", "/rest/api/2/issue/OPS-9/attachments"]
    assert b'filename="ekran.jpg"' in uploads[1].content

    container = Container.build(settings)
    viewer = {"X-Forwarded-User": "denetci", "X-Forwarded-Groups": "branchops-viewers"}
    with TestClient(create_app(settings, container), client=("127.0.0.1", 5000), headers=viewer) as api:
        response = api.get(f"/api/issues/{key}/attachments")
        assert response.status_code == 200, response.text
        listed = response.json()
        assert [a["filename"] for a in listed] == ["ekran.jpg"]
        downloaded = api.get(f"/api/attachments/{listed[0]['id']}")
        assert downloaded.status_code == 200 and downloaded.content == att.data
        assert downloaded.headers["content-disposition"].startswith("attachment;")
    with db.session() as s:
        actions = [a for a in s.execute(select(AuditLog.action)).scalars() if a.startswith("attachment")]
        assert actions == ["attachment_downloaded"]


def test_attachment_limit_per_incident(db: Database) -> None:
    settings = reporting_settings(db)
    text = base64.b64encode(b"log satiri").decode()

    async def flow(client: Client[Any]) -> str:
        key = (await client.call_tool("report_incident", {"draft": DRAFT})).structured_content["incident_key"]
        for n in range(5):
            await client.call_tool(
                "upload_attachment",
                {
                    "incident_key": key,
                    "filename": f"l{n}.txt",
                    "content_type": "text/plain",
                    "data_base64": text,
                    "kind": "log",
                },
            )
        with pytest.raises(ToolError, match="at most 5"):
            await client.call_tool(
                "upload_attachment",
                {
                    "incident_key": key,
                    "filename": "l6.txt",
                    "content_type": "text/plain",
                    "data_base64": text,
                },
            )
        return str(key)

    run_as(db, "ali", ["branchops-branch-ops"], flow, settings)
    with db.session() as s:
        assert len(s.execute(select(Attachment)).scalars().all()) == 5

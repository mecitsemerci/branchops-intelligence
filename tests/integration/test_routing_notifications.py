"""Routing and notifications end to end (PostgreSQL).

report with screenshot via the fastmcp client -> incident routed to the screen owner -> notification_outbox
rows for the owner (To) and branch-ops (CC) -> worker drains with a fake SMTP relay and a fake Zoom webhook ->
analysis_ready after the workflow -> an admin changes the screen owner -> re-route picks the new owner.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from sqlalchemy import select

from branchops.api.app import create_app
from branchops.application.commands.notifications import NOTIFICATION_OUTBOX
from branchops.application.commands.ownership import OwnershipService
from branchops.application.commands.routing import IncidentRouter
from branchops.application.handlers.process_event import EventProcessor
from branchops.config.settings import Settings
from branchops.container import Container
from branchops.domain.entities.ownership import ApplicationInput, ScreenInput, TeamInput, TeamKind
from branchops.infrastructure.database.models import AuditLog, IncidentRouting, NotificationOutbox
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.notifications.handler import NotificationHandler
from branchops.infrastructure.notifications.smtp import SmtpEmailChannel
from branchops.infrastructure.notifications.zoom import ZoomTeamChatChannel
from branchops.workers.dispatcher import Dispatcher
from branchops.workers.outbox import OutboxDrainer
from branchops.workers.processor import InboxItemRunner
from tests.helpers import ScriptedProvider
from tests.integration.test_end_to_end import drain
from tests.integration.test_in_app_report import APP_CONTEXT
from tests.integration.test_mcp_server import run_as
from tests.integration.test_reporting import DRAFT
from tests.unit.test_attachments import png
from tests.unit.test_mcp import mcp_settings
from tests.unit.test_notifications import FakeSMTP

pytestmark = pytest.mark.postgres
BRANCH_OPS = ["branchops-branch-ops"]


@pytest.fixture
def settings(db: Database, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ZOOM_OPS", "zoom-token-ops")
    return mcp_settings(
        database={"url": db.engine.url.render_as_string(hide_password=False)},
        reporting={"enabled": True},
        worker={"concurrency": 1, "batch_size": 1},
        dashboard={"public_url": "https://branchops.apps.bank.internal"},
        notifications={
            "email": {"backend": "smtp", "smtp_host": "smtp-relay.bank.internal"},
            "zoom": {"backend": "webhook", "webhook_base_url": "https://zoom-egress.bank.internal/hooks"},
        },
    )


def seed(own: OwnershipService) -> dict[str, Any]:
    ops = own.save_team(
        TeamInput(
            key="branch-ops",
            name="Şube Operasyon",
            kind=TeamKind.BRANCH_OPS,
            email="subeops@bank.internal",
            zoom_channel_id="ops-channel",
            zoom_webhook_secret_ref="ZOOM_OPS",
        ),
        "admin",
    )
    platform = own.save_team(
        TeamInput(key="platform", name="Platform", kind=TeamKind.PLATFORM, email="platform@bank.internal"),
        "admin",
    )
    eft = own.save_team(TeamInput(key="eft-team", name="EFT Ekibi", email="eft@bank.internal"), "admin")
    pay = own.save_team(TeamInput(key="payments", name="Ödemeler", email="odeme@bank.internal"), "admin")
    app = own.save_application(
        ApplicationInput(key="eft", name="EFT Uygulaması", owner_team_id=pay["id"]), "admin"
    )
    screen = own.save_screen(
        ScreenInput(
            application_id=app["id"], key="eft-new", title="Yeni EFT Gönderimi", owner_team_id=eft["id"]
        ),
        "admin",
    )
    return {"ops": ops, "platform": platform, "eft": eft, "pay": pay, "app": app, "screen": screen}


def outbox_rows(db: Database) -> list[NotificationOutbox]:
    with db.session() as s:
        rows = s.execute(select(NotificationOutbox).order_by(NotificationOutbox.created_at)).scalars().all()
        for r in rows:
            s.expunge(r)
        return list(rows)


class Delivery:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.zoom_requests: list[httpx.Request] = []
        FakeSMTP.instances.clear()

        def zoom(request: httpx.Request) -> httpx.Response:
            self.zoom_requests.append(request)
            return httpx.Response(200)

        channels = {
            "email": SmtpEmailChannel(settings.notifications.email, settings.network, smtp_factory=FakeSMTP),
            "zoom": ZoomTeamChatChannel(
                settings.notifications.zoom, settings.network, transport=httpx.MockTransport(zoom)
            ),
        }
        self.drainer = OutboxDrainer(
            "notifications", db, NOTIFICATION_OUTBOX, NotificationHandler(db, channels), settings.outbox, "w"
        )

    def drain(self) -> None:
        while self.drainer.drain_once():
            pass

    @property
    def emails(self) -> list[tuple[list[str], Any]]:
        return [(c[3], c[1]) for smtp in FakeSMTP.instances for c in smtp.calls if c[0] == "send"]


@pytest.fixture
def container(settings: Settings) -> Iterator[Container]:
    c = Container.build(settings)
    yield c
    c.close()


def run_worker(container: Container, settings: Settings, db: Database) -> None:
    processor = EventProcessor(
        db=container.db,
        settings=settings,
        registry=container.registry,
        workflow=container.build_workflow(ScriptedProvider()),
        on_analysis=IncidentRouter(container.db, settings).on_analysis_complete,
    )
    runner = InboxItemRunner(container.db, processor, settings.worker, "test-worker")
    drain(Dispatcher(container.db, runner, settings.worker, "test-worker"), db)


def test_report_route_notify_analyse_reroute(db: Database, settings: Settings, container: Container) -> None:
    ids = seed(OwnershipService(db, settings))
    shot = base64.b64encode(png(200, 120)).decode()
    draft = {**DRAFT, "app_context": APP_CONTEXT}

    async def report(client: Client[Any]) -> dict[str, Any]:
        owner = (
            await client.call_tool("resolve_owner", {"app_id": "eft", "screen_id": "eft-new"})
        ).structured_content
        token = (await client.call_tool("check_incident_draft", {"draft": draft})).structured_content[
            "draft_token"
        ]
        await client.call_tool(
            "upload_attachment",
            {"draft_token": token, "filename": "ekran.png", "content_type": "image/png", "data_base64": shot},
        )
        result = (
            await client.call_tool("report_incident", {"draft": draft, "draft_token": token})
        ).structured_content
        return {"owner": owner, "result": result}

    out = run_as(db, "ali", BRANCH_OPS, report, settings)
    assert out["owner"]["owner"]["key"] == "eft-team"
    key = out["result"]["incident_key"]
    routing = out["result"]["extra"]["routing"]
    assert routing["owner_team"] == "eft-team" and routing["cc_teams"] == ["branch-ops"]
    assert routing["matched_level"] == "SCREEN_KEY" and routing["notifications_queued"] == 3

    rows = outbox_rows(db)
    assert [(r.event, r.channel, r.recipient, r.role) for r in rows] == [
        ("incident_reported", "email", "eft@bank.internal", "to"),
        ("incident_reported", "email", "subeops@bank.internal", "cc"),
        ("incident_reported", "zoom", "ops-channel", "cc"),
    ]
    assert "0532 123 45 67" not in json.dumps(rows[0].payload, ensure_ascii=False)  # masked description

    delivery = Delivery(db, settings)
    delivery.drain()
    assert [rcpts for rcpts, _ in delivery.emails] == [["eft@bank.internal"], ["subeops@bank.internal"]]
    first = delivery.emails[0][1]
    assert first["To"] == "eft@bank.internal" and first["Cc"] == "subeops@bank.internal"
    assert "Yeni EFT Gönderimi" in first.get_body(("plain",)).get_content()
    card = json.loads(delivery.zoom_requests[0].content)
    assert delivery.zoom_requests[0].headers["authorization"] == "zoom-token-ops"
    assert any("Ekler: 1 dosya" in b["text"] for b in card["body"])
    with db.session() as s:
        assert s.get(IncidentRouting, key).first_notified_at is not None  # type: ignore[union-attr]

    run_worker(container, settings, db)  # analysis -> analysis_ready to the routed teams
    ready = [r for r in outbox_rows(db) if r.event == "analysis_ready"]
    assert {(r.channel, r.recipient) for r in ready} == {
        ("email", "eft@bank.internal"),
        ("email", "subeops@bank.internal"),
        ("zoom", "ops-channel"),
    }
    assert "Analiz tamamlandı" in ready[0].payload["subject"]
    delivery.drain()
    assert all(r.status == "DONE" for r in outbox_rows(db))

    # Admin changes the screen owner (same service as the dashboard /yonetim page), then re-routes.
    with TestClient(
        create_app(settings, container),
        client=("127.0.0.1", 1),
        headers={
            "X-Forwarded-User": "yonetici",
            "X-Forwarded-Groups": "branchops-admins",
        },
    ) as api:
        changed = api.post(
            "/api/admin/ownership/screens/bulk-owner",
            json={"screen_ids": [ids["screen"]["id"]], "owner_team_id": ids["pay"]["id"]},
        )
        assert changed.json() == {"changed": 1}
        rerouted = api.post(f"/api/routing/{key}/reroute").json()
        assert rerouted["owner_team"] == "payments"
        assert api.get(f"/api/routing/{key}").json()["decided_by"] == "reroute:yonetici"
        log = api.get("/api/notifications", params={"incident_key": key}).json()
    new_rows = [r for r in outbox_rows(db) if r.recipient == "odeme@bank.internal"]
    assert [(r.event, r.role) for r in new_rows] == [("incident_reported", "to")]
    assert "yeniden yönlendirildi" in new_rows[0].payload["text"]
    assert {r["event"] for r in log} == {"incident_reported", "analysis_ready"}
    with db.session() as s:
        assert s.execute(select(AuditLog.action).where(AuditLog.action == "incident_rerouted")).scalar_one()
        changes = OwnershipService(db, settings).changes(entity="screen", limit=1)
        assert changes[0]["action"] == "owner_change" and changes[0]["actor"] == "yonetici"


def test_unowned_screen_notifies_branch_ops_and_platform(db: Database, settings: Settings) -> None:
    seed(OwnershipService(db, settings))
    # Unknown application and screen (and no free-text application that names a registered one).
    draft = {k: v for k, v in DRAFT.items() if k != "application"}
    draft["app_context"] = {**APP_CONTEXT, "app_id": "kredi", "screen_id": "basvuru"}

    draft["title"] = "EFT hatası, müşteri 0532 123 45 67 bekliyor"

    async def report(client: Client[Any]) -> dict[str, Any]:
        out = dict((await client.call_tool("report_incident", {"draft": draft})).structured_content or {})
        # Update while the report is still in the inbox (raw, not yet merged by the worker).
        await client.call_tool(
            "add_incident_update",
            {"incident_key": out["incident_key"], "text": "Müşteri ayse@example.com tekrar aradı"},
        )
        return out

    result = run_as(db, "ali", BRANCH_OPS, report, settings)
    payloads = json.dumps([r.payload for r in outbox_rows(db)], ensure_ascii=False)
    assert "0532 123 45 67" not in payloads and "ayse@example.com" not in payloads
    assert any(r.event == "update_added" for r in outbox_rows(db)), [
        (r.event, r.recipient) for r in outbox_rows(db)
    ]
    routing = result["extra"]["routing"]
    assert routing["owner_team"] == "branch-ops" and routing["unowned_screen"] is True
    events = {(r.event, r.recipient) for r in outbox_rows(db)}
    assert ("ownership_unresolved", "subeops@bank.internal") in events
    assert ("ownership_unresolved", "platform@bank.internal") in events
    with db.session() as s:
        from branchops.application.queries.routing import routing_analytics, unowned_screens

        unowned = unowned_screens(s)
        assert [(u["application_key"], u["screen_key"]) for u in unowned] == [("kredi", "basvuru")]
        analytics = routing_analytics(s)
        assert analytics["routed_incidents"] == 1 and analytics["unowned_screen_rate"] == 1.0

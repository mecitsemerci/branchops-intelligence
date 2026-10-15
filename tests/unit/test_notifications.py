"""Notification engine: rendering, working hours, idempotent enqueue, SMTP and Zoom adapters."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from branchops.application.commands.notifications import (
    IncidentFacts,
    NotificationComposer,
    NotificationService,
    Recipient,
    recipients_for,
)
from branchops.application.interfaces.delivery import PermanentDeliveryError
from branchops.application.interfaces.notifications import NotificationMessage
from branchops.domain.entities.ownership import OwnershipRegistry, Team, TeamKind
from branchops.infrastructure.notifications import build_channels
from branchops.infrastructure.notifications.smtp import SmtpEmailChannel
from branchops.infrastructure.notifications.zoom import ZoomTeamChatChannel
from tests.helpers import make_settings

OPS = Team(
    1,
    "branch-ops",
    "Şube Operasyon",
    TeamKind.BRANCH_OPS,
    email="subeops@bank.internal",
    zoom_channel_id="ops-chan",
    zoom_webhook_secret_ref="ZOOM_OPS",
    working_hours="Mon-Fri 08:30-17:30",
    oncall_contact="Nöbetçi 0212 000 00 00",
)
EFT = Team(2, "eft-team", "EFT Ekibi", email="eft@bank.internal")
NO_CHANNEL = Team(3, "silent", "Kanalsız")
REGISTRY = OwnershipRegistry(teams=(OPS, EFT, NO_CHANNEL))
FACTS = IncidentFacts(
    incident_key="app:RPT-1",
    summary="EFT gönderimi zaman aşımı",
    description_excerpt="Müşteri işlemi tamamlanamadı",
    application="EFT Uygulaması",
    screen="Yeni EFT",
    error_code="EFT-504",
    missing_fields=["Tekrarlama adımları"],
    attachment_count=1,
)
THURSDAY_NOON = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)  # 12:00 Istanbul
THURSDAY_NIGHT = datetime(2026, 10, 15, 20, 0, tzinfo=UTC)  # 23:00 Istanbul


class FakeOutboxSession:
    """Collects enqueue calls through OutboxRepository.enqueue's session.execute()."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def execute(self, stmt: Any) -> Any:
        params = stmt.compile().params
        key = params["idempotency_key"]
        created = key not in self.rows
        if created:
            self.rows[key] = params
        new_id = params["id"] if created else None

        class R:
            def scalar_one_or_none(self) -> Any:
                return new_id

        return R()


def service(**settings: Any) -> NotificationService:
    return NotificationService(
        make_settings(dashboard={"public_url": "https://branchops.apps.bank.internal"}, **settings)
    )


class TestEnqueue:
    def test_rows_per_team_and_channel_are_idempotent(self) -> None:
        svc, s = service(), FakeOutboxSession()
        recipients = recipients_for(REGISTRY, [EFT.id], [OPS.id, NO_CHANNEL.id])
        kwargs: dict[str, Any] = {
            "event": "incident_reported",
            "facts": FACTS,
            "recipients": recipients,
            "channels": ("email", "zoom"),
            "registry": REGISTRY,
            "owner_team_id": EFT.id,
            "now": THURSDAY_NOON,
            "extra": {"matched_level_tr": "ekran (anahtar)"},
        }
        assert svc.enqueue(s, **kwargs) == 3  # EFT e-mail, ops e-mail + zoom; no channel -> nothing
        assert svc.enqueue(s, **kwargs) == 0  # same event again: no duplicates
        assert svc.enqueue(s, **{**kwargs, "suffix": ":reroute:1"}) == 3
        rows = list(s.rows.values())[:3]
        assert [(r["channel"], r["recipient"], r["role"]) for r in rows] == [
            ("email", "eft@bank.internal", "to"),
            ("email", "subeops@bank.internal", "cc"),
            ("zoom", "ops-chan", "cc"),
        ]
        email = rows[0]["payload"]
        assert email["to"] == ["eft@bank.internal"] and email["cc"] == ["subeops@bank.internal"]
        assert email["subject"].startswith("[BranchOps] Yeni bildirim app:RPT-1")
        assert "https://branchops.apps.bank.internal/issues/app:RPT-1" in email["text"]
        assert "Sahip ekip: EFT Ekibi" in email["text"] and "Bilgi (CC): Şube Operasyon" in email["text"]
        zoom = rows[2]["payload"]
        card = json.dumps(zoom["card"], ensure_ascii=False)
        assert zoom["html"] is None and zoom["secret_ref"] == "ZOOM_OPS"
        assert "Müşteri işlemi" not in card  # no description on chat cards
        assert "Ekler: 1 dosya" in card and "Eksik bilgiler: Tekrarlama adımları" in card

    def test_off_hours_marks_and_adds_oncall(self) -> None:
        svc, s = service(), FakeOutboxSession()
        svc.enqueue(
            s,
            event="update_added",
            facts=FactsWith(update_text="Düzeldi", update_author="ali"),
            recipients=[Recipient(OPS, "cc")],
            channels=("email",),
            registry=REGISTRY,
            now=THURSDAY_NIGHT,
        )
        payload = next(iter(s.rows.values()))["payload"]
        assert payload["subject"].startswith("[Mesai dışı] ") and payload["off_hours"] is True
        assert "Nöbetçi 0212 000 00 00" in payload["text"] and "Nöbetçi 0212 000 00 00" in payload["html"]
        assert "Güncelleme (ali): Düzeldi" in payload["text"]

    def test_disabled_notifications_enqueue_nothing(self) -> None:
        svc, s = service(notifications={"enabled": False}), FakeOutboxSession()
        assert (
            svc.enqueue(
                s,
                event="test",
                facts=FACTS,
                recipients=[Recipient(OPS, "to")],
                channels=("email",),
                registry=REGISTRY,
            )
            == 0
        )

    def test_html_is_escaped(self) -> None:
        composer = NotificationComposer(make_settings())
        ctx = {
            **FACTS.__dict__,
            "summary": "<script>alert(1)</script>",
            "owner": None,
            "cc_teams": [],
            "matched_level_tr": "",
            "link": None,
            "off_hours": False,
            "oncall_contact": None,
            "recipient_role_tr": "",
            "team_name": "x",
            "event": "incident_reported",
        }
        _, text, html = composer.render("incident_reported", ctx)
        assert "<script>" not in html and "&lt;script&gt;" in html and "<script>" in text


def FactsWith(**changes: Any) -> IncidentFacts:  # noqa: N802 - reads like a constructor in the tests
    return IncidentFacts(**{**FACTS.__dict__, **changes})


MESSAGE = NotificationMessage(
    event="incident_reported",
    channel="email",
    recipient="eft@bank.internal",
    subject="Konu",
    text="metin",
    html="<p>metin</p>",
    to=["eft@bank.internal"],
    cc=["subeops@bank.internal"],
    incident_key="app:RPT-1",
)


class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host, self.port = host, port
        self.calls: list[Any] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self, context: Any) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user))

    def send_message(self, msg: Any, from_addr: str, to_addrs: list[str]) -> dict[str, Any]:
        self.calls.append(("send", msg, from_addr, to_addrs))
        return {}


class TestChannels:
    def test_smtp_sends_to_the_recipient_only_with_display_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SMTP_USER", "branchops")
        monkeypatch.setenv("SMTP_PASSWORD", "pw")
        settings = make_settings(
            notifications={
                "email": {
                    "backend": "smtp",
                    "smtp_host": "smtp-relay.bank.internal",
                    "username_env": "SMTP_USER",
                    "password_env": "SMTP_PASSWORD",
                }
            }
        )
        channel = SmtpEmailChannel(settings.notifications.email, settings.network, smtp_factory=FakeSMTP)
        channel.send(MESSAGE)
        smtp = FakeSMTP.instances[-1]
        assert smtp.calls[0] == "starttls" and smtp.calls[1] == ("login", "branchops")
        _, msg, sender, rcpts = smtp.calls[2]
        assert rcpts == ["eft@bank.internal"] and sender == "branchops@bank.internal"
        assert msg["Cc"] == "subeops@bank.internal" and msg["X-BranchOps-Incident"] == "app:RPT-1"
        assert msg.get_body(("html",)) is not None

    def test_smtp_host_must_be_internal(self) -> None:
        settings = make_settings(notifications={"email": {"backend": "smtp", "smtp_host": "smtp.gmail.com"}})
        with pytest.raises(ValueError, match="not in network"):
            SmtpEmailChannel(settings.notifications.email, settings.network)

    def test_zoom_posts_the_card_with_the_team_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ZOOM_OPS", "verification-token")
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200)

        settings = make_settings(
            notifications={
                "zoom": {"backend": "webhook", "webhook_base_url": "https://zoom-egress.bank.internal/hooks"}
            }
        )
        channel = ZoomTeamChatChannel(
            settings.notifications.zoom, settings.network, transport=httpx.MockTransport(handler)
        )
        card = {"head": {"text": "Konu"}, "body": [{"type": "message", "text": "Kayıt: app:RPT-1"}]}
        message = NotificationMessage(
            event="incident_reported",
            channel="zoom",
            recipient="ops chan",
            subject="Konu",
            text="t",
            card=card,
            secret_ref="ZOOM_OPS",
        )
        channel.send(message)
        assert str(seen[0].url) == "https://zoom-egress.bank.internal/hooks/ops%20chan?format=full"
        assert seen[0].headers["authorization"] == "verification-token"
        assert json.loads(seen[0].content) == card
        with pytest.raises(PermanentDeliveryError, match="token missing"):
            channel.send(
                NotificationMessage(
                    event="x", channel="zoom", recipient="c", subject="s", text="t", secret_ref="ZOOM_UNKNOWN"
                )
            )
        public = make_settings(
            notifications={
                "zoom": {"backend": "webhook", "webhook_base_url": "https://integrations.zoom.us/chat"}
            }
        )
        with pytest.raises(ValueError, match="not in network"):
            ZoomTeamChatChannel(public.notifications.zoom, public.network)

    def test_dev_defaults_are_log_channels(self) -> None:
        channels = build_channels(make_settings())
        assert {k: type(v).__name__ for k, v in channels.items()} == {
            "email": "LogChannel",
            "zoom": "LogChannel",
        }
        assert channels["email"].send(MESSAGE) == {"delivered": "log"}


def test_every_text_fact_is_masked_before_rendering() -> None:
    svc, s = service(), FakeOutboxSession()
    leaky = FactsWith(
        application="EFT 0532 123 45 67",
        screen="Müşteri ayse@example.com",
        error_code="TR330006100519786457841326",
        route="/m/10000000146",
    )
    svc.enqueue(
        s,
        event="incident_reported",
        facts=leaky,
        recipients=[Recipient(EFT, "to")],
        channels=("email",),
        registry=REGISTRY,
        extra={"matched_level_tr": ""},
    )
    payload = json.dumps(next(iter(s.rows.values()))["payload"], ensure_ascii=False)
    for secret in ("0532 123 45 67", "ayse@example.com", "TR3300061005", "10000000146"):
        assert secret not in payload

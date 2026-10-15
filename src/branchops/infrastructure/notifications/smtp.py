"""E-mail through the bank's internal SMTP relay (STARTTLS with the internal CA, optional AUTH)."""

from __future__ import annotations

import os
import smtplib
import ssl
from collections.abc import Callable
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any

from branchops.application.interfaces.delivery import PermanentDeliveryError
from branchops.application.interfaces.notifications import NotificationMessage
from branchops.config.settings import EmailChannelSettings, NetworkSettings
from branchops.infrastructure.security.network_guard import assert_internal_host

SmtpFactory = Callable[..., Any]


class SmtpEmailChannel:
    name = "email"

    def __init__(
        self,
        settings: EmailChannelSettings,
        network: NetworkSettings,
        *,
        smtp_factory: SmtpFactory = smtplib.SMTP,
    ) -> None:
        if not settings.smtp_host:
            raise ValueError("notifications.email.smtp_host is required for the smtp backend")
        assert_internal_host(settings.smtp_host, network, purpose="notifications.email.smtp_host")
        self._settings = settings
        self._factory = smtp_factory

    def build(self, message: NotificationMessage) -> EmailMessage:
        cfg = self._settings
        msg = EmailMessage()
        msg["From"] = str(Address(display_name=cfg.from_name, addr_spec=cfg.from_address))
        msg["To"] = ", ".join(message.to or [message.recipient])
        if message.cc:
            msg["Cc"] = ", ".join(message.cc)
        msg["Subject"] = message.subject
        msg["Message-ID"] = make_msgid(domain=cfg.from_address.rsplit("@", 1)[-1])
        if message.incident_key:
            msg["X-BranchOps-Incident"] = message.incident_key
        msg["X-BranchOps-Event"] = message.event
        msg.set_content(message.text)
        if message.html:
            msg.add_alternative(message.html, subtype="html")
        return msg

    def send(self, message: NotificationMessage) -> dict[str, Any] | None:
        cfg = self._settings
        if "@" not in message.recipient:
            raise PermanentDeliveryError(f"invalid e-mail recipient {message.recipient!r}")
        msg = self.build(message)
        with self._factory(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout_seconds) as smtp:
            if cfg.starttls:
                smtp.starttls(context=ssl.create_default_context(cafile=cfg.ca_bundle))
            user = os.environ.get(cfg.username_env) if cfg.username_env else None
            password = os.environ.get(cfg.password_env) if cfg.password_env else None
            if user and password:
                smtp.login(user, password)
            refused = smtp.send_message(msg, from_addr=cfg.from_address, to_addrs=[message.recipient])
        if refused:
            raise PermanentDeliveryError(f"recipient refused: {sorted(refused)}")
        return {"message_id": msg["Message-ID"]}

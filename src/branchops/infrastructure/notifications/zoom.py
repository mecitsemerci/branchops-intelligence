"""Zoom Team Chat incoming webhook (``format=full`` message card), via the bank's egress proxy."""

from __future__ import annotations

import os
import ssl
from typing import Any
from urllib.parse import quote

import httpx

from branchops.application.interfaces.delivery import PermanentDeliveryError
from branchops.application.interfaces.notifications import NotificationMessage
from branchops.config.settings import NetworkSettings, ZoomChannelSettings
from branchops.infrastructure.security.network_guard import assert_internal_url


class ZoomTeamChatChannel:
    name = "zoom"

    def __init__(
        self,
        settings: ZoomChannelSettings,
        network: NetworkSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not settings.webhook_base_url:
            raise ValueError("notifications.zoom.webhook_base_url is required for the webhook backend")
        assert_internal_url(settings.webhook_base_url, network, purpose="notifications.zoom.webhook_base_url")
        self._base = settings.webhook_base_url.rstrip("/")
        verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=settings.ca_bundle) if settings.ca_bundle else True
        )
        self._client = httpx.Client(
            timeout=settings.timeout_seconds, verify=verify, transport=transport, follow_redirects=False
        )

    def send(self, message: NotificationMessage) -> dict[str, Any] | None:
        token = os.environ.get(message.secret_ref) if message.secret_ref else None
        if not token:
            raise PermanentDeliveryError(f"zoom token missing (environment variable {message.secret_ref!r})")
        response = self._client.post(
            f"{self._base}/{quote(message.recipient, safe='')}",
            params={"format": "full"},
            headers={"Authorization": token},
            json=message.card
            or {"head": {"text": message.subject}, "body": [{"type": "message", "text": message.text}]},
        )
        if response.status_code >= 500 or response.status_code in (408, 429):
            response.raise_for_status()
        if response.status_code >= 400:
            raise PermanentDeliveryError(f"zoom rejected the message: HTTP {response.status_code}")
        return {"status": response.status_code}

"""Jira REST v2 ticketing sink (internal Jira only; bearer personal access token)."""

from __future__ import annotations

import ssl

import httpx

from branchops.application.interfaces.delivery import PermanentDeliveryError
from branchops.application.interfaces.ticketing import TicketRef, TicketRequest
from branchops.config.settings import NetworkSettings, TicketingSettings
from branchops.infrastructure.security.network_guard import assert_internal_url


class JiraTicketingSink:
    name = "jira"

    def __init__(
        self,
        settings: TicketingSettings,
        network: NetworkSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not settings.base_url:
            raise ValueError("ticketing.base_url is required for the jira sink")
        assert_internal_url(settings.base_url, network, purpose="ticketing.base_url")
        self._base = settings.base_url.rstrip("/")
        token = settings.token()
        verify: ssl.SSLContext | bool = True
        if settings.ca_bundle:
            verify = ssl.create_default_context(cafile=settings.ca_bundle)
        self._client = httpx.Client(
            base_url=self._base,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=settings.timeout_seconds,
            verify=verify,
            transport=transport,
            follow_redirects=False,
        )
        self._issue_type = settings.issue_type

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        detail = response.text[:300]
        if response.status_code in (408, 409, 425, 429) or response.status_code >= 500:
            response.raise_for_status()  # retried by the outbox drainer
        raise PermanentDeliveryError(f"jira rejected the request: HTTP {response.status_code} {detail}")

    def create_ticket(self, request: TicketRequest) -> TicketRef:
        response = self._client.post(
            "/rest/api/2/issue",
            json={
                "fields": {
                    "project": {"key": request.project_key},
                    "summary": request.summary[:250],
                    "description": request.description,
                    "issuetype": {"name": self._issue_type},
                    "labels": request.labels,
                }
            },
        )
        self._check(response)
        key = str(response.json()["key"])
        return TicketRef(key=key, url=f"{self._base}/browse/{key}")

    def add_comment(self, ticket_key: str, body: str) -> None:
        self._check(self._client.post(f"/rest/api/2/issue/{ticket_key}/comment", json={"body": body}))

    def add_attachment(self, ticket_key: str, filename: str, content_type: str, data: bytes) -> None:
        self._check(
            self._client.post(
                f"/rest/api/2/issue/{ticket_key}/attachments",
                headers={"X-Atlassian-Token": "no-check"},
                files={"file": (filename, data, content_type)},
            )
        )

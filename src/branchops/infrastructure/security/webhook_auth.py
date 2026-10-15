"""Webhook authentication for push sources (Jira, ServiceNow, ...).

Supported mechanisms (configurable, several secrets allowed for rotation):

* ``hmac``  — ``X-Hub-Signature: sha256=<hex>`` HMAC of the raw body (Jira Data Center 10+/Cloud webhook
  secret).
* ``token`` — shared token in a header (``X-BranchOps-Webhook-Token``) or, only when explicitly enabled
  for older Jira versions that cannot send custom headers, a ``token`` query parameter of the registered
  webhook URL. Query tokens come from a separate secret list (never usable as HMAC keys) and are
  redacted from logs.

Optionally the source address must be inside ``allowed_source_cidrs``.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from collections.abc import Mapping
from typing import Protocol

from branchops.application.interfaces.source_adapter import SourceAuthError


class WebhookAuthConfig(Protocol):
    """Satisfied by ``SourceAuthSettings`` and the legacy ``WebhookSettings``."""

    @property
    def auth_mode(self) -> str: ...

    @property
    def token_header(self) -> str: ...

    @property
    def allow_query_token(self) -> bool: ...

    @property
    def allowed_source_cidrs(self) -> list[str]: ...


class WebhookAuthError(SourceAuthError):
    pass


def _compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def verify_hmac(body: bytes, header_value: str | None, secrets: list[str]) -> bool:
    if not header_value or "=" not in header_value:
        return False
    algorithm, _, signature = header_value.partition("=")
    if algorithm.lower() != "sha256":
        return False
    for secret in secrets:
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if _compare(expected, signature.strip().lower()):
            return True
    return False


def verify_token(provided: str | None, secrets: list[str]) -> bool:
    if not provided:
        return False
    return any(_compare(provided, s) for s in secrets)


def authenticate_webhook(
    *,
    body: bytes,
    headers: Mapping[str, str],
    query: Mapping[str, str],
    client_host: str | None,
    settings: WebhookAuthConfig,
    secrets: list[str],
    query_tokens: list[str] | None = None,
) -> str:
    """Return the mechanism that authenticated the request, or raise ``WebhookAuthError``."""
    query_tokens = query_tokens or []
    if not secrets and not (settings.allow_query_token and query_tokens):
        raise WebhookAuthError("webhook secrets are not configured")
    if settings.allowed_source_cidrs:
        try:
            addr = ipaddress.ip_address(client_host or "")
        except ValueError as exc:
            raise WebhookAuthError("unknown source address") from exc
        if not any(addr in ipaddress.ip_network(c, strict=False) for c in settings.allowed_source_cidrs):
            raise WebhookAuthError("source address not allowed")

    lower = {k.lower(): v for k, v in headers.items()}
    if settings.auth_mode in ("hmac", "hmac_or_token") and verify_hmac(
        body, lower.get("x-hub-signature"), secrets
    ):
        return "hmac"
    if settings.auth_mode in ("token", "hmac_or_token"):
        if verify_token(lower.get(settings.token_header.lower()), secrets):
            return "token-header"
        if settings.allow_query_token and verify_token(query.get("token"), query_tokens):
            return "token-query"
    raise WebhookAuthError("invalid webhook credentials")


def sign_body(body: bytes, secret: str) -> str:
    """Helper for tests and for operators verifying their webhook configuration."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

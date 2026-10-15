"""Sanitising the in-app ``app_context`` before it is stored, and deriving the routing input from it."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from branchops.domain.entities.ownership import RoutingInput
from branchops.domain.entities.report import AppContext
from branchops.domain.services.masking import SensitiveDataMasker
from branchops.domain.services.prompt_injection import detect_prompt_injection


def strip_query(path: str) -> str:
    """Query strings and fragments often carry identifiers (customer numbers, tokens): drop them."""
    return path.split("?", 1)[0].split("#", 1)[0]


def pseudonymise(username: str, key: bytes) -> str:
    """Stable, non-reversible reference to the host-application user (the reporter is known via SSO).

    Keyed (HMAC-SHA256) so that a list of bank usernames cannot be hashed to re-identify the value.
    """
    return "u-" + hmac.new(key, username.strip().casefold().encode(), hashlib.sha256).hexdigest()[:16]


@dataclass(frozen=True)
class SanitizedContext:
    context: dict[str, Any]
    security_flags: list[str]


def sanitize_app_context(
    ctx: AppContext, masker: SensitiveDataMasker, pseudonym_key: bytes
) -> SanitizedContext:
    """Every free-text value is PII-masked and scanned for prompt injection; URLs lose their query strings;
    the host-app username is pseudonymised."""
    data = ctx.model_dump(mode="json")
    texts: list[str] = []

    def clean(value: Any, key: str = "") -> Any:
        if isinstance(value, str):
            if key in ("route", "path"):
                value = strip_query(value)
            masked = masker.mask_text(value) or ""
            texts.append(value)
            return masked
        if isinstance(value, dict):
            return {k: clean(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v, key) for v in value]
        return value

    user = data.get("user_context") or {}
    if user.get("username"):
        user["username"] = pseudonymise(str(user["username"]), pseudonym_key)
    cleaned = clean(data)
    return SanitizedContext(
        context={k: v for k, v in cleaned.items() if v not in (None, [], {})},
        security_flags=detect_prompt_injection(texts),
    )


def routing_input(ctx: AppContext | None, branch_code: str | None = None) -> RoutingInput:
    if ctx is None:
        return RoutingInput(branch_code=branch_code)
    return RoutingInput(
        app_key=ctx.app_id,
        screen_key=ctx.screen_id,
        route=strip_query(ctx.route) if ctx.route else None,
        error_code=ctx.error_code,
        branch_code=(ctx.user_context.branch_code if ctx.user_context else None) or branch_code,
    )

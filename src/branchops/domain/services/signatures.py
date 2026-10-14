"""Deterministic error signatures used for duplicate gating and recurrence detection."""

from __future__ import annotations

import re

from branchops.domain.services.evidence import normalize_for_match

_CODE_RE = re.compile(
    r"\b(?:[A-Z]{2,10}[-_]?\d{2,6}|0x[0-9A-Fa-f]{4,8}|HTTP\s?[45]\d{2}|ORA-\d{5})\b",
)


def error_codes(text: str | None) -> list[str]:
    if not text:
        return []
    seen: list[str] = []
    for m in _CODE_RE.finditer(text):
        code = m.group(0).upper().replace(" ", "")
        if code not in seen:
            seen.append(code)
    return seen


def error_signature(application: str | None, error_details: str | None) -> str | None:
    codes = error_codes(error_details)
    if not codes:
        return None
    app = normalize_for_match(application or "unknown")[:80]
    return f"{app}|{codes[0]}"[:200]

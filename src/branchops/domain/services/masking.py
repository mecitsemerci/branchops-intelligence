"""Deterministic masking of customer-identifying data before storage, prompting and display.

Branch incident tickets frequently contain customer data pasted by branch staff (TCKN, IBAN, card
numbers, phone numbers). The model and the dashboard never need these values, so they are replaced with
typed placeholders. Checksums (TCKN algorithm, Luhn) are used to avoid masking ordinary numbers such as
error codes or branch codes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

_TCKN_RE = re.compile(r"(?<!\d)([1-9]\d{10})(?!\d)")
_IBAN_RE = re.compile(r"\bTR\s?\d{2}(?:\s?\d{4}){5}\s?\d{2}\b", re.IGNORECASE)
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+90[\s-]?|0)?5\d{2}[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)")


def is_valid_tckn(digits: str) -> bool:
    if len(digits) != 11 or not digits.isdigit() or digits[0] == "0":
        return False
    d = [int(c) for c in digits]
    tenth = ((d[0] + d[2] + d[4] + d[6] + d[8]) * 7 - (d[1] + d[3] + d[5] + d[7])) % 10
    eleventh = sum(d[:10]) % 10
    return d[9] == tenth and d[10] == eleventh


def luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


@dataclass
class MaskingResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)


class SensitiveDataMasker:
    def __init__(self, extra_patterns: Iterable[tuple[str, str]] = ()) -> None:
        self._extra = [(label, re.compile(pattern)) for label, pattern in extra_patterns]

    def mask(self, text: str | None) -> MaskingResult:
        if not text:
            return MaskingResult(text=text or "")
        counts: dict[str, int] = {}

        def bump(label: str) -> None:
            counts[label] = counts.get(label, 0) + 1

        def iban(m: re.Match[str]) -> str:
            bump("IBAN")
            return "[IBAN]"

        def card(m: re.Match[str]) -> str:
            digits = re.sub(r"\D", "", m.group(0))
            if 13 <= len(digits) <= 19 and luhn_valid(digits):
                bump("CARD")
                return f"[KART-****{digits[-4:]}]"
            return m.group(0)

        def tckn(m: re.Match[str]) -> str:
            if is_valid_tckn(m.group(1)):
                bump("TCKN")
                return "[TCKN]"
            return m.group(0)

        def email(m: re.Match[str]) -> str:
            bump("EMAIL")
            return "[EPOSTA]"

        def phone(m: re.Match[str]) -> str:
            bump("PHONE")
            return "[TELEFON]"

        out = _IBAN_RE.sub(iban, text)
        out = _CARD_RE.sub(card, out)
        out = _TCKN_RE.sub(tckn, out)
        out = _EMAIL_RE.sub(email, out)
        out = _PHONE_RE.sub(phone, out)
        for label, pattern in self._extra:

            def extra(m: re.Match[str], label: str = label) -> str:
                bump(label)
                return f"[{label}]"

            out = pattern.sub(extra, out)
        return MaskingResult(text=out, counts=counts)

    def mask_text(self, text: str | None) -> str | None:
        if text is None:
            return None
        return self.mask(text).text

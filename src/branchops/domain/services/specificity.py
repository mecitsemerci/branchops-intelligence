"""Deterministic specificity check for free-text fields (summary, description).

A text is *specific* when it contains at least one operational anchor that lets a support team act on it:

* a number (branch code, time, count, version, terminal number),
* an error code (``APP-1203``, ``ORA-12345``, ``HTTP 504`` …),
* a quoted message (``'servis kullanılamıyor'``),
* a named device, system or business process from the domain lexicon (``para sayma makinesi``, ``EFT``).

Symptoms alone ("çalışmıyor", "donuyor", "hata var") are not anchors: "Uygulama çalışmıyor." and a padded
paragraph without any anchor are both non-specific, whereas "145 numaralı şubede para yatırma işlemi
onaylandığında DCS Host yanıt vermiyor." is specific. The check only *caps* a model-proposed rubric level (see
the scoring policy); it never raises one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from branchops.domain.services.evidence import normalize_for_match
from branchops.domain.services.signatures import error_codes

SPECIFICITY_VERSION: Final = "2026.10-1"

_DIGIT_RE = re.compile(r"\d")
_QUOTED_RE = re.compile(r"['\"“‘«][^'\"”’»]{4,}['\"”’»]")
# Nouns naming a concrete device, system, channel or business process (lower-case, Turkish letters).
OBJECT_TERMS: Final[tuple[str, ...]] = (
    "para sayma",
    "banknot",
    "kasa cihazı",
    "recycler",
    "atm",
    "kiosk",
    "pos",
    "yazıcı",
    "printer",
    "pasbook",
    "hesap cüzdanı",
    "tarayıcı",
    "scanner",
    "kart okuyucu",
    "imza tableti",
    "terminal",
    "switch",
    "modem",
    "router",
    "vpn",
    "wifi",
    "data hattı",
    "ağ bağlantısı",
    "ldap",
    "active directory",
    "şifre",
    "otp",
    "token",
    "sms",
    "eft",
    "fast",
    "havale",
    "swift",
    "para yatırma",
    "para çekme",
    "nakit",
    "döviz",
    "kredi",
    "kart işlem",
    "hesap açılış",
    "hesap açma",
    "müşteri kaydı",
    "kps",
    "mernis",
    "kkb",
    "ana bankacılık",
    "core banking",
    "host",
    "dcs",
    "gateway",
    "servis",
    "veritabanı",
    "gişe",
    "vezne",
    "dekont",
    "rapor ekranı",
    "portal",
)


def _term_re(term: str) -> re.Pattern[str]:
    # Short acronyms (EFT, POS, KPS) only as a word, optionally with an apostrophe suffix ("EFT'de"), so that
    # "pos" does not match "posta"; longer nouns take any Turkish suffix ("yazıcısı", "terminalde").
    if len(term) <= 4:
        return re.compile(rf"(?<!\w){re.escape(term)}(?:'\w+)?(?!\w)")
    return re.compile(rf"(?<!\w){re.escape(term)}\w*")


_OBJECT_RES: Final = tuple(_term_re(term) for term in OBJECT_TERMS)


@dataclass(frozen=True)
class Specificity:
    anchors: tuple[str, ...]

    @property
    def specific(self) -> bool:
        return bool(self.anchors)


def specificity(text: str | None) -> Specificity:
    """Anchors found in ``text`` (empty tuple = non-specific)."""
    if not text or not text.strip():
        return Specificity(())
    anchors: list[str] = []
    if error_codes(text):
        anchors.append("error_code")
    if _DIGIT_RE.search(text):
        anchors.append("number")
    if _QUOTED_RE.search(text):
        anchors.append("quoted_message")
    norm = normalize_for_match(text)
    if any(r.search(norm) for r in _OBJECT_RES):
        anchors.append("named_object")
    return Specificity(tuple(anchors))

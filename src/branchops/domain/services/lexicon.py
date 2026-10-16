"""Versioned Turkish branch-operations synonym lexicon for candidate retrieval.

PostgreSQL's ``turkish`` text-search configuration stems words but knows no domain synonyms, so "para sayma
makinesi" never meets "banknot sayım cihazı". Retrieval expands the query with the other members of every
synonym group it mentions. The lexicon is data in the code base (reviewable, no download); changing it
requires a new ``LEXICON_VERSION`` because it changes which candidates an analysis sees.

Expansion only widens *candidate retrieval*. Whether two incidents are related is still decided by the
deterministic signals and the verification step, never by a synonym hit alone.
"""

from __future__ import annotations

import re
from typing import Final

from branchops.domain.services.evidence import normalize_for_match

LEXICON_VERSION: Final = "2026.10-1"

# Each group lists interchangeable surface forms (already lower-case, Turkish letters).
SYNONYM_GROUPS: Final[tuple[tuple[str, ...], ...]] = (
    (
        "para sayma makinesi",
        "para sayma cihazı",
        "banknot sayım cihazı",
        "banknot sayma",
        "para sayım cihazı",
    ),
    ("bağlantı kesiliyor", "bağlantı koptu", "bağlantı kopuyor", "iletişim hatası", "iletişim kesildi"),
    ("zaman aşımı", "timeout", "time out", "yanıt süresi aşıldı", "cevap gelmiyor"),
    ("donuyor", "dondu", "kilitleniyor", "takılıyor", "yanıt vermiyor"),
    ("yazıcı", "printer", "dekont yazıcısı"),
    ("tarayıcı", "scanner", "belge tarayıcı"),
    ("pasbook", "hesap cüzdanı yazıcısı"),
    ("kart okuyucu", "card reader", "kart okuma cihazı"),
    ("giriş yapılamıyor", "oturum açılamıyor", "login olunamıyor", "kimlik doğrulama hatası"),
    ("ldap", "active directory", "dizin servisi"),
    ("ağ bağlantısı", "network", "hat", "data hattı"),
    ("ana bankacılık", "core banking", "corebanking"),
    ("eft", "elektronik fon transferi"),
    ("havale", "para transferi"),
    ("vezne", "gişe", "kasa"),
)

_GROUPS_NORMALIZED: Final = tuple(tuple(normalize_for_match(t) for t in g) for g in SYNONYM_GROUPS)


def _contains(text: str, term: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) is not None


def expansions(text: str | None) -> list[str]:
    """Synonyms of every group mentioned in ``text`` that do not already occur in it (stable order)."""
    if not text:
        return []
    norm = normalize_for_match(text)
    out: list[str] = []
    for group in _GROUPS_NORMALIZED:
        if any(_contains(norm, term) for term in group):
            out.extend(t for t in group if not _contains(norm, t) and t not in out)
    return out


def expand_query(text: str | None) -> str:
    """``text`` followed by its synonym expansions (used to build the full-text query)."""
    extra = expansions(text)
    return " ".join([text or "", *extra]).strip()


def concept_ids(text: str | None) -> frozenset[int]:
    """Indexes of the synonym groups mentioned in ``text`` (a cheap, explainable shared-concept signal)."""
    if not text:
        return frozenset()
    norm = normalize_for_match(text)
    return frozenset(
        i for i, group in enumerate(_GROUPS_NORMALIZED) if any(_contains(norm, t) for t in group)
    )

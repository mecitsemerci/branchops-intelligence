"""Verification of model-provided evidence quotes against the source issue text."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping

from branchops.domain.value_objects import EvidenceRef

_WS_RE = re.compile(r"\s+")
_TR_LOWER = str.maketrans({"I": "ı", "İ": "i"})


def normalize_for_match(text: str) -> str:
    """Case-fold (Turkish-aware), NFC-normalise and collapse whitespace and quotes."""
    text = unicodedata.normalize("NFC", text).translate(_TR_LOWER).lower()
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return _WS_RE.sub(" ", text).strip()


def resolve_source(sources: Mapping[str, str], ref_source: str) -> str | None:
    if ref_source in sources:
        return sources[ref_source]
    if ref_source.startswith("comment") and ref_source.lower() == "comments":
        return "\n".join(v for k, v in sources.items() if k.startswith("comment:"))
    return None


MIN_QUOTE_CHARS = 3


def verify_evidence(sources: Mapping[str, str], refs: Iterable[EvidenceRef]) -> list[EvidenceRef]:
    """Return refs with ``verified`` recomputed. If the stated source is wrong but the quote exists in
    another source, the reference is corrected to that source."""
    verified: list[EvidenceRef] = []
    for ref in refs:
        quote = normalize_for_match(ref.quote)
        source_text = resolve_source(sources, ref.source)
        # Very short quotes ("a", "ok") occur almost anywhere and prove nothing (SEC-3).
        if len(quote) < MIN_QUOTE_CHARS:
            verified.append(EvidenceRef(source=ref.source, quote=ref.quote, verified=False))
            continue
        if source_text is not None and quote in normalize_for_match(source_text):
            verified.append(EvidenceRef(source=ref.source, quote=ref.quote, verified=True))
            continue
        found = next(
            (name for name, text in sources.items() if quote and text and quote in normalize_for_match(text)),
            None,
        )
        if found is not None:
            verified.append(EvidenceRef(source=found, quote=ref.quote, verified=True))
        else:
            verified.append(EvidenceRef(source=ref.source, quote=ref.quote, verified=False))
    return verified


def value_occurs_in_sources(sources: Mapping[str, str], value: str) -> bool:
    needle = normalize_for_match(value)
    return bool(needle) and any(needle in normalize_for_match(t) for t in sources.values() if t)

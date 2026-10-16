"""In-memory incident history for offline evaluation.

Implements the workflow's ``HistoricalIssueReader`` port over the cases analysed earlier in the run, so the
similarity step sees the same kind of candidates it would see in production. Ranking approximates the
PostgreSQL ``turkish`` full-text search with a deterministic token-overlap score over stemmed-ish prefixes;
the expansion with the domain lexicon is the same as in production. Absolute ranks therefore differ from
``ts_rank_cd``; retrieval recall measured here is a property of the candidate *logic*, not of PostgreSQL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from branchops.application.interfaces.repositories import CandidateIssue
from branchops.domain.services.evidence import normalize_for_match
from branchops.domain.services.lexicon import expand_query

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_STOP = frozenset(
    (
        "ve",
        "ile",
        "bir",
        "bu",
        "şu",
        "da",
        "de",
        "için",
        "olarak",
        "olan",
        "gibi",
        "çok",
        "daha",
        "ama",
        "fakat",
        "veya",
        "ya",
        "ki",
        "mi",
        "mı",
        "mu",
        "mü",
        "ne",
        "her",
        "tüm",
    )
)


def terms(text: str | None) -> frozenset[str]:
    """Lower-cased word prefixes (5 chars) without stop words: a crude, deterministic Turkish stemmer."""
    if not text:
        return frozenset()
    words = _WORD_RE.findall(normalize_for_match(text))
    return frozenset(w[:5] for w in words if len(w) >= 3 and w not in _STOP)


@dataclass
class EvaluationHistory:
    """History of analysed cases; ``retrieved`` keeps the candidate keys returned per incident."""

    issues: list[CandidateIssue] = field(default_factory=list)
    retrieved: dict[str, list[str]] = field(default_factory=dict)

    def add(self, issue: CandidateIssue) -> None:
        self.issues = [i for i in self.issues if i.incident_key != issue.incident_key] + [issue]

    def find_candidates(
        self,
        incident_key: str,
        query_text: str,
        error_signature: str | None,
        lookback_days: int,
        limit: int,
    ) -> list[CandidateIssue]:
        query = terms(expand_query(query_text))
        newest = max((i.created_at for i in self.issues if i.created_at), default=None)
        scored: list[tuple[float, CandidateIssue]] = []
        for issue in self.issues:
            if issue.incident_key == incident_key:
                continue
            if newest and issue.created_at and newest - issue.created_at > timedelta(days=lookback_days):
                continue
            doc = terms(" ".join(filter(None, [issue.summary, issue.description_excerpt])))
            overlap = len(query & doc)
            rank = overlap / (len(query) + len(doc) - overlap) if query and doc else 0.0
            same_signature = bool(error_signature) and issue.error_signature == error_signature
            if rank > 0 or same_signature:
                scored.append((rank, issue))
        scored.sort(key=lambda item: (-item[0], item[1].incident_key))
        out = [
            CandidateIssue(**{**issue.__dict__, "text_rank": round(rank, 4)})
            for rank, issue in scored[:limit]
        ]
        self.retrieved[incident_key] = [c.incident_key for c in out]
        return out

    def count_signature_occurrences(self, incident_key: str, error_signature: str, window_days: int) -> int:
        return 1 + sum(
            1 for i in self.issues if i.incident_key != incident_key and i.error_signature == error_signature
        )

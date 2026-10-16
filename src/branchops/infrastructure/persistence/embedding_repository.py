"""Optional hybrid candidate retrieval: full-text search (baseline) + internal embeddings.

``HybridHistoryReader`` implements the workflow's ``HistoricalIssueReader`` port. It always runs the lexical
retrieval first; the semantic part only *adds* candidates (marked with ``semantic_score``) and is skipped
silently when the embedding endpoint fails, so analysis never depends on it. Nothing is merged: candidates
still go through the deterministic shortlist and the verification step.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from branchops.application.interfaces.embeddings import EmbeddingProvider, EmbeddingUnavailableError
from branchops.application.interfaces.repositories import CandidateIssue, HistoricalIssueReader
from branchops.config.settings import EmbeddingSettings
from branchops.infrastructure.database.models import Issue, IssueEmbedding
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.observability.telemetry import get_instruments

logger = logging.getLogger(__name__)


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class EmbeddingRepository:
    def upsert(self, session: Session, incident_key: str, model: str, vector: list[float], text: str) -> None:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        stmt = insert(IssueEmbedding).values(
            incident_key=incident_key,
            model=model,
            vector=vector,
            content_sha256=digest,
            updated_at=datetime.now(UTC),
        )
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["incident_key", "model"],
                set_={
                    "vector": stmt.excluded.vector,
                    "content_sha256": digest,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
        )

    def nearest(
        self,
        session: Session,
        *,
        model: str,
        vector: list[float],
        exclude: str,
        lookback_days: int,
        pool: int,
        top_k: int,
        min_score: float,
    ) -> list[tuple[CandidateIssue, float]]:
        since = datetime.now(UTC) - timedelta(days=lookback_days)
        rows = session.execute(
            select(IssueEmbedding.vector, Issue)
            .join(Issue, Issue.incident_key == IssueEmbedding.incident_key)
            .where(
                IssueEmbedding.model == model,
                IssueEmbedding.incident_key != exclude,
                Issue.is_deleted.is_(False),
                IssueEmbedding.updated_at >= since,
            )
            .order_by(IssueEmbedding.updated_at.desc())
            .limit(pool)
        ).all()
        scored = [(cosine(vector, list(v)), issue) for v, issue in rows]
        scored = [(score, issue) for score, issue in scored if score >= min_score]
        scored.sort(key=lambda t: (-t[0], t[1].incident_key))
        return [
            (
                CandidateIssue(
                    incident_key=issue.incident_key,
                    source=issue.source,
                    summary=issue.summary,
                    description_excerpt=(issue.description or "")[:400] or None,
                    category=issue.category,
                    subcategory=issue.subcategory,
                    branch_code=issue.branch_code,
                    application=issue.application,
                    error_signature=issue.error_signature,
                    created_at=issue.source_created_at or issue.created_at,
                    text_rank=0.0,
                    semantic_score=round(score, 4),
                ),
                score,
            )
            for score, issue in scored[:top_k]
        ]


class HybridHistoryReader:
    def __init__(
        self,
        lexical: HistoricalIssueReader,
        db: Database,
        embedder: EmbeddingProvider,
        settings: EmbeddingSettings,
        repo: EmbeddingRepository | None = None,
    ) -> None:
        self._lexical = lexical
        self._db = db
        self._embedder = embedder
        self._settings = settings
        self._repo = repo or EmbeddingRepository()

    def find_candidates(
        self,
        incident_key: str,
        query_text: str,
        error_signature: str | None,
        lookback_days: int,
        limit: int,
    ) -> list[CandidateIssue]:
        lexical = self._lexical.find_candidates(
            incident_key, query_text, error_signature, lookback_days, limit
        )
        if not query_text.strip():
            return lexical
        try:
            vector = self._embedder.embed([query_text])[0]
        except EmbeddingUnavailableError as exc:
            logger.warning("embedding retrieval skipped", extra={"reason": str(exc)})
            get_instruments().model_errors.add(1, {"agent": "embeddings", "kind": "unavailable"})
            return lexical
        model = self._embedder.model_id
        with self._db.session() as s:
            self._repo.upsert(s, incident_key, model, vector, query_text)
            semantic = self._repo.nearest(
                s,
                model=model,
                vector=vector,
                exclude=incident_key,
                lookback_days=lookback_days,
                pool=self._settings.candidate_pool,
                top_k=self._settings.top_k,
                min_score=self._settings.min_score,
            )
        by_key = {c.incident_key: c for c in lexical}
        for candidate, score in semantic:
            base = by_key.get(candidate.incident_key, candidate)
            by_key[candidate.incident_key] = replace(base, semantic_score=round(score, 4))
        return list(by_key.values())

    def count_signature_occurrences(self, incident_key: str, error_signature: str, window_days: int) -> int:
        return self._lexical.count_signature_occurrences(incident_key, error_signature, window_days)

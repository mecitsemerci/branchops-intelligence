"""Hybrid similarity: lexicon, deterministic relationship bounds, evidence, optional embeddings."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from branchops.agents.base import StructuredCaller
from branchops.agents.similarity import (
    IssueFingerprint,
    SimilarityAgent,
    bounded_relationship,
    passes_duplicate_gate,
    shortlist,
)
from branchops.application.interfaces.embeddings import EmbeddingUnavailableError
from branchops.application.interfaces.model_provider import CompletionRequest
from branchops.application.interfaces.repositories import CandidateIssue
from branchops.domain.enums import RelationshipType
from branchops.domain.services.evidence import verify_evidence
from branchops.domain.services.lexicon import concept_ids, expand_query, expansions
from branchops.domain.value_objects import EvidenceRef
from branchops.infrastructure.llm.embeddings import OpenAICompatibleEmbeddingProvider
from branchops.infrastructure.persistence.embedding_repository import HybridHistoryReader, cosine
from branchops.infrastructure.security.network_guard import ExternalEndpointError
from tests.helpers import ScriptedProvider, make_settings

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
WINDOW = timedelta(hours=72)


def fingerprint(**kw: Any) -> IssueFingerprint:
    base: dict[str, Any] = {
        "incident_key": "jira:CUR-1",
        "summary": "EFT gönderiminde zaman aşımı",
        "error_details": "EFT-504",
        "category": "TRANSACTION",
        "subcategory": "EFT_FAST",
        "branch_code": "2201",
        "application": "Şube Bankacılık",
        "error_signature": "şube bankacılık|EFT-504",
        "created_at": T0,
    }
    return IssueFingerprint(**{**base, **kw})


def candidate(**kw: Any) -> CandidateIssue:
    base: dict[str, Any] = {
        "incident_key": "jira:OLD-1",
        "summary": "Rapor ekranı zaman aşımı veriyor",
        "description_excerpt": "Kredi Raporlama uygulamasında RPT-408 zaman aşımı hatası",
        "category": "APPLICATION",
        "subcategory": "PERFORMANCE",
        "branch_code": "2201",
        "application": "Kredi Raporlama",
        "error_signature": "kredi raporlama|RPT-408",
        "created_at": T0 - timedelta(hours=1),
        "text_rank": 0.2,
    }
    return CandidateIssue(**{**base, **kw})


class TestLexicon:
    def test_turkish_synonyms_meet(self) -> None:
        a = "Para sayma makinesi bağlantısı kesiliyor"
        b = "Banknot sayım cihazı iletişim zaman aşımı hatası"
        assert "banknot sayım cihazı" in expansions(a)
        assert concept_ids(a) & concept_ids(b)
        assert expand_query("Uygulama çalışmıyor") == "Uygulama çalışmıyor"


class TestRelationshipBounds:
    def test_timeout_in_unrelated_applications_is_never_a_duplicate(self) -> None:
        """Hard negative: same branch, same hour, both say 'zaman aşımı', different applications."""
        signals = ["same_branch", "within_duplicate_window", "same_subcategory", "shared_concept"]
        assert not passes_duplicate_gate(signals)
        rel, note = bounded_relationship(RelationshipType.DUPLICATE_CANDIDATE, signals)
        assert rel is RelationshipType.POSSIBLE_COMMON_CAUSE and note

    def test_duplicate_needs_application_or_error_signature(self) -> None:
        signals = ["same_branch", "within_duplicate_window", "same_error_signature"]
        assert bounded_relationship(RelationshipType.DUPLICATE_CANDIDATE, signals) == (
            RelationshipType.DUPLICATE_CANDIDATE,
            None,
        )
        rel, _ = bounded_relationship(RelationshipType.DUPLICATE_CANDIDATE, ["same_application"])
        assert rel is RelationshipType.RELATED

    def test_related_without_anchor_is_a_possible_common_cause(self) -> None:
        rel, _ = bounded_relationship(RelationshipType.RELATED, ["same_category", "within_duplicate_window"])
        assert rel is RelationshipType.POSSIBLE_COMMON_CAUSE
        assert bounded_relationship(RelationshipType.UNRELATED, []) == (RelationshipType.UNRELATED, None)

    def test_semantic_match_keeps_a_candidate_without_text_rank(self) -> None:
        sem = candidate(incident_key="jira:SEM-1", text_rank=0.0, semantic_score=0.91)
        weak = candidate(incident_key="jira:WEAK-1", text_rank=0.0)
        out = shortlist(fingerprint(), [sem, weak], min_rank=0.01, top_n=5, duplicate_window=WINDOW)
        assert [s.incident_key for s in out] == ["jira:SEM-1"]
        assert "semantic_match" in out[0].deterministic_signals


class TestEvidence:
    def test_quotes_are_verified_against_current_and_candidate_texts(self) -> None:
        sources = {"summary": "EFT gönderiminde zaman aşımı", "candidate:jira:OLD-1": "RPT-408 zaman aşımı"}
        refs = verify_evidence(
            sources,
            [
                EvidenceRef(source="candidate:jira:OLD-1", quote="RPT-408"),
                EvidenceRef(source="summary", quote="uydurma alıntı"),
                EvidenceRef(source="summary", quote="EF"),  # too short to prove anything
            ],
        )
        assert [r.verified for r in refs] == [True, False, False]

    def test_agent_applies_bounds_and_verifies_quotes(self) -> None:
        def verdict(_: CompletionRequest) -> str:
            return json.dumps(
                {
                    "verdicts": [
                        {
                            "incident_key": "jira:OLD-1",
                            "relationship": "DUPLICATE_CANDIDATE",
                            "rationale": "İkisi de zaman aşımı.",
                            "evidence": [
                                {"source": "candidate:jira:OLD-1", "quote": "RPT-408 zaman aşımı"},
                                {"source": "description", "quote": "uydurma"},
                            ],
                        }
                    ]
                }
            )

        agent = SimilarityAgent(StructuredCaller(ScriptedProvider({"SimilarityOutput": [verdict]})))
        current = fingerprint()
        cand = candidate()
        short = shortlist(current, [cand], min_rank=0.01, top_n=5, duplicate_window=WINDOW)
        verified, _ = agent.verify(
            current=current,
            shortlisted=short,
            candidates={cand.incident_key: cand},
            correlation_id="c",
            sources={"summary": current.summary or ""},
        )
        assert verified[0].relationship is RelationshipType.POSSIBLE_COMMON_CAUSE
        assert "duplicate gate not met" in (verified[0].rationale or "")
        assert [e.verified for e in verified[0].evidence] == [True, False]


class _Lexical:
    def __init__(self, items: list[CandidateIssue]) -> None:
        self.items = items

    def find_candidates(self, *args: Any) -> list[CandidateIssue]:
        return list(self.items)

    def count_signature_occurrences(self, *args: Any) -> int:
        return 1


class _Embedder:
    model_id = "bge-m3-internal"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise EmbeddingUnavailableError("down")
        return [[1.0, 0.0]] * len(texts)


class _Repo:
    def __init__(self, hits: list[tuple[CandidateIssue, float]]) -> None:
        self.hits = hits
        self.stored: list[str] = []

    def upsert(self, session: Any, incident_key: str, model: str, vector: list[float], text: str) -> None:
        self.stored.append(incident_key)

    def nearest(self, session: Any, **kw: Any) -> list[tuple[CandidateIssue, float]]:
        return self.hits


class _DB:
    def session(self) -> Any:
        from contextlib import nullcontext

        return nullcontext(None)


class TestHybridRetrieval:
    def test_semantic_hits_are_added_and_scored(self) -> None:
        lexical = candidate(incident_key="jira:LEX-1")
        semantic_only = candidate(incident_key="jira:SEM-1", text_rank=0.0)
        repo = _Repo([(semantic_only, 0.93), (lexical, 0.81)])
        reader = HybridHistoryReader(
            _Lexical([lexical]),
            _DB(),
            _Embedder(),
            make_settings().workflow.similarity.embeddings,
            repo,  # type: ignore[arg-type]
        )
        out = {c.incident_key: c for c in reader.find_candidates("jira:CUR-1", "para sayma", None, 180, 20)}
        assert out["jira:SEM-1"].semantic_score == 0.93 and out["jira:LEX-1"].semantic_score == 0.81
        assert repo.stored == ["jira:CUR-1"]

    def test_embedding_outage_falls_back_to_full_text(self) -> None:
        lexical = candidate(incident_key="jira:LEX-1")
        reader = HybridHistoryReader(
            _Lexical([lexical]),
            _DB(),
            _Embedder(fail=True),
            make_settings().workflow.similarity.embeddings,  # type: ignore[arg-type]
            _Repo([]),  # type: ignore[arg-type]
        )
        assert [c.incident_key for c in reader.find_candidates("jira:CUR-1", "x", None, 180, 20)] == [
            "jira:LEX-1"
        ]

    def test_cosine(self) -> None:
        assert cosine([1, 0], [1, 0]) == 1.0 and cosine([1, 0], [0, 1]) == 0.0 and cosine([], []) == 0.0


class TestEmbeddingProvider:
    def _settings(self, **emb: Any) -> Any:
        base = {
            "enabled": True,
            "base_url": "https://embeddings.bank.internal/v1",
            "model": "bge-m3-internal",
        }
        return make_settings(workflow={"similarity": {"embeddings": {**base, **emb}}})

    def test_parses_vectors_in_index_order(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200, json={"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]}
            )

        s = self._settings()
        provider = OpenAICompatibleEmbeddingProvider(
            s.workflow.similarity.embeddings, s.network, transport=httpx.MockTransport(handler)
        )
        assert provider.embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
        assert json.loads(seen[0].content)["model"] == "bge-m3-internal"

    def test_errors_are_unavailable_not_crashes(self) -> None:
        s = self._settings()
        provider = OpenAICompatibleEmbeddingProvider(
            s.workflow.similarity.embeddings,
            s.network,
            transport=httpx.MockTransport(lambda r: httpx.Response(503)),
        )
        with pytest.raises(EmbeddingUnavailableError):
            provider.embed(["a"])

    def test_public_endpoints_and_the_generative_model_are_refused(self) -> None:
        with pytest.raises(ExternalEndpointError):
            s = self._settings(base_url="https://api.openai.com/v1")
            OpenAICompatibleEmbeddingProvider(s.workflow.similarity.embeddings, s.network)
        with pytest.raises(ValueError, match="not the generative model"):
            self._settings(model="GLM-5.3-Flash")

"""Test doubles and fixtures shared across test modules."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from branchops.application.interfaces.model_provider import (
    CompletionRequest,
    CompletionResult,
    ModelHealth,
    ModelUnavailableError,
    OutputMode,
    TokenUsage,
    UnsupportedOutputModeError,
)
from branchops.application.interfaces.repositories import CandidateIssue
from branchops.config.settings import ModelCapabilities, Settings
from branchops.domain.entities.analysis import AnalysisResult
from branchops.infrastructure.llm.mock_provider import MockModelProvider

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples" / "jira"
SAMPLES_ROOT = ROOT / "samples"
TEMPLATES = ROOT / "templates"

Responder = Callable[[CompletionRequest], str | Exception]


def load_sample(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def make_settings(**overrides: Any) -> Settings:
    data: dict[str, Any] = {
        "app": {"environment": "test"},
        "llm": {"provider": "mock"},
        "templates": {"directory": str(TEMPLATES)},
        "jira": {"field_mapping": {"branch_code": "customfield_10101", "application": "customfield_10102"}},
        "workflow": {"checkpointer": "memory"},
        "worker": {"backoff_base_seconds": 0.01, "backoff_max_seconds": 0.05, "max_attempts": 3},
        # The sample payloads carry fixed 2026 timestamps: keep the replay window wide enough that they never
        # age out (the window itself is tested in tests/integration/test_ingestion_reliability.py).
        "webhook": {"max_event_age_days": 36_500},
        "retention": {"inbox_days": 36_501},
    }
    for key, value in overrides.items():
        if isinstance(value, list):  # e.g. sources=[...]
            data[key] = value
        else:
            data.setdefault(key, {}).update(value)
    return Settings.model_validate(data)


class ScriptedProvider:
    """Provider whose answers are produced by per-schema responders; falls back to the mock provider."""

    def __init__(
        self,
        responders: dict[str, list[Responder]] | None = None,
        capabilities: ModelCapabilities | None = None,
        reject_modes: set[OutputMode] | None = None,
    ) -> None:
        self._responders = {k: list(v) for k, v in (responders or {}).items()}
        self._mock = MockModelProvider()
        self._caps = capabilities or ModelCapabilities(structured_outputs=True, json_mode=True)
        self._reject = reject_modes or set()
        self.requests: list[CompletionRequest] = []

    @property
    def model_id(self) -> str:
        return "scripted-model"

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._caps

    def health(self) -> ModelHealth:
        return ModelHealth(True, self.model_id, "ok")

    def calls_for(self, schema: str) -> list[CompletionRequest]:
        return [r for r in self.requests if r.schema_name == schema]

    def complete(self, request: CompletionRequest, *, timeout: float | None = None) -> CompletionResult:
        self.requests.append(request)
        if request.output_mode in self._reject:
            raise UnsupportedOutputModeError(f"response_format {request.output_mode} not supported")
        queue = self._responders.get(request.schema_name or "")
        if queue:
            answer = queue.pop(0)(request)
            if isinstance(answer, Exception):
                raise answer
            return CompletionResult(
                content=answer,
                model_id=self.model_id,
                output_mode=request.output_mode,
                usage=TokenUsage(10, 5),
                latency_ms=3,
            )
        result = self._mock.complete(request)
        return CompletionResult(
            content=result.content,
            model_id=self.model_id,
            output_mode=request.output_mode,
            usage=result.usage,
            latency_ms=1,
        )


def unavailable(_req: CompletionRequest) -> Exception:
    return ModelUnavailableError("connection refused")


class InMemoryHistory:
    def __init__(self, candidates: list[CandidateIssue] | None = None, signature_count: int = 1) -> None:
        self.candidates = candidates or []
        self.signature_count = signature_count

    def find_candidates(
        self, incident_key: str, query_text: str, error_signature: str | None, lookback_days: int, limit: int
    ) -> list[CandidateIssue]:
        return [c for c in self.candidates if c.incident_key != incident_key][:limit]

    def count_signature_occurrences(self, incident_key: str, error_signature: str, window_days: int) -> int:
        return self.signature_count


class MemorySink:
    def __init__(self) -> None:
        self.results: list[AnalysisResult] = []

    def save(self, result: AnalysisResult) -> None:
        self.results.append(result)

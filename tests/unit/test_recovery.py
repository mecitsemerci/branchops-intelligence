"""Model recovery: error classification, backoff, latest-analysis rule (no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from branchops.application.interfaces.model_provider import (
    ModelAccessError,
    ModelRequestError,
    ModelUnavailableError,
)
from branchops.config.settings import RecoverySettings
from branchops.domain.entities.analysis import AnalysisResult
from branchops.domain.enums import AnalysisStatus
from branchops.infrastructure.persistence.analysis_repository import replaces_latest
from branchops.infrastructure.persistence.recovery_repository import recovery_delay
from branchops.workers.recovery import ModelHealthProbe
from tests.unit.test_openai_provider import make_provider, request

T0 = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize("status", [401, 403, 404])
def test_access_errors_are_retried_like_outages(status: int) -> None:
    """REC-3: a rotated key or renamed model must not degrade every analysis for good."""
    provider, _ = make_provider(lambda r: httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(ModelAccessError) as exc:
        provider.complete(request())
    assert isinstance(exc.value, ModelUnavailableError) and isinstance(exc.value, ModelRequestError)


def test_backoff_is_exponential_jittered_and_capped() -> None:
    cfg = RecoverySettings(base_delay_seconds=300, max_delay_seconds=3600)
    assert recovery_delay(0, cfg, rand=lambda: 1.0) == 300
    assert recovery_delay(2, cfg, rand=lambda: 1.0) == 1200
    assert recovery_delay(10, cfg, rand=lambda: 1.0) == 3600
    assert recovery_delay(0, cfg, rand=lambda: 0.0) == 225  # jitter floor 75 %


def _result(*, recoverable: bool, started: datetime, updated: datetime | None) -> AnalysisResult:
    return AnalysisResult(
        analysis_id="00000000-0000-0000-0000-000000000001",
        incident_key="jira:A-1",
        correlation_id="c",
        input_hash="h",
        prompt_version="p",
        started_at=started,
        status=AnalysisStatus.NEEDS_REVIEW if recoverable else AnalysisStatus.COMPLETED,
        recoverable=recoverable,
        source_updated_at=updated,
    )


def _latest(*, recoverable: bool, started: datetime, updated: datetime | None) -> Any:
    return SimpleNamespace(recoverable=recoverable, started_at=started, source_updated_at=updated)


class TestLatestAnalysisRule:
    def test_newer_complete_result_replaces(self) -> None:
        later = T0 + timedelta(minutes=5)
        assert replaces_latest(
            _result(recoverable=False, started=later, updated=T0),
            _latest(recoverable=True, started=T0, updated=T0),
        )

    def test_provisional_never_replaces_complete(self) -> None:
        later = T0 + timedelta(minutes=5)
        assert not replaces_latest(
            _result(recoverable=True, started=later, updated=later),
            _latest(recoverable=False, started=T0, updated=T0),
        )

    def test_older_issue_version_never_replaces(self) -> None:
        """A late recovery of an older snapshot must not overwrite an analysis of newer content."""
        later = T0 + timedelta(minutes=5)
        assert not replaces_latest(
            _result(recoverable=False, started=later, updated=T0),
            _latest(recoverable=False, started=T0, updated=T0 + timedelta(minutes=1)),
        )

    def test_older_run_never_replaces(self) -> None:
        assert not replaces_latest(
            _result(recoverable=False, started=T0, updated=T0),
            _latest(recoverable=False, started=T0 + timedelta(seconds=1), updated=T0),
        )


class _Provider:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls = 0

    @property
    def model_id(self) -> str:
        return "m"

    def health(self) -> Any:
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return SimpleNamespace(healthy=self.outcome, detail="d")


def test_health_probe_caches_and_never_raises() -> None:
    ok = _Provider(True)
    probe = ModelHealthProbe(ok, ttl_seconds=60)  # type: ignore[arg-type]
    assert probe.check() and probe.check()
    assert ok.calls == 1
    assert probe.check(force=True) and ok.calls == 2
    broken = ModelHealthProbe(_Provider(RuntimeError("boom")), ttl_seconds=0)  # type: ignore[arg-type]
    assert broken.check() is False and broken.detail == "RuntimeError"


def test_model_calls_are_audited_without_content(caplog: pytest.LogCaptureFixture) -> None:
    from branchops.application.interfaces.model_provider import ChatMessage, CompletionRequest
    from branchops.infrastructure.llm.audit import AuditingModelProvider
    from tests.helpers import ScriptedProvider, unavailable

    secret_prompt = "Müşteri TCKN [TCKN] EFT hatası"
    request = CompletionRequest(
        messages=[ChatMessage("user", secret_prompt)],
        agent="classify",
        correlation_id="corr-9",
        schema_name="X",
    )
    caplog.set_level("INFO", logger="branchops.model_audit")
    AuditingModelProvider(ScriptedProvider()).complete(request)
    with pytest.raises(ModelUnavailableError):
        AuditingModelProvider(ScriptedProvider({"X": [unavailable]})).complete(request)
    records = [r for r in caplog.records if r.name == "branchops.model_audit"]
    assert [r.outcome for r in records] == ["ok", "ModelUnavailableError"]
    ok = records[0].__dict__
    assert ok["correlation_id"] == "corr-9" and len(ok["prompt_sha256"]) == 64 and ok["prompt_tokens"] >= 0
    assert all(secret_prompt not in str(r.__dict__) for r in records)

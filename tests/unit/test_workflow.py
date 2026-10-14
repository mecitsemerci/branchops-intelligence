from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from branchops.agents.base import StructuredCaller
from branchops.application.interfaces.model_provider import ModelProvider, ModelUnavailableError
from branchops.domain.entities.jira import JiraIssueSnapshot
from branchops.domain.enums import AnalysisStatus, FieldState, RelationshipType, StepStatus
from branchops.domain.services.masking import SensitiveDataMasker
from branchops.domain.services.snapshot_merge import merge_event
from branchops.infrastructure.jira.adapter import JiraWebhookAdapter
from branchops.infrastructure.llm.mock_provider import MockModelProvider
from branchops.templates.registry import TemplateRegistry
from branchops.workflows.issue_analysis import IssueAnalysisWorkflow
from branchops.workflows.state import AnalysisState
from tests.helpers import (
    InMemoryHistory,
    MemorySink,
    ScriptedProvider,
    load_sample,
    make_settings,
    unavailable,
)
from tests.unit.test_agents import _candidate

SETTINGS = make_settings()


def snapshot_from(sample: str) -> JiraIssueSnapshot:
    adapter = JiraWebhookAdapter(SETTINGS.jira.field_mapping)
    masker = SensitiveDataMasker()
    event = adapter.normalize(load_sample(sample), transform=masker.mask_text)
    return merge_event(None, event).snapshot


def build(
    registry: TemplateRegistry,
    provider: ModelProvider | None = None,
    history: InMemoryHistory | None = None,
    checkpointer: Any = None,
) -> tuple[IssueAnalysisWorkflow, MemorySink]:
    sink = MemorySink()
    wf = IssueAnalysisWorkflow(
        caller=StructuredCaller(provider or MockModelProvider()),
        registry=registry,
        policy=SETTINGS.scoring,
        history=history or InMemoryHistory(),
        sink=sink,
        settings=SETTINGS.workflow,
        checkpointer=checkpointer,
    )
    return wf, sink


def initial(
    snap: JiraIssueSnapshot, *, allow_degraded: bool = False, input_hash: str = "h1"
) -> AnalysisState:
    return {
        "analysis_id": str(uuid.uuid4()),
        "issue_key": snap.issue_key,
        "correlation_id": "corr-1",
        "inbox_event_id": None,
        "input_hash": input_hash,
        "allow_degraded": allow_degraded,
        "started_at": datetime.now(UTC).isoformat(),
        "snapshot": snap.model_dump(mode="json"),
        "steps": [],
        "errors": [],
        "degraded": [],
    }


def test_complete_incident_scores_high(registry: TemplateRegistry) -> None:
    wf, sink = build(registry)
    result = wf.run(initial(snapshot_from("complete_incident")), thread_id="t1")
    assert result.status is AnalysisStatus.COMPLETED
    assert result.classification is not None
    assert result.classification.category == "TRANSACTION"
    assert result.template_id == "transaction-incident"
    assert result.scores is not None
    assert result.scores.completeness_score is not None and result.scores.completeness_score >= 90
    assert result.scores.quality_score is not None and result.scores.quality_score >= 70
    assert result.canonical_issue is not None
    assert result.canonical_issue.value("branch_code") == "1234"
    assert result.error_signature == "gişe ekranı|EFT-4012"
    assert [s.step for s in result.steps][:3] == ["normalize", "classify", "select_template"]
    assert sink.results == [result] or sink.results[0].analysis_id == result.analysis_id
    assert sum(s.prompt_tokens for s in result.steps) > 0


def test_generic_description_scores_low_with_recommendations(registry: TemplateRegistry) -> None:
    wf, _ = build(registry)
    result = wf.run(initial(snapshot_from("generic_description")), thread_id="t2")
    assert result.scores is not None
    assert result.scores.completeness_score is not None and result.scores.completeness_score < 40
    assert result.scores.quality_score is not None and result.scores.quality_score < 30
    assert "summary" in result.scores.insufficient_fields
    assert result.template_id == "generic-operational-incident"
    rec_fields = {r.field for r in result.recommendations}
    assert {"branch_code", "error_details", "occurrence_time"} <= rec_fields


def test_missing_branch_code_identified(registry: TemplateRegistry) -> None:
    wf, _ = build(registry)
    result = wf.run(initial(snapshot_from("missing_branch_code")), thread_id="t3")
    assert result.scores is not None
    assert "branch_code" in result.scores.missing_required_fields
    assert any(r.field == "branch_code" for r in result.recommendations)


def test_missing_application_identified(registry: TemplateRegistry) -> None:
    wf, _ = build(registry)
    result = wf.run(initial(snapshot_from("missing_application")), thread_id="t4")
    assert result.canonical_issue is not None
    assert result.canonical_issue.field("application").state is FieldState.MISSING


def test_prompt_injection_flagged_for_review(registry: TemplateRegistry) -> None:
    wf, _ = build(registry)
    result = wf.run(initial(snapshot_from("prompt_injection")), thread_id="t5")
    assert result.status is AnalysisStatus.NEEDS_REVIEW
    assert any(f.startswith("prompt_injection:") for f in result.security_flags)
    assert result.classification is not None
    assert result.classification.category == "PERIPHERAL_DEVICE"  # not the injected NETWORK
    assert result.scores is not None and result.scores.quality_score is not None
    assert result.scores.quality_score < 100


def test_pii_never_reaches_model(registry: TemplateRegistry) -> None:
    provider = MockModelProvider()
    wf, _ = build(registry, provider)
    wf.run(initial(snapshot_from("pii_incident")), thread_id="t6")
    prompts = " ".join(m.content for r in provider.calls for m in r.messages)
    for secret in ("10000000146", "4111 1111 1111 1111", "0532 123 45 67", "TR33 0006"):
        assert secret not in prompts


def test_similar_issues_and_recurrence(registry: TemplateRegistry) -> None:
    history = InMemoryHistory(
        candidates=[_candidate("BOPS-101"), _candidate("BOPS-105", branch_code="3307")], signature_count=3
    )
    wf, _ = build(registry, history=history)
    snap = snapshot_from("duplicate_issue")
    result = wf.run(initial(snap), thread_id="t7")
    rel = {s.issue_key: s.relationship for s in result.similar_issues}
    assert rel["BOPS-101"] is RelationshipType.DUPLICATE_CANDIDATE
    assert rel["BOPS-105"] is RelationshipType.RELATED
    assert result.recurring and result.recurring_count == 3


def test_model_unavailable_raises_unless_last_attempt(registry: TemplateRegistry) -> None:
    provider = ScriptedProvider({"ClassificationOutput": [unavailable] * 10})
    wf, sink = build(registry, provider)
    with pytest.raises(ModelUnavailableError):
        wf.run(initial(snapshot_from("complete_incident")), thread_id="t8")
    assert sink.results == []

    provider = ScriptedProvider(
        {"ClassificationOutput": [unavailable] * 10, "ExtractionOutput": [unavailable] * 10}
    )
    wf, sink = build(registry, provider)
    result = wf.run(initial(snapshot_from("complete_incident"), allow_degraded=True), thread_id="t9")
    assert result.status is AnalysisStatus.NEEDS_REVIEW
    assert result.template_id == "generic-operational-incident"
    assert result.scores is not None and result.scores.completeness_score is not None
    failed = {s.step for s in result.steps if s.status is StepStatus.FAILED}
    assert {"classify", "extract"} <= failed
    assert len(sink.results) == 1


def test_invalid_output_degrades_to_partial(registry: TemplateRegistry) -> None:
    provider = ScriptedProvider({"QualityAssessmentOutput": [lambda r: "garbage"] * 4})
    wf, _ = build(registry, provider)
    result = wf.run(initial(snapshot_from("complete_incident")), thread_id="t10")
    assert result.status is AnalysisStatus.PARTIAL
    assert result.scores is not None
    assert result.scores.quality_score is None
    assert result.scores.completeness_score is not None


def test_resume_from_checkpoint_skips_completed_llm_steps(registry: TemplateRegistry) -> None:
    saver = InMemorySaver()
    provider = ScriptedProvider({"ExtractionOutput": [unavailable, unavailable]})
    wf, sink = build(registry, provider, checkpointer=saver)
    state = initial(snapshot_from("complete_incident"))
    with pytest.raises(ModelUnavailableError):
        wf.run(state, thread_id="inbox:1")
    assert len(provider.calls_for("ClassificationOutput")) == 1

    result = wf.run(state, thread_id="inbox:1")  # retry of the same event
    assert result.status is AnalysisStatus.COMPLETED
    assert len(provider.calls_for("ClassificationOutput")) == 1  # not repeated
    assert len(sink.results) == 1

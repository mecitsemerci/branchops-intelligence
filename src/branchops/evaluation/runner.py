"""Run the production analysis workflow over an evaluation dataset, offline.

The workflow, agents, templates and scoring engine are the production ones; only the persistence and history
ports are replaced by in-memory implementations, and the model provider is wrapped for metering (and,
optionally, recording). Cases are analysed in dataset order so that each one sees the earlier cases as its
history.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from branchops.agents.base import StructuredCaller
from branchops.application.interfaces.model_provider import ModelProvider
from branchops.application.interfaces.repositories import CandidateIssue
from branchops.config.settings import Settings
from branchops.domain.entities.analysis import AnalysisResult
from branchops.domain.services.lexicon import LEXICON_VERSION
from branchops.domain.services.masking import SensitiveDataMasker
from branchops.domain.services.snapshot_merge import merge_event
from branchops.evaluation.dataset import EvalCase, EvalDataset
from branchops.evaluation.history import EvaluationHistory
from branchops.evaluation.providers import CallRecord, MeteringProvider
from branchops.infrastructure.sources.jira.adapter import JiraWebhookAdapter
from branchops.templates.registry import TemplateRegistry
from branchops.workflows.issue_analysis import IssueAnalysisWorkflow
from branchops.workflows.state import AnalysisState

EVAL_FORMAT = "branchops-eval-run/1"


class _Sink:
    def __init__(self) -> None:
        self.results: list[AnalysisResult] = []

    def save(self, result: AnalysisResult) -> None:
        self.results.append(result)


@dataclass
class CaseRun:
    """Raw output of one case: the analysis plus model call statistics and retrieved candidates."""

    case: EvalCase
    result: AnalysisResult | None
    calls: list[CallRecord]
    retrieved: list[str]
    error: str | None = None


@dataclass
class EvaluationRun:
    dataset: EvalDataset
    metadata: dict[str, Any]
    cases: list[CaseRun] = field(default_factory=list)
    runtime: dict[str, Any] = field(default_factory=dict)


def run_metadata(
    settings: Settings, registry: TemplateRegistry, provider: ModelProvider, dataset: EvalDataset, label: str
) -> dict[str, Any]:
    """Everything that makes two runs comparable (deterministic for a fixed configuration)."""
    return {
        "format": EVAL_FORMAT,
        "label": label,
        "model_id": provider.model_id,
        "prompt_version": settings.workflow.prompt_version,
        "scoring_policy_version": settings.scoring.version,
        "templates": [f"{t.template_id}@{t.version}" for t in registry.latest_published()],
        "lexicon_version": LEXICON_VERSION,
        "dataset": {"name": dataset.name, "version": dataset.version, "sha256": dataset.sha256},
        "case_count": len(dataset.cases),
    }


class EvaluationRunner:
    def __init__(
        self,
        settings: Settings,
        registry: TemplateRegistry,
        provider: ModelProvider,
        *,
        label: str | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._metered = MeteringProvider(provider)
        self._label = label or provider.model_id
        self._masker = SensitiveDataMasker(settings.security.masking_extra_patterns)
        self._adapter = JiraWebhookAdapter(settings.jira.field_mapping)

    def _workflow(self, history: EvaluationHistory, sink: _Sink) -> IssueAnalysisWorkflow:
        llm = self._settings.llm
        return IssueAnalysisWorkflow(
            caller=StructuredCaller(
                self._metered,
                max_repair_attempts=self._settings.workflow.max_repair_attempts,
                max_output_tokens=llm.max_output_tokens,
            ),
            registry=self._registry,
            policy=self._settings.scoring,
            history=history,
            sink=sink,
            settings=self._settings.workflow,
            checkpointer=None,
            text_filter=self._masker.mask_str,
        )

    def run(self, dataset: EvalDataset) -> EvaluationRun:
        started = time.perf_counter()
        history = EvaluationHistory()
        sink = _Sink()
        workflow = self._workflow(history, sink)
        run = EvaluationRun(
            dataset=dataset,
            metadata=run_metadata(self._settings, self._registry, self._metered, dataset, self._label),
        )
        for case in dataset.cases:
            run.cases.append(self._run_case(workflow, history, case))
        run.runtime = {
            "finished_at": datetime.now(UTC).isoformat(),
            "wall_seconds": round(time.perf_counter() - started, 3),
        }
        return run

    def _run_case(
        self, workflow: IssueAnalysisWorkflow, history: EvaluationHistory, case: EvalCase
    ) -> CaseRun:
        correlation_id = f"eval:{case.id}"
        event = self._adapter.normalize(case.payload, transform=lambda t: self._masker.mask(t).text)
        snapshot = merge_event(None, event).snapshot
        state: AnalysisState = {
            "analysis_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"branchops-eval:{case.id}")),
            "incident_key": snapshot.incident_key,
            "correlation_id": correlation_id,
            "inbox_event_id": None,
            "input_hash": "eval",
            "allow_degraded": True,
            "started_at": (snapshot.created_at or datetime(2026, 1, 1, tzinfo=UTC)).isoformat(),
            "snapshot": snapshot.model_dump(mode="json"),
            "steps": [],
            "errors": [],
            "degraded": [],
        }
        result: AnalysisResult | None = None
        error: str | None = None
        try:
            result = workflow.run(state, thread_id=correlation_id)
        except Exception as exc:  # one broken case must not abort the run; it is reported
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
        if result is not None:
            history.add(_candidate(result))
        return CaseRun(
            case=case,
            result=result,
            calls=self._metered.calls_for(correlation_id),
            retrieved=list(history.retrieved.get(snapshot.incident_key, [])),
            error=error,
        )


def _candidate(result: AnalysisResult) -> CandidateIssue:
    issue = result.canonical_issue
    cls = result.classification
    return CandidateIssue(
        incident_key=result.incident_key,
        summary=issue.value("summary") if issue else None,
        description_excerpt=(issue.value("description") or "")[:400] if issue else None,
        category=cls.category if cls else None,
        subcategory=cls.subcategory if cls else None,
        branch_code=issue.value("branch_code") if issue else None,
        application=issue.value("application") if issue else None,
        error_signature=result.error_signature,
        created_at=issue.source_created_at if issue else None,
        text_rank=0.0,
        source=issue.source if issue else "jira",
    )

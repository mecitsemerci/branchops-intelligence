"""Bounded LangGraph workflow: normalise → classify → template → extract → assess → score → similarity →
recommend → persist.

The graph is a fixed linear pipeline (no model-driven routing, no tools, recursion limit enforced). Model
steps degrade gracefully: non-transient failures produce partial results, transient unavailability
re-raises so the inbox retries the event later, except on the last attempt (``allow_degraded``) where a
partial analysis is persisted instead of losing the event.
"""

from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from branchops.agents.base import StructuredCaller, StructuredOutputError
from branchops.agents.classification import ClassificationAgent
from branchops.agents.extraction import ExtractionAgent, ExtractionOutput, build_canonical_issue
from branchops.agents.prompts import issue_data_block, issue_sources
from branchops.agents.quality_assessment import QualityAssessmentAgent
from branchops.agents.recommendation import RecommendationAgent, deterministic_recommendations
from branchops.agents.similarity import IssueFingerprint, SimilarityAgent, shortlist
from branchops.application.interfaces.model_provider import ModelRequestError, ModelUnavailableError
from branchops.application.interfaces.repositories import (
    AnalysisSink,
    HistoricalIssueReader,
    NullProgressReporter,
    ProgressReporter,
    RunInfo,
)
from branchops.config.settings import WorkflowSettings
from branchops.domain.entities.analysis import (
    AnalysisResult,
    Classification,
    FieldAssessment,
    Recommendation,
    ScoreResult,
    SimilarIssue,
    StepRecord,
)
from branchops.domain.entities.incident import IncidentSnapshot
from branchops.domain.entities.issue import CanonicalIssue
from branchops.domain.enums import AnalysisStatus, RelationshipType, StepStatus
from branchops.domain.services.prompt_injection import detect_prompt_injection
from branchops.domain.services.signatures import error_signature
from branchops.infrastructure.observability.telemetry import get_instruments, tracer
from branchops.scoring.engine import resolve_fields, score_issue
from branchops.scoring.policies import ScoringPolicy
from branchops.templates.registry import TemplateRegistry
from branchops.workflows.state import AnalysisState

logger = logging.getLogger(__name__)

NODES = (
    "normalize",
    "classify",
    "select_template",
    "extract",
    "assess_quality",
    "score",
    "similarity",
    "recommend",
    "persist",
)
_RECOVERABLE = (StructuredOutputError, ModelRequestError)


class _StepTimer:
    def __init__(self, step: str) -> None:
        self.step = step
        self.started = datetime.now(UTC)
        self._t0 = time.perf_counter()

    def record(
        self,
        status: StepStatus,
        *,
        attempts: int = 1,
        model_id: str | None = None,
        output_mode: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error: str | None = None,
    ) -> dict[str, Any]:
        duration = int((time.perf_counter() - self._t0) * 1000)
        get_instruments().agent_duration_ms.record(duration, {"step": self.step, "status": status.value})
        return StepRecord(
            step=self.step,
            status=status,
            started_at=self.started,
            duration_ms=duration,
            attempts=attempts,
            model_id=model_id,
            output_mode=output_mode,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            error=error[:1000] if error else None,
        ).model_dump(mode="json")


class IssueAnalysisWorkflow:
    def __init__(
        self,
        *,
        caller: StructuredCaller,
        registry: TemplateRegistry,
        policy: ScoringPolicy,
        history: HistoricalIssueReader,
        sink: AnalysisSink,
        settings: WorkflowSettings,
        checkpointer: Any = None,
        progress: ProgressReporter | None = None,
    ) -> None:
        self._caller = caller
        self._registry = registry
        self._policy = policy
        self._history = history
        self._sink = sink
        self._settings = settings
        self._classifier = ClassificationAgent(caller)
        self._extractor = ExtractionAgent(caller)
        self._assessor = QualityAssessmentAgent(caller)
        self._similarity = SimilarityAgent(caller)
        self._recommender = RecommendationAgent(caller)
        self._checkpointer = checkpointer
        self._progress: ProgressReporter = progress or NullProgressReporter()
        self.graph = self._build().compile(checkpointer=checkpointer)

    @property
    def model_id(self) -> str:
        return self._caller.provider.model_id

    # --- graph ----------------------------------------------------------------------------------

    def _build(self) -> StateGraph[AnalysisState]:
        g: StateGraph[AnalysisState] = StateGraph(AnalysisState)
        transient_retry = RetryPolicy(
            max_attempts=2, initial_interval=2.0, retry_on=lambda e: isinstance(e, ModelUnavailableError)
        )
        nodes: list[tuple[str, Callable[[AnalysisState], dict[str, Any]], RetryPolicy | None]] = [
            ("normalize", self._normalize, None),
            ("classify", self._classify, transient_retry),
            ("select_template", self._select_template, None),
            ("extract", self._extract, transient_retry),
            ("assess_quality", self._assess_quality, transient_retry),
            ("score", self._score, None),
            ("similarity", self._similarity_node, transient_retry),
            ("recommend", self._recommend, transient_retry),
            ("persist", self._persist, None),
        ]
        for name, fn, retry in nodes:
            action: Any = self._tracked(name, fn)
            g.add_node(name, action, retry_policy=retry)
        g.add_edge(START, "normalize")
        for a, b in itertools.pairwise(NODES):
            g.add_edge(a, b)
        g.add_edge("persist", END)
        return g

    def _tracked(
        self, name: str, fn: Callable[[AnalysisState], dict[str, Any]]
    ) -> Callable[[AnalysisState], dict[str, Any]]:
        """Report node start/finish to the live progress reporter (best effort, never raises)."""

        def node(state: AnalysisState) -> dict[str, Any]:
            analysis_id = state["analysis_id"]
            self._progress.step_started(analysis_id, name)
            update = fn(state)
            if name != "persist":  # persist writes the final step list itself
                for record in update.get("steps", []):
                    self._progress.step_finished(analysis_id, StepRecord.model_validate(record))
            return update

        node.__name__ = name
        return node

    def run(self, initial: AnalysisState, thread_id: str) -> AnalysisResult:
        """Run (or resume) the workflow. ``thread_id`` must be stable across retries of the same event."""
        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": len(NODES) + 5,
        }
        inputs: AnalysisState | None = initial
        resumed = False
        if self._checkpointer is not None:
            existing = self.graph.get_state(config)
            if existing.next and existing.values.get("input_hash") == initial.get("input_hash"):
                logger.info("resuming analysis from checkpoint", extra={"next": list(existing.next)})
                self.graph.update_state(config, {"allow_degraded": initial.get("allow_degraded", False)})
                inputs = None
                resumed = True
        self._progress.run_started(
            RunInfo(
                analysis_id=initial["analysis_id"],
                incident_key=initial["incident_key"],
                correlation_id=initial["correlation_id"],
                inbox_event_id=initial.get("inbox_event_id"),
                input_hash=initial["input_hash"],
                prompt_version=self._settings.prompt_version,
                started_at=datetime.fromisoformat(initial["started_at"]),
                worker_id=initial.get("worker_id"),
                model_id=self.model_id,
                resumed=resumed,
            )
        )
        with tracer().start_as_current_span("workflow.issue_analysis") as span:
            span.set_attribute("branchops.incident_key", initial.get("incident_key", ""))
            span.set_attribute("branchops.correlation_id", initial.get("correlation_id", ""))
            try:
                final: AnalysisState = self.graph.invoke(inputs, config)  # type: ignore[assignment]
            except Exception as exc:
                self._progress.run_failed(initial["analysis_id"], f"{type(exc).__name__}: {exc}")
                raise
        if self._checkpointer is not None:
            try:
                self._checkpointer.delete_thread(thread_id)
            except Exception:  # cleanup only
                logger.warning("checkpoint cleanup failed", exc_info=True)
        return self._to_result(final)

    # --- helpers --------------------------------------------------------------------------------

    def _deadline(self) -> float:
        return time.monotonic() + self._settings.node_timeout_seconds

    def _llm_step[T](
        self, state: AnalysisState, step: str, fn: Callable[[], tuple[T, Any]]
    ) -> tuple[T | None, dict[str, Any], str | None]:
        timer = _StepTimer(step)
        try:
            value, stats = fn()
        except ModelUnavailableError as exc:
            if not state.get("allow_degraded", False):
                raise
            return None, timer.record(StepStatus.FAILED, error=str(exc)), f"{step}: {exc}"
        except _RECOVERABLE as exc:
            usage = getattr(exc, "usage", None)
            record = timer.record(
                StepStatus.FAILED,
                attempts=getattr(exc, "attempts", 1),
                model_id=self.model_id,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                error=str(exc),
            )
            return None, record, f"{step}: {exc}"
        if stats is None:
            return value, timer.record(StepStatus.SKIPPED), None
        record = timer.record(
            StepStatus.SUCCEEDED,
            attempts=stats.attempts,
            model_id=stats.model_id,
            output_mode=str(stats.output_mode) if stats.output_mode else None,
            prompt_tokens=stats.prompt_tokens,
            completion_tokens=stats.completion_tokens,
        )
        return value, record, None

    @staticmethod
    def _snapshot(state: AnalysisState) -> IncidentSnapshot:
        return IncidentSnapshot.model_validate(state["snapshot"])

    # --- nodes ----------------------------------------------------------------------------------

    def _normalize(self, state: AnalysisState) -> dict[str, Any]:
        timer = _StepTimer("normalize")
        snapshot = self._snapshot(state)
        sources = issue_sources(snapshot)
        flags = detect_prompt_injection(sources.values())
        if flags:
            logger.warning("prompt injection indicators detected", extra={"flags": ",".join(flags)})
        return {
            "sources": sources,
            "issue_block": issue_data_block(snapshot, self._settings.max_input_chars),
            "security_flags": flags,
            "model_id": self.model_id,
            "steps": [timer.record(StepStatus.SUCCEEDED)],
        }

    def _classify(self, state: AnalysisState) -> dict[str, Any]:
        def call() -> tuple[Classification, Any]:
            classification, result = self._classifier.run(
                issue_block=state["issue_block"],
                sources=state["sources"],
                taxonomy=self._registry.taxonomy(),
                category_descriptions=self._registry.category_descriptions(),
                correlation_id=state["correlation_id"],
                deadline=self._deadline(),
            )
            return classification, result.stats

        value, record, error = self._llm_step(state, "classify", call)
        if value is None:
            get_instruments().classification_failures.add(1)
        return {
            "classification": value.model_dump(mode="json") if value else None,
            "steps": [record],
            "errors": [error] if error else [],
            "degraded": ["classify"] if error else [],
        }

    def _select_template(self, state: AnalysisState) -> dict[str, Any]:
        timer = _StepTimer("select_template")
        cls = state.get("classification")
        ctx = {
            "category": cls.get("category") if cls else None,
            "subcategory": cls.get("subcategory") if cls else None,
        }
        template = self._registry.select(ctx["category"], ctx["subcategory"], ctx)
        return {
            "template_id": template.template_id,
            "template_version": template.version,
            "steps": [timer.record(StepStatus.SUCCEEDED)],
        }

    def _extract(self, state: AnalysisState) -> dict[str, Any]:
        def call() -> tuple[ExtractionOutput, Any]:
            result = self._extractor.run(
                issue_block=state["issue_block"],
                correlation_id=state["correlation_id"],
                deadline=self._deadline(),
            )
            return result.value, result.stats

        value, record, error = self._llm_step(state, "extract", call)
        cls = state.get("classification")
        canonical = build_canonical_issue(
            self._snapshot(state),
            state["sources"],
            Classification.model_validate(cls) if cls else None,
            value,
            self.model_id,
            extraction_failed=value is None,
        )
        return {
            "extraction": value.model_dump(mode="json") if value else None,
            "extraction_failed": value is None,
            "canonical": canonical.model_dump(mode="json"),
            "steps": [record],
            "errors": [error] if error else [],
            "degraded": ["extract"] if error else [],
        }

    def _template(self, state: AnalysisState) -> Any:
        template = self._registry.get(state["template_id"], state["template_version"])
        if template is None:  # pragma: no cover - registry is immutable within a process
            raise RuntimeError(f"template {state['template_id']}@{state['template_version']} not loaded")
        return template

    def _assess_quality(self, state: AnalysisState) -> dict[str, Any]:
        template = self._template(state)
        canonical = CanonicalIssue.model_validate(state["canonical"])
        resolved = resolve_fields(template, canonical)

        def call() -> tuple[dict[str, FieldAssessment], Any]:
            assessments, result = self._assessor.run(
                resolved=resolved,
                issue_block=state["issue_block"],
                sources=state["sources"],
                correlation_id=state["correlation_id"],
                deadline=self._deadline(),
            )
            return assessments, result.stats if result else None

        value, record, error = self._llm_step(state, "assess_quality", call)
        return {
            "assessments": (
                {k: v.model_dump(mode="json") for k, v in value.items()} if value is not None else None
            ),
            "steps": [record],
            "errors": [error] if error else [],
            "degraded": ["assess_quality"] if error else [],
        }

    def _score(self, state: AnalysisState) -> dict[str, Any]:
        timer = _StepTimer("score")
        template = self._template(state)
        canonical = CanonicalIssue.model_validate(state["canonical"])
        raw = state.get("assessments")
        assessments = (
            {k: FieldAssessment.model_validate(v) for k, v in raw.items()} if raw is not None else None
        )
        output = score_issue(template, canonical, assessments, self._policy)
        instruments = get_instruments()
        if output.result.completeness_score is not None:
            instruments.completeness_score.record(
                output.result.completeness_score, {"template": template.template_id}
            )
        if output.result.quality_score is not None:
            instruments.quality_score.record(output.result.quality_score, {"template": template.template_id})
        return {
            "scores": output.result.model_dump(mode="json"),
            "validated_assessments": [a.model_dump(mode="json") for a in output.assessments],
            "steps": [timer.record(StepStatus.SUCCEEDED)],
        }

    def _similarity_node(self, state: AnalysisState) -> dict[str, Any]:
        cfg = self._settings.similarity
        canonical = CanonicalIssue.model_validate(state["canonical"])
        signature = error_signature(canonical.value("application"), canonical.value("error_details"))
        current = IssueFingerprint(
            incident_key=canonical.incident_key,
            summary=canonical.value("summary"),
            error_details=canonical.value("error_details"),
            category=canonical.value("category"),
            subcategory=canonical.value("subcategory"),
            branch_code=canonical.value("branch_code"),
            application=canonical.value("application"),
            error_signature=signature,
            created_at=canonical.source_created_at,
        )
        query = " ".join(
            filter(None, [current.summary, current.error_details, current.application, current.subcategory])
        )
        timer = _StepTimer("similarity.retrieve")
        candidates = self._history.find_candidates(
            canonical.incident_key, query, signature, cfg.lookback_days, cfg.candidate_limit
        )
        short = shortlist(
            current,
            candidates,
            min_rank=cfg.min_text_rank,
            top_n=cfg.verify_top_n,
            duplicate_window=timedelta(hours=cfg.duplicate_window_hours),
        )
        retrieve_record = timer.record(StepStatus.SUCCEEDED)
        by_key = {c.incident_key: c for c in candidates}

        def call() -> tuple[list[SimilarIssue], Any]:
            verified, result = self._similarity.verify(
                current=current,
                shortlisted=short,
                candidates=by_key,
                correlation_id=state["correlation_id"],
                deadline=self._deadline(),
            )
            return verified, result.stats if result else None

        value, record, error = self._llm_step(state, "similarity.verify", call)
        similar = value if value is not None else short  # unverified candidates are still useful
        recurring_count = 1
        if signature:
            recurring_count = self._history.count_signature_occurrences(
                canonical.incident_key, signature, cfg.recurring_window_days
            )
        related = sum(
            1
            for s in similar
            if s.relationship in (RelationshipType.RELATED, RelationshipType.DUPLICATE_CANDIDATE)
        )
        recurring_count = max(recurring_count, related + 1)
        return {
            "similar": [s.model_dump(mode="json") for s in similar],
            "recurring": recurring_count >= cfg.recurring_threshold,
            "recurring_count": recurring_count,
            "steps": [retrieve_record, record],
            "errors": [error] if error else [],
            "degraded": ["similarity"] if error else [],
        }

    def _recommend(self, state: AnalysisState) -> dict[str, Any]:
        template = self._template(state)
        scores = ScoreResult.model_validate(state["scores"])
        baseline = deterministic_recommendations(template, scores)

        def call() -> tuple[list[Recommendation], Any]:
            recs, result = self._recommender.run(
                baseline=baseline,
                issue_block=state["issue_block"],
                correlation_id=state["correlation_id"],
                deadline=self._deadline(),
            )
            return recs, result.stats if result else None

        value, record, error = self._llm_step(state, "recommend", call)
        recs = value if value is not None else baseline
        return {
            "recommendations": [r.model_dump(mode="json") for r in recs],
            "steps": [record],
            "errors": [error] if error else [],
            "degraded": ["recommend"] if error else [],
        }

    def _persist(self, state: AnalysisState) -> dict[str, Any]:
        timer = _StepTimer("persist")
        status = self._final_status(state)
        steps = [*state.get("steps", []), timer.record(StepStatus.SUCCEEDED)]
        result = self._to_result({**state, "status": status.value, "steps": steps})  # type: ignore[typeddict-item]
        self._sink.save(result)
        return {"status": status.value, "steps": [steps[-1]]}

    @staticmethod
    def _final_status(state: AnalysisState) -> AnalysisStatus:
        degraded = set(state.get("degraded", []))
        if state.get("classification") is None or state.get("security_flags"):
            return AnalysisStatus.NEEDS_REVIEW
        if degraded:
            return AnalysisStatus.PARTIAL
        return AnalysisStatus.COMPLETED

    def _to_result(self, state: AnalysisState) -> AnalysisResult:
        cls = state.get("classification")
        canonical = state.get("canonical")
        canonical_issue = CanonicalIssue.model_validate(canonical) if canonical else None
        signature = (
            error_signature(canonical_issue.value("application"), canonical_issue.value("error_details"))
            if canonical_issue
            else None
        )
        status = state.get("status") or self._final_status(state).value
        return AnalysisResult(
            analysis_id=state["analysis_id"],
            incident_key=state["incident_key"],
            correlation_id=state["correlation_id"],
            inbox_event_id=state.get("inbox_event_id"),
            input_hash=state["input_hash"],
            prompt_version=self._settings.prompt_version,
            started_at=datetime.fromisoformat(state["started_at"]),
            completed_at=datetime.now(UTC),
            status=AnalysisStatus(status),
            classification=Classification.model_validate(cls) if cls else None,
            template_id=state.get("template_id"),
            template_version=state.get("template_version"),
            scores=ScoreResult.model_validate(state["scores"]) if state.get("scores") else None,
            assessments=[FieldAssessment.model_validate(a) for a in state.get("validated_assessments", [])],
            recommendations=[Recommendation.model_validate(r) for r in state.get("recommendations", [])],
            similar_issues=[SimilarIssue.model_validate(s) for s in state.get("similar", [])],
            recurring=state.get("recurring", False),
            recurring_count=state.get("recurring_count", 0),
            security_flags=state.get("security_flags", []),
            steps=[StepRecord.model_validate(s) for s in state.get("steps", [])],
            errors=state.get("errors", []),
            model_id=state.get("model_id"),
            canonical_issue=canonical_issue,
            error_signature=signature,
            source_updated_at=self._snapshot(state).updated_at,
        )

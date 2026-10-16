"""Contracts produced by the analysis workflow."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field

from branchops.domain.entities.issue import CanonicalIssue
from branchops.domain.enums import (
    AnalysisStatus,
    Confidence,
    FieldState,
    RecommendationPriority,
    RelationshipType,
    RubricLevel,
    StepStatus,
)
from branchops.domain.value_objects import EvidenceRef


class Classification(BaseModel):
    category: str
    subcategory: str | None = None
    application: str | None = None
    business_process: str | None = None
    confidence: Confidence = Confidence.LOW
    evidence: list[EvidenceRef] = Field(default_factory=list)
    rationale: str | None = Field(default=None, max_length=600)


class FieldAssessment(BaseModel):
    field: str
    level: RubricLevel
    reason: str = Field(default="", max_length=600)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    # Set by Python validation when a deterministic rule overrode the model's level.
    adjusted_from: RubricLevel | None = None
    adjustment_reason: str | None = None


class FieldScore(BaseModel):
    field: str
    weight: float
    state: FieldState
    applicable: bool
    required: bool
    completeness_credit: float | None
    quality_credit: float | None
    rubric_level: RubricLevel | None = None
    validation_errors: list[str] = Field(default_factory=list)


class ScoreResult(BaseModel):
    completeness_score: float | None
    quality_score: float | None
    field_scores: list[FieldScore]
    missing_fields: list[str]
    insufficient_fields: list[str]
    unknown_fields: list[str]
    # Required fields in MISSING *or* UNKNOWN state (kept for compatibility: both need a follow-up).
    missing_required_fields: list[str]
    # The same split by cause: confirmed absent vs. not determinable from the record (e.g. extraction failed).
    # Defaults keep analyses stored before these lists existed readable.
    missing_required_confirmed: list[str] = Field(default_factory=list)
    unknown_required: list[str] = Field(default_factory=list)
    template_id: str
    template_version: str
    scoring_policy_version: str


class Recommendation(BaseModel):
    field: str | None = None
    priority: RecommendationPriority = RecommendationPriority.MEDIUM
    message: str = Field(min_length=3, max_length=800)
    source: str = "deterministic"


class SimilarIssue(BaseModel):
    incident_key: str = Field(validation_alias=AliasChoices("incident_key", "issue_key"))
    source: str = "jira"
    summary: str | None = None
    text_rank: float = 0.0
    deterministic_signals: list[str] = Field(default_factory=list)
    relationship: RelationshipType = RelationshipType.UNVERIFIED_CANDIDATE
    rationale: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)


class InvestigationFinding(BaseModel):
    """One conclusion of the bounded historical investigation.

    ``kind`` is OBSERVATION only when at least one evidence reference was verified by Python against the
    current record or a tool result; anything else (including every root-cause statement the model could not
    support) is stored as HYPOTHESIS.
    """

    statement: str = Field(max_length=600)
    kind: Literal["OBSERVATION", "HYPOTHESIS"]
    evidence: list[EvidenceRef] = Field(default_factory=list)
    related_incidents: list[str] = Field(default_factory=list)


class ToolCallRecord(BaseModel):
    tool: str
    arguments: dict[str, str] = Field(default_factory=dict)
    result_count: int = 0
    error: str | None = None


class InvestigationResult(BaseModel):
    status: Literal["COMPLETED", "BUDGET_EXHAUSTED", "DEGRADED", "SKIPPED"]
    stopped_reason: str | None = None
    rounds: int = 0
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    findings: list[InvestigationFinding] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0


class StepRecord(BaseModel):
    step: str
    status: StepStatus
    started_at: datetime
    duration_ms: int
    attempts: int = 1
    model_id: str | None = None
    output_mode: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None


class AnalysisResult(BaseModel):
    analysis_id: str
    incident_key: str
    correlation_id: str
    inbox_event_id: str | None = None
    input_hash: str
    prompt_version: str
    started_at: datetime
    completed_at: datetime | None = None
    status: AnalysisStatus
    classification: Classification | None = None
    template_id: str | None = None
    template_version: str | None = None
    scores: ScoreResult | None = None
    assessments: list[FieldAssessment] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    similar_issues: list[SimilarIssue] = Field(default_factory=list)
    recurring: bool = False
    recurring_count: int = 0
    security_flags: list[str] = Field(default_factory=list)
    steps: list[StepRecord] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    # True when a step degraded because the model was unavailable: the result is provisional and the
    # recovery scheduler re-analyses the incident once the model is back.
    recoverable: bool = False
    investigation: InvestigationResult | None = None
    model_id: str | None = None
    canonical_issue: CanonicalIssue | None = None
    error_signature: str | None = None
    source_updated_at: datetime | None = None

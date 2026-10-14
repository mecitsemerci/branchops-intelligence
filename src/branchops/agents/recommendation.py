"""RecommendationAgent: specific, actionable requests for missing or weak information.

Deterministic recommendations (from template hints) are always produced; the model may rewrite them to be
more specific to the issue. Priorities are always computed deterministically, and the model may only
address fields that are actual gaps.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationInfo, model_validator

from branchops.agents.base import StructuredCaller, StructuredResult
from branchops.agents.prompts import GUARDRAILS, json_block
from branchops.domain.entities.analysis import FieldScore, Recommendation, ScoreResult
from branchops.domain.enums import FieldState, RecommendationPriority
from branchops.templates.model import IssueTemplate

AGENT_NAME = "recommendation"
GAP_STATES = (FieldState.MISSING, FieldState.UNKNOWN, FieldState.INSUFFICIENT)


def gap_priority(fs: FieldScore) -> RecommendationPriority:
    if fs.required and fs.state in (FieldState.MISSING, FieldState.UNKNOWN):
        return RecommendationPriority.HIGH
    if fs.required or fs.weight >= 8:
        return RecommendationPriority.MEDIUM
    return RecommendationPriority.LOW


_PRIORITY_ORDER = {
    RecommendationPriority.HIGH: 0,
    RecommendationPriority.MEDIUM: 1,
    RecommendationPriority.LOW: 2,
}


def deterministic_recommendations(template: IssueTemplate, scores: ScoreResult) -> list[Recommendation]:
    recs: list[Recommendation] = []
    for fs in scores.field_scores:
        if fs.state not in GAP_STATES:
            continue
        definition = template.field_def(fs.field)
        if definition is None:
            continue
        message = definition.recommendation
        if fs.validation_errors:
            message = f"{message} ({'; '.join(fs.validation_errors)})"
        recs.append(
            Recommendation(field=fs.field, priority=gap_priority(fs), message=message, source="template")
        )
    recs.sort(key=lambda r: (_PRIORITY_ORDER[r.priority], r.field or ""))
    return recs


class RecommendationItemOutput(BaseModel):
    field: str
    message: str = Field(min_length=10, max_length=600)


class RecommendationOutput(BaseModel):
    recommendations: list[RecommendationItemOutput] = Field(max_length=20)

    @model_validator(mode="after")
    def _only_gaps(self, info: ValidationInfo) -> RecommendationOutput:
        ctx: dict[str, Any] = info.context or {}
        gaps: set[str] | None = ctx.get("gaps")
        if gaps is not None:
            extra = sorted({r.field for r in self.recommendations} - gaps)
            if extra:
                raise ValueError(f"recommendations for non-gap fields {extra}; allowed {sorted(gaps)}")
        return self


SYSTEM_PROMPT = (
    GUARDRAILS
    + """
Task: write one recommendation per gap in <gaps>, in Turkish, addressed to the branch staff who opened the
ticket. Each recommendation must say exactly what information to add, referring to the concrete situation
in the issue (application, process, error) where possible. Do not suggest technical fixes or remediation,
only information to provide. Do not ask for customer personal data (TCKN, card number, IBAN).
"""
)


class RecommendationAgent:
    def __init__(self, caller: StructuredCaller) -> None:
        self._caller = caller

    def run(
        self,
        *,
        baseline: list[Recommendation],
        issue_block: str,
        correlation_id: str,
        deadline: float | None = None,
    ) -> tuple[list[Recommendation], StructuredResult[RecommendationOutput] | None]:
        gaps = [r for r in baseline if r.field]
        if not gaps:
            return baseline, None
        payload = [{"field": r.field, "template_hint": r.message} for r in gaps]
        result = self._caller.call(
            agent=AGENT_NAME,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=f"{json_block('gaps', payload)}\n\n{issue_block}",
            schema=RecommendationOutput,
            correlation_id=correlation_id,
            validation_context={"gaps": {r.field for r in gaps}},
            deadline=deadline,
        )
        rewritten = {r.field: r.message.strip() for r in result.value.recommendations}
        merged = [
            r.model_copy(update={"message": rewritten[r.field], "source": "model"})
            if r.field in rewritten
            else r
            for r in baseline
        ]
        return merged, result

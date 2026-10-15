"""QualityAssessmentAgent: rubric levels per present field. It never produces numeric scores."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationInfo, model_validator

from branchops.agents.base import StructuredCaller, StructuredResult
from branchops.agents.prompts import GUARDRAILS, EvidenceQuote, json_block
from branchops.domain.entities.analysis import FieldAssessment
from branchops.domain.enums import FieldState, RubricLevel
from branchops.domain.services.evidence import verify_evidence
from branchops.domain.value_objects import EvidenceRef
from branchops.scoring.engine import ResolvedField

AGENT_NAME = "quality_assessment"


class FieldLevelOutput(BaseModel):
    field: str
    level: RubricLevel = Field(description="GOOD, ACCEPTABLE or POOR (ABSENT only if the value is empty)")
    reason: str = Field(
        default="", max_length=400, description="one sentence in Turkish referring to the rubric"
    )
    evidence: list[EvidenceQuote] = Field(default_factory=list, max_length=2)


class QualityAssessmentOutput(BaseModel):
    assessments: list[FieldLevelOutput]

    @model_validator(mode="after")
    def _known_fields(self, info: ValidationInfo) -> QualityAssessmentOutput:
        ctx: dict[str, Any] = info.context or {}
        expected: set[str] | None = ctx.get("fields")
        if expected is None:
            return self
        unknown = sorted({a.field for a in self.assessments} - expected)
        if unknown:
            raise ValueError(f"assessments for unexpected fields: {unknown}; allowed: {sorted(expected)}")
        missing = sorted(expected - {a.field for a in self.assessments})
        if missing:
            raise ValueError(f"missing assessments for fields: {missing}")
        return self


SYSTEM_PROMPT = (
    GUARDRAILS
    + """
Task: judge how useful each provided field value is for resolving the incident, using the rubric given
for that field in <fields_to_assess>. Assign exactly one level per field:
GOOD = meets the "good" description; ACCEPTABLE = meets "acceptable"; POOR = matches "poor" or is too
generic to act on. Judge only the information quality, not the incident severity. Do not output scores.
"""
)


class QualityAssessmentAgent:
    def __init__(self, caller: StructuredCaller) -> None:
        self._caller = caller

    def run(
        self,
        *,
        resolved: list[ResolvedField],
        issue_block: str,
        sources: dict[str, str],
        correlation_id: str,
        deadline: float | None = None,
    ) -> tuple[dict[str, FieldAssessment], StructuredResult[QualityAssessmentOutput] | None]:
        to_assess = [r for r in resolved if r.state in (FieldState.SUFFICIENT, FieldState.INSUFFICIENT)]
        if not to_assess:
            return {}, None
        payload = [
            {
                "field": r.definition.name,
                "label": r.definition.label,
                "value": (r.value or "")[:1500],
                "rubric": r.definition.rubric.model_dump(),
                "validation_problems": list(r.validation_errors),
            }
            for r in to_assess
        ]
        user_prompt = f"{json_block('fields_to_assess', payload)}\n\n{issue_block}"
        result = self._caller.call(
            agent=AGENT_NAME,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema=QualityAssessmentOutput,
            correlation_id=correlation_id,
            validation_context={"fields": {r.definition.name for r in to_assess}},
            deadline=deadline,
        )
        assessments: dict[str, FieldAssessment] = {}
        for item in result.value.assessments:
            if item.field in assessments:
                continue
            assessments[item.field] = FieldAssessment(
                field=item.field,
                level=item.level,
                reason=item.reason,
                evidence=verify_evidence(
                    sources, [EvidenceRef(source=e.source, quote=e.quote) for e in item.evidence]
                ),
            )
        return assessments, result

"""ClassificationAgent: category, subcategory, application and business process of an incident."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationInfo, model_validator

from branchops.agents.base import StructuredCaller, StructuredResult
from branchops.agents.prompts import GUARDRAILS, EvidenceQuote, json_block
from branchops.domain.entities.analysis import Classification
from branchops.domain.enums import Confidence
from branchops.domain.services.evidence import verify_evidence
from branchops.domain.value_objects import EvidenceRef

AGENT_NAME = "classification"
FALLBACK_CATEGORY = "OTHER"


class ClassificationOutput(BaseModel):
    category: str = Field(description="one of the allowed categories")
    subcategory: str | None = Field(default=None, description="one of the allowed subcategories or null")
    application: str | None = Field(
        default=None, description="affected application name if stated, else null"
    )
    business_process: str | None = Field(default=None, description="affected banking process if stated")
    confidence: Confidence
    evidence: list[EvidenceQuote] = Field(default_factory=list, max_length=5)
    rationale: str = Field(default="", max_length=400, description="one or two sentences, no reasoning trace")

    @model_validator(mode="after")
    def _taxonomy(self, info: ValidationInfo) -> ClassificationOutput:
        ctx: dict[str, Any] = info.context or {}
        taxonomy: dict[str, list[str]] | None = ctx.get("taxonomy")
        if taxonomy is None:
            return self
        if self.category not in taxonomy:
            raise ValueError(f"category must be one of {sorted(taxonomy)}")
        allowed = taxonomy[self.category]
        if self.subcategory is not None and self.subcategory not in allowed:
            raise ValueError(f"subcategory for {self.category} must be one of {allowed} or null")
        return self


SYSTEM_PROMPT = (
    GUARDRAILS
    + """
Task: classify the operational incident into the bank's taxonomy.
- Choose exactly one category from <taxonomy>. Use "OTHER" only when no category fits.
- Choose a subcategory from the list of the chosen category, or null if none fits clearly.
- application / business_process: copy names as written in the data, or null if not stated.
- confidence: HIGH only when the data states the problem explicitly; LOW when guessing.
"""
)


class ClassificationAgent:
    def __init__(self, caller: StructuredCaller) -> None:
        self._caller = caller

    def run(
        self,
        *,
        issue_block: str,
        sources: dict[str, str],
        taxonomy: dict[str, list[str]],
        correlation_id: str,
        deadline: float | None = None,
    ) -> tuple[Classification, StructuredResult[ClassificationOutput]]:
        user_prompt = f"{json_block('taxonomy', taxonomy)}\n\n{issue_block}"
        result = self._caller.call(
            agent=AGENT_NAME,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema=ClassificationOutput,
            correlation_id=correlation_id,
            validation_context={"taxonomy": taxonomy},
            deadline=deadline,
        )
        out = result.value
        evidence = verify_evidence(
            sources, [EvidenceRef(source=e.source, quote=e.quote) for e in out.evidence]
        )
        confidence = out.confidence
        if confidence is Confidence.HIGH and not any(e.verified for e in evidence):
            confidence = Confidence.MEDIUM  # unsupported high confidence is not accepted
        if out.category == FALLBACK_CATEGORY:
            confidence = Confidence.LOW
        classification = Classification(
            category=out.category,
            subcategory=out.subcategory,
            application=out.application,
            business_process=out.business_process,
            confidence=confidence,
            evidence=evidence,
            rationale=out.rationale or None,
        )
        return classification, result

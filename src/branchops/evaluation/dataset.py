"""Evaluation dataset format: source records with expert expectations.

A dataset is one JSON file::

    {"name": "...", "version": "1", "description": "...", "cases": [EvalCase, ...]}

Cases are analysed in file order; duplicate expectations may only point to *earlier* cases (the history an
analysis can see). Every expectation is optional: metrics are computed over the cases that state them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from branchops.domain.enums import FieldState, RubricLevel

ExpectedRelationship = Literal["DUPLICATE", "RELATED", "POSSIBLE_COMMON_CAUSE", "UNRELATED"]
Grade = Literal["LOW", "MEDIUM", "HIGH"]


class ExpectedRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(description="Id of an earlier case in the same dataset.")
    relationship: ExpectedRelationship


class ExpertAssessment(BaseModel):
    """Reference assessment by branch-operations experts (used by the score calibration)."""

    model_config = ConfigDict(extra="forbid")

    quality_score: float | None = Field(default=None, ge=0, le=100)
    completeness_score: float | None = Field(default=None, ge=0, le=100)
    grade: Grade | None = None
    field_states: dict[str, FieldState] = Field(default_factory=dict)
    annotators: int = Field(default=1, ge=1)
    notes: str | None = None


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str | None = None
    subcategory: str | None = None
    template_id: str | None = None
    # Values the extraction should find; matching is case-insensitive containment either way.
    fields: dict[str, str] = Field(default_factory=dict)
    # Template fields that are genuinely absent from the record (confirmed MISSING, not UNKNOWN).
    missing_fields: list[str] = Field(default_factory=list)
    rubric_levels: dict[str, RubricLevel] = Field(default_factory=dict)
    relations: list[ExpectedRelation] = Field(default_factory=list)
    prompt_injection: bool | None = None

    @model_validator(mode="after")
    def _disjoint(self) -> Expected:
        overlap = set(self.fields) & set(self.missing_fields)
        if overlap:
            raise ValueError(f"fields both expected and missing: {sorted(overlap)}")
        return self


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    tags: list[str] = Field(default_factory=list)
    description: str | None = None
    source: Literal["jira"] = "jira"
    payload: dict[str, Any] = Field(description="Original Jira webhook payload (synthetic or masked).")
    expected: Expected = Field(default_factory=Expected)
    expert: ExpertAssessment | None = None

    @field_validator("payload")
    @classmethod
    def _has_issue(cls, value: dict[str, Any]) -> dict[str, Any]:
        issue = value.get("issue")
        if not isinstance(issue, dict) or not issue.get("key"):
            raise ValueError("payload.issue.key is required")
        return value

    @property
    def incident_key(self) -> str:
        return f"{self.source}:{self.payload['issue']['key']}"


class EvalDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str | None = None
    cases: list[EvalCase] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> EvalDataset:
        seen: set[str] = set()
        keys: set[str] = set()
        for case in self.cases:
            if case.id in seen:
                raise ValueError(f"duplicate case id {case.id!r}")
            if case.incident_key in keys:
                raise ValueError(f"duplicate incident key {case.incident_key!r} ({case.id})")
            for rel in case.expected.relations:
                if rel.case_id not in seen:
                    raise ValueError(
                        f"case {case.id!r}: relation to {rel.case_id!r} must point to an earlier case"
                    )
            seen.add(case.id)
            keys.add(case.incident_key)
        return self

    def case(self, case_id: str) -> EvalCase:
        for c in self.cases:
            if c.id == case_id:
                return c
        raise KeyError(case_id)

    @property
    def sha256(self) -> str:
        """Content hash: two reports are comparable only when this matches."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_dataset(path: str | Path) -> EvalDataset:
    return EvalDataset.model_validate_json(Path(path).read_text(encoding="utf-8"))

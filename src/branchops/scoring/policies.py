"""Versioned scoring policies.

A policy is part of every stored score: changing any number here requires a new ``version`` so that
historical scores remain explainable.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from branchops.domain.enums import RubricLevel


class UnknownHandling(StrEnum):
    CREDIT = "CREDIT"  # count in denominator, award ``unknown_credit``
    EXCLUDE = "EXCLUDE"  # remove from denominator, like NOT_APPLICABLE


class ScoringPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str = "2026.10-1"
    # Completeness: presence of information.
    sufficient_completeness_credit: float = Field(default=1.0, ge=0, le=1)
    insufficient_completeness_credit: float = Field(default=1.0, ge=0, le=1)
    unknown_completeness_handling: UnknownHandling = UnknownHandling.CREDIT
    unknown_completeness_credit: float = Field(default=0.0, ge=0, le=1)
    # Quality: usefulness of the information present.
    level_credits: dict[RubricLevel, float] = Field(
        default_factory=lambda: {
            RubricLevel.ABSENT: 0.0,
            RubricLevel.POOR: 0.25,
            RubricLevel.ACCEPTABLE: 0.6,
            RubricLevel.GOOD: 1.0,
        }
    )
    unknown_quality_handling: UnknownHandling = UnknownHandling.CREDIT
    unknown_quality_credit: float = Field(default=0.0, ge=0, le=1)
    # Classification of an issue as "low quality" for dashboards/alerts.
    low_quality_threshold: float = Field(default=50.0, ge=0, le=100)
    score_decimals: int = Field(default=2, ge=0, le=4)

    def level_credit(self, level: RubricLevel) -> float:
        return self.level_credits[level]


DEFAULT_POLICY = ScoringPolicy()

"""Versioned scoring policies.

A policy is part of every stored score: changing any number here requires a new ``version`` so that
historical scores remain explainable. Every released policy is registered in ``POLICY_HISTORY``:

* a configuration that names a registered version gets exactly the registered parameters (fields it does not
  set are filled from the registry); the settings validator refuses a configuration that changes any of them
  without a new version (``registry_conflicts``);
* an unregistered version string (a bank-specific policy) is accepted as configured.

So an analysis stored with ``scoring_policy_version: 2026.10-1`` can always be explained with
``POLICY_HISTORY["2026.10-1"]``, and a deployment that pins an old version keeps scoring exactly as before.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from branchops.domain.enums import RubricLevel


class UnknownHandling(StrEnum):
    CREDIT = "CREDIT"  # count in denominator, award ``unknown_credit``
    EXCLUDE = "EXCLUDE"  # remove from denominator, like NOT_APPLICABLE


class ScoringPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str = "2026.10-2"
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
    # Free-text fields whose model-proposed level is capped when the text has no operational anchor (number,
    # error code, quoted message, named device/system/process; ``domain/services/specificity.py``).
    specificity_fields: tuple[str, ...] = ("summary", "description")
    non_specific_level_cap: RubricLevel | None = RubricLevel.POOR

    @model_validator(mode="before")
    @classmethod
    def _registered_defaults(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("version") in POLICY_HISTORY:
            return {**POLICY_HISTORY[data["version"]], **data}
        return data

    def level_credit(self, level: RubricLevel) -> float:
        return self.level_credits[level]


_LEVELS_2026_10: Final = {"ABSENT": 0.0, "POOR": 0.25, "ACCEPTABLE": 0.6, "GOOD": 1.0}

# Every released policy, verbatim. Append only; never edit an entry.
POLICY_HISTORY: Final[dict[str, dict[str, Any]]] = {
    "2026.10-1": {
        "version": "2026.10-1",
        "sufficient_completeness_credit": 1.0,
        "insufficient_completeness_credit": 1.0,
        "unknown_completeness_handling": "CREDIT",
        "unknown_completeness_credit": 0.0,
        "level_credits": _LEVELS_2026_10,
        "unknown_quality_handling": "CREDIT",
        "unknown_quality_credit": 0.0,
        "low_quality_threshold": 50.0,
        "score_decimals": 2,
        "specificity_fields": (),
        "non_specific_level_cap": None,
    },
    # Adds the non-specific text cap for summary/description (gap analysis SCO-1); credits unchanged.
    "2026.10-2": {
        "version": "2026.10-2",
        "sufficient_completeness_credit": 1.0,
        "insufficient_completeness_credit": 1.0,
        "unknown_completeness_handling": "CREDIT",
        "unknown_completeness_credit": 0.0,
        "level_credits": _LEVELS_2026_10,
        "unknown_quality_handling": "CREDIT",
        "unknown_quality_credit": 0.0,
        "low_quality_threshold": 50.0,
        "score_decimals": 2,
        "specificity_fields": ("summary", "description"),
        "non_specific_level_cap": "POOR",
    },
}


def _parse(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    out["level_credits"] = {RubricLevel(k): float(v) for k, v in data["level_credits"].items()}
    out["unknown_completeness_handling"] = UnknownHandling(data["unknown_completeness_handling"])
    out["unknown_quality_handling"] = UnknownHandling(data["unknown_quality_handling"])
    out["specificity_fields"] = tuple(data["specificity_fields"])
    cap = data["non_specific_level_cap"]
    out["non_specific_level_cap"] = RubricLevel(cap) if cap else None
    return out


def registry_conflicts(policy: ScoringPolicy) -> list[str]:
    """Parameters in which ``policy`` differs from the released policy of the same version (empty = fine)."""
    registered = POLICY_HISTORY.get(policy.version)
    if registered is None:
        return []
    expected = ScoringPolicy.model_construct(**_parse(registered))
    return [k for k in type(policy).model_fields if getattr(policy, k) != getattr(expected, k)]


def registered_policy(version: str) -> ScoringPolicy | None:
    """The released policy ``version`` (to explain a historical score), or ``None`` if not registered."""
    return ScoringPolicy.model_validate(POLICY_HISTORY[version]) if version in POLICY_HISTORY else None


DEFAULT_POLICY = ScoringPolicy()

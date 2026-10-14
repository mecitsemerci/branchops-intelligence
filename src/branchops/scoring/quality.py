"""Quality: weighted usefulness of template fields, based on validated rubric levels."""

from __future__ import annotations

from branchops.domain.enums import FieldState, RubricLevel
from branchops.scoring.policies import ScoringPolicy, UnknownHandling

_ORDER = [RubricLevel.ABSENT, RubricLevel.POOR, RubricLevel.ACCEPTABLE, RubricLevel.GOOD]


def cap_level(level: RubricLevel, ceiling: RubricLevel) -> RubricLevel:
    return level if _ORDER.index(level) <= _ORDER.index(ceiling) else ceiling


def validated_level(
    state: FieldState, model_level: RubricLevel | None, has_validation_errors: bool
) -> tuple[RubricLevel | None, str | None]:
    """Apply deterministic constraints to the level proposed by the model.

    Returns ``(level, adjustment_reason)``. ``None`` level means "not scorable" (excluded state).
    """
    if state in (FieldState.NOT_APPLICABLE, FieldState.UNKNOWN):
        return None, None
    if state is FieldState.MISSING:
        if model_level not in (None, RubricLevel.ABSENT):
            return RubricLevel.ABSENT, "field has no value"
        return RubricLevel.ABSENT, None
    proposed = model_level or RubricLevel.ACCEPTABLE
    reason = None if model_level else "not assessed by model; deterministic default"
    if proposed is RubricLevel.ABSENT:
        # A value exists, so ABSENT is inconsistent; the weakest level for present data is POOR.
        return RubricLevel.POOR, "value present; ABSENT raised to POOR"
    if has_validation_errors and proposed is not cap_level(proposed, RubricLevel.POOR):
        return RubricLevel.POOR, "template validation failed"
    if state is FieldState.INSUFFICIENT and proposed is not cap_level(proposed, RubricLevel.POOR):
        return RubricLevel.POOR, "field state is INSUFFICIENT"
    return proposed, reason


def quality_credit(state: FieldState, level: RubricLevel | None, policy: ScoringPolicy) -> float | None:
    match state:
        case FieldState.NOT_APPLICABLE:
            return None
        case FieldState.UNKNOWN:
            if policy.unknown_quality_handling is UnknownHandling.EXCLUDE:
                return None
            return policy.unknown_quality_credit
        case FieldState.MISSING:
            return 0.0
        case _:
            return policy.level_credit(level or RubricLevel.POOR)

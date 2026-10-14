"""Completeness: weighted presence of template fields."""

from __future__ import annotations

from branchops.domain.enums import FieldState
from branchops.scoring.policies import ScoringPolicy, UnknownHandling


def completeness_credit(state: FieldState, policy: ScoringPolicy) -> float | None:
    """Credit in [0, 1], or ``None`` when the field is excluded from the denominator."""
    match state:
        case FieldState.NOT_APPLICABLE:
            return None
        case FieldState.SUFFICIENT:
            return policy.sufficient_completeness_credit
        case FieldState.INSUFFICIENT:
            return policy.insufficient_completeness_credit
        case FieldState.UNKNOWN:
            if policy.unknown_completeness_handling is UnknownHandling.EXCLUDE:
                return None
            return policy.unknown_completeness_credit
        case FieldState.MISSING:
            return 0.0


def weighted_score(items: list[tuple[float, float | None]], decimals: int) -> float | None:
    """``items`` are (weight, credit) pairs; ``None`` credits are excluded. Returns 0..100 or None."""
    counted = [(w, c) for w, c in items if c is not None]
    denominator = sum(w for w, _ in counted)
    if denominator <= 0:
        return None
    score = 100.0 * sum(w * c for w, c in counted) / denominator
    return round(min(100.0, max(0.0, score)), decimals)

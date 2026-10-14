"""Deterministic scoring engine combining template rules, extracted fields and validated rubric levels."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from branchops.domain.entities.analysis import FieldAssessment, FieldScore, ScoreResult
from branchops.domain.entities.issue import CanonicalIssue
from branchops.domain.enums import FieldState, RubricLevel
from branchops.scoring.completeness import completeness_credit, weighted_score
from branchops.scoring.policies import ScoringPolicy
from branchops.scoring.quality import quality_credit, validated_level
from branchops.templates.model import FieldDefinition, IssueTemplate
from branchops.templates.validation import ConditionContext, is_applicable, is_required, validate_value


def condition_context(issue: CanonicalIssue) -> dict[str, str | None]:
    ctx: dict[str, str | None] = {name: issue.value(name) for name in issue.fields}
    ctx.setdefault("category", None)
    ctx.setdefault("subcategory", None)
    return ctx


@dataclass(frozen=True)
class ResolvedField:
    definition: FieldDefinition
    state: FieldState
    applicable: bool
    required: bool
    value: str | None
    validation_errors: tuple[str, ...]


def resolve_field(definition: FieldDefinition, issue: CanonicalIssue, ctx: ConditionContext) -> ResolvedField:
    """Pre-assessment state of a template field (before rubric levels are known)."""
    extracted = issue.field(definition.name)
    applicable = is_applicable(definition, ctx)
    required = applicable and is_required(definition, ctx)
    value = extracted.value if extracted.has_value else None
    if not applicable:
        state = FieldState.NOT_APPLICABLE
    elif extracted.state is FieldState.NOT_APPLICABLE:
        state = (
            FieldState.NOT_APPLICABLE
            if definition.allow_not_applicable and not required
            else FieldState.MISSING
        )
    elif extracted.state is FieldState.UNKNOWN:
        state = FieldState.UNKNOWN
    elif value is None:
        state = FieldState.MISSING
    else:
        state = (
            FieldState.INSUFFICIENT if extracted.state is FieldState.INSUFFICIENT else FieldState.SUFFICIENT
        )
    errors = tuple(validate_value(definition, value)) if state is not FieldState.NOT_APPLICABLE else ()
    if errors and state is FieldState.SUFFICIENT:
        state = FieldState.INSUFFICIENT
    return ResolvedField(definition, state, applicable, required, value, errors)


def resolve_fields(template: IssueTemplate, issue: CanonicalIssue) -> list[ResolvedField]:
    ctx = condition_context(issue)
    return [resolve_field(d, issue, ctx) for d in template.fields]


@dataclass(frozen=True)
class ScoringOutput:
    result: ScoreResult
    assessments: list[FieldAssessment]


def score_issue(
    template: IssueTemplate,
    issue: CanonicalIssue,
    assessments: Mapping[str, FieldAssessment] | None,
    policy: ScoringPolicy,
) -> ScoringOutput:
    """Compute completeness and quality.

    ``assessments=None`` means the quality assessment step was unavailable: completeness is still
    computed, quality is reported as ``None`` instead of being guessed.
    """
    field_scores: list[FieldScore] = []
    validated: list[FieldAssessment] = []
    completeness_items: list[tuple[float, float | None]] = []
    quality_items: list[tuple[float, float | None]] = []

    for resolved in resolve_fields(template, issue):
        d = resolved.definition
        state = resolved.state
        level: RubricLevel | None = None
        if assessments is not None:
            proposal = assessments.get(d.name)
            level, reason = validated_level(
                state, proposal.level if proposal else None, bool(resolved.validation_errors)
            )
            if level is not None and level is RubricLevel.POOR and state is FieldState.SUFFICIENT:
                state = FieldState.INSUFFICIENT
            if level is not None:
                validated.append(
                    FieldAssessment(
                        field=d.name,
                        level=level,
                        reason=proposal.reason if proposal else "",
                        evidence=proposal.evidence if proposal else [],
                        adjusted_from=proposal.level if proposal and proposal.level is not level else None,
                        adjustment_reason=reason,
                    )
                )
        c_credit = completeness_credit(state, policy)
        q_credit = quality_credit(state, level, policy) if assessments is not None else None
        completeness_items.append((d.weight, c_credit))
        quality_items.append((d.weight, q_credit))
        field_scores.append(
            FieldScore(
                field=d.name,
                weight=d.weight,
                state=state,
                applicable=state is not FieldState.NOT_APPLICABLE,
                required=resolved.required,
                completeness_credit=c_credit,
                quality_credit=q_credit,
                rubric_level=level,
                validation_errors=list(resolved.validation_errors),
            )
        )

    result = ScoreResult(
        completeness_score=weighted_score(completeness_items, policy.score_decimals),
        quality_score=(
            weighted_score(quality_items, policy.score_decimals) if assessments is not None else None
        ),
        field_scores=field_scores,
        missing_fields=[f.field for f in field_scores if f.state is FieldState.MISSING],
        insufficient_fields=[f.field for f in field_scores if f.state is FieldState.INSUFFICIENT],
        unknown_fields=[f.field for f in field_scores if f.state is FieldState.UNKNOWN],
        missing_required_fields=[
            f.field
            for f in field_scores
            if f.required and f.state in (FieldState.MISSING, FieldState.UNKNOWN)
        ],
        template_id=template.template_id,
        template_version=template.version,
        scoring_policy_version=policy.version,
    )
    return ScoringOutput(result=result, assessments=validated)

"""Quality-score calibration against expert reference assessments.

Two comparisons, both deterministic:

* **Scoring-rule calibration** (no model involved). The deterministic engine scores the *expert's* field
  states and rubric levels (``gold_scores``) and the result is compared with the expert's own score and
  grade. A disagreement here is a property of the template weights or the scoring policy.
* **System calibration**. The scores of an evaluation run (model + engine) are compared with the expert's.

Metrics: mean absolute error, grade agreement and confusion matrix, field-level rubric agreement (exact and
linearly weighted κ), false high-/low-quality rates (relative to the policy's ``low_quality_threshold``) and a
breakdown by expected category. Grade bands belong to the calibration, not to the scoring policy: they are a
reporting convention and never change a stored score.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from branchops.domain.entities.analysis import AnalysisResult, FieldAssessment
from branchops.domain.entities.issue import CanonicalIssue
from branchops.domain.enums import FieldState, RubricLevel
from branchops.domain.services.masking import SensitiveDataMasker
from branchops.domain.services.snapshot_merge import merge_event
from branchops.domain.value_objects import ExtractedField
from branchops.evaluation.dataset import EvalCase, EvalDataset, Grade
from branchops.evaluation.metrics import (
    accuracy,
    cohen_kappa,
    confusion_matrix,
    mean,
    mean_absolute_error,
    rounded,
)
from branchops.scoring.engine import score_issue
from branchops.scoring.policies import ScoringPolicy
from branchops.templates.registry import TemplateRegistry

if TYPE_CHECKING:
    from branchops.infrastructure.sources.jira.adapter import JiraWebhookAdapter

GRADES: tuple[Grade, ...] = ("LOW", "MEDIUM", "HIGH")
LEVELS = {
    RubricLevel.ABSENT.value: 0,
    RubricLevel.POOR.value: 1,
    RubricLevel.ACCEPTABLE.value: 2,
    RubricLevel.GOOD.value: 3,
}


@dataclass(frozen=True)
class GradeBands:
    """``score < low`` → LOW, ``< high`` → MEDIUM, else HIGH."""

    low: float = 50.0
    high: float = 75.0

    @classmethod
    def from_policy(cls, policy: ScoringPolicy, high: float = 75.0) -> GradeBands:
        return cls(low=policy.low_quality_threshold, high=max(high, policy.low_quality_threshold))

    def grade(self, score: float) -> Grade:
        if score < self.low:
            return "LOW"
        if score < self.high:
            return "MEDIUM"
        return "HIGH"

    def as_dict(self) -> dict[str, float]:
        return {"low": self.low, "high": self.high}


def gold_issue(case: EvalCase, base: CanonicalIssue) -> CanonicalIssue:
    """``base`` with every field the expert specified replaced by the expert's value/state."""
    fields = dict(base.fields)
    for name, value in case.expected.fields.items():
        fields[name] = ExtractedField(value=value, state=FieldState.SUFFICIENT)
    for name in case.expected.missing_fields:
        fields[name] = ExtractedField.missing()
    if case.expert is not None:
        for name, state in case.expert.field_states.items():
            current = fields.get(name, ExtractedField.missing())
            if state in (FieldState.SUFFICIENT, FieldState.INSUFFICIENT) and current.has_value:
                fields[name] = ExtractedField(value=current.value, state=state)
            elif state in (FieldState.MISSING, FieldState.UNKNOWN, FieldState.NOT_APPLICABLE):
                fields[name] = ExtractedField(state=state)
    if case.expected.category:
        fields["category"] = ExtractedField(value=case.expected.category, state=FieldState.SUFFICIENT)
    if case.expected.subcategory:
        fields["subcategory"] = ExtractedField(value=case.expected.subcategory, state=FieldState.SUFFICIENT)
    return base.model_copy(update={"fields": fields})


def base_issue(case: EvalCase, adapter: JiraWebhookAdapter, masker: SensitiveDataMasker) -> CanonicalIssue:
    """Canonical issue holding only what the source states directly (summary, description, mapped fields)."""
    event = adapter.normalize(case.payload, transform=lambda t: masker.mask(t).text)
    snap = merge_event(None, event).snapshot
    fields: dict[str, ExtractedField] = {}
    if snap.summary:
        fields["summary"] = ExtractedField(value=snap.summary, state=FieldState.SUFFICIENT)
    if snap.description:
        fields["description"] = ExtractedField(value=snap.description, state=FieldState.SUFFICIENT)
    for name, value in snap.mapped_fields.items():
        fields[name] = ExtractedField(value=value, state=FieldState.SUFFICIENT)
    return CanonicalIssue(
        incident_key=snap.incident_key, source=snap.source, source_created_at=snap.created_at, fields=fields
    )


def gold_scores(
    case: EvalCase,
    result: AnalysisResult | None,
    registry: TemplateRegistry,
    policy: ScoringPolicy,
    base: CanonicalIssue | None = None,
) -> dict[str, Any] | None:
    """Engine scores for the expert's inputs; ``None`` without a template or any expert input."""
    exp = case.expected
    template_id = exp.template_id or (result.template_id if result else None)
    template = next((t for t in registry.latest_published() if t.template_id == template_id), None)
    if template is None:
        return None
    start = base or (result.canonical_issue if result and result.canonical_issue else None)
    if start is None:
        return None
    issue = gold_issue(case, start)
    assessments = (
        {name: FieldAssessment(field=name, level=level) for name, level in exp.rubric_levels.items()}
        if exp.rubric_levels
        else None
    )
    out = score_issue(template, issue, assessments, policy)
    return {
        "template": f"{template.template_id}@{template.version}",
        "completeness": out.result.completeness_score,
        "quality": out.result.quality_score,
        "levels": {a.field: a.level.value for a in out.assessments},
        "adjusted": {a.field: a.adjustment_reason for a in out.assessments if a.adjustment_reason},
    }


def _calibrate(rows: list[dict[str, Any]], bands: GradeBands, threshold: float) -> dict[str, Any]:
    scored = [r for r in rows if r["system"] is not None and r["expert"] is not None]
    pairs = [(r["system"], r["expert"]) for r in scored]
    grade_pairs = [(bands.grade(s), r["expert_grade"]) for r, (s, _) in zip(scored, pairs, strict=True)]
    level_pairs = [p for r in rows for p in r["level_pairs"]]
    expert_low = [r for r in scored if r["expert"] < threshold]
    expert_ok = [r for r in scored if r["expert"] >= threshold]
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in scored:
        by_category[r["category"] or "UNLABELLED"].append(r)
    return {
        "n": len(scored),
        "mae": rounded(mean_absolute_error(pairs), 2),
        "mean_signed_error": rounded(mean(s - e for s, e in pairs), 2),
        "grade_agreement": rounded(accuracy(grade_pairs)),
        "grade_confusion": confusion_matrix([(p, e) for p, e in grade_pairs], GRADES),
        "field_rubric_agreement": rounded(accuracy(level_pairs)),
        "field_rubric_weighted_kappa": rounded(cohen_kappa(level_pairs, LEVELS)) if level_pairs else None,
        "field_rubric_confusion": confusion_matrix(level_pairs, list(LEVELS)),
        "false_high_quality_rate": rounded(
            sum(1 for r in expert_low if r["system"] >= threshold) / len(expert_low)
        )
        if expert_low
        else None,
        "false_low_quality_rate": rounded(
            sum(1 for r in expert_ok if r["system"] < threshold) / len(expert_ok)
        )
        if expert_ok
        else None,
        "by_category": {
            cat: {
                "n": len(items),
                "mae": rounded(mean_absolute_error([(i["system"], i["expert"]) for i in items]), 2),
                "grade_agreement": rounded(
                    accuracy([(bands.grade(i["system"]), i["expert_grade"]) for i in items])
                ),
            }
            for cat, items in sorted(by_category.items())
        },
        "largest_deviations": [
            {
                "case_id": r["case_id"],
                "system": r["system"],
                "expert": r["expert"],
                "delta": round(r["system"] - r["expert"], 2),
            }
            for r in sorted(scored, key=lambda r: -abs(r["system"] - r["expert"]))[:10]
        ],
    }


def calibrate(
    dataset: EvalDataset,
    registry: TemplateRegistry,
    policy: ScoringPolicy,
    adapter: JiraWebhookAdapter,
    masker: SensitiveDataMasker,
    *,
    results: dict[str, AnalysisResult] | None = None,
    bands: GradeBands | None = None,
) -> dict[str, Any]:
    """Calibration report. ``results`` (case id → analysis) adds the system calibration of a run."""
    bands = bands or GradeBands.from_policy(policy)
    rule_rows: list[dict[str, Any]] = []
    system_rows: list[dict[str, Any]] = []
    for case in dataset.cases:
        if case.expert is None or case.expert.quality_score is None:
            continue
        expert_grade = case.expert.grade or bands.grade(case.expert.quality_score)
        gold = gold_scores(case, None, registry, policy, base=base_issue(case, adapter, masker))
        common = {
            "case_id": case.id,
            "category": case.expected.category,
            "expert": case.expert.quality_score,
            "expert_grade": expert_grade,
        }
        rule_rows.append({**common, "system": gold["quality"] if gold else None, "level_pairs": []})
        result = (results or {}).get(case.id)
        if result is not None:
            levels = {a.field: a.level.value for a in result.assessments}
            level_pairs = [
                (levels[name], level.value)
                for name, level in case.expected.rubric_levels.items()
                if name in levels
            ]
            system_rows.append(
                {
                    **common,
                    "system": result.scores.quality_score if result.scores else None,
                    "level_pairs": level_pairs,
                }
            )
    report: dict[str, Any] = {
        "scoring_policy_version": policy.version,
        "grade_bands": bands.as_dict(),
        "low_quality_threshold": policy.low_quality_threshold,
        "scoring_rules": _calibrate(rule_rows, bands, policy.low_quality_threshold),
    }
    if results is not None:
        report["system"] = _calibrate(system_rows, bands, policy.low_quality_threshold)
    return report

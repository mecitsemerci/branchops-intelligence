from __future__ import annotations

import pytest

from branchops.domain.entities.analysis import FieldAssessment
from branchops.domain.entities.issue import CanonicalIssue
from branchops.domain.enums import FieldState, RubricLevel
from branchops.domain.value_objects import ExtractedField
from branchops.scoring.completeness import completeness_credit, weighted_score
from branchops.scoring.engine import score_issue
from branchops.scoring.policies import ScoringPolicy, UnknownHandling
from branchops.scoring.quality import quality_credit, validated_level
from branchops.templates.model import IssueTemplate

POLICY = ScoringPolicy()


def _template(**field_overrides: dict[str, object]) -> IssueTemplate:
    rubric = {"criteria": "c", "good": "g", "acceptable": "a", "poor": "p"}
    fields = [
        {
            "name": "summary",
            "label": "Özet",
            "weight": 10,
            "required": True,
            "rubric": rubric,
            "recommendation": "özet ekle",
        },
        {
            "name": "branch_code",
            "label": "Şube kodu",
            "weight": 20,
            "required": True,
            "rubric": rubric,
            "recommendation": "şube kodu ekle",
            "validation": {"pattern": r"^\d{3,5}$"},
        },
        {
            "name": "error_details",
            "label": "Hata",
            "weight": 30,
            "rubric": rubric,
            "recommendation": "hata ekle",
        },
        {
            "name": "environment",
            "label": "Ortam",
            "weight": 40,
            "rubric": rubric,
            "recommendation": "ortam ekle",
            "allow_not_applicable": True,
        },
    ]
    for f in fields:
        f.update(field_overrides.get(str(f["name"]), {}))
    return IssueTemplate.model_validate(
        {
            "template_id": "unit-test",
            "version": "1.0.0",
            "name": "t",
            "category": "APPLICATION",
            "fields": fields,
        }
    )


def _issue(**fields: ExtractedField) -> CanonicalIssue:
    return CanonicalIssue(issue_key="T-1", fields=fields)


def present(value: str) -> ExtractedField:
    return ExtractedField(value=value, state=FieldState.SUFFICIENT)


def levels(**lv: RubricLevel) -> dict[str, FieldAssessment]:
    return {k: FieldAssessment(field=k, level=v) for k, v in lv.items()}


class TestPrimitives:
    def test_weighted_score_bounds_and_rounding(self) -> None:
        assert weighted_score([(1, 1.0), (2, 0.0)], 2) == 33.33
        assert weighted_score([(1, 1.0)], 2) == 100.0
        assert weighted_score([(1, 0.0)], 2) == 0.0

    def test_weighted_score_excludes_none_and_handles_empty_denominator(self) -> None:
        assert weighted_score([(5, None), (5, 1.0)], 2) == 100.0
        assert weighted_score([(5, None)], 2) is None
        assert weighted_score([], 2) is None

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (FieldState.SUFFICIENT, 1.0),
            (FieldState.INSUFFICIENT, 1.0),
            (FieldState.MISSING, 0.0),
            (FieldState.UNKNOWN, 0.0),
            (FieldState.NOT_APPLICABLE, None),
        ],
    )
    def test_completeness_credit_defaults(self, state: FieldState, expected: float | None) -> None:
        assert completeness_credit(state, POLICY) == expected

    def test_unknown_policies(self) -> None:
        exclude = ScoringPolicy(
            unknown_completeness_handling=UnknownHandling.EXCLUDE,
            unknown_quality_handling=UnknownHandling.EXCLUDE,
        )
        partial = ScoringPolicy(unknown_completeness_credit=0.5, unknown_quality_credit=0.25)
        assert completeness_credit(FieldState.UNKNOWN, exclude) is None
        assert quality_credit(FieldState.UNKNOWN, None, exclude) is None
        assert completeness_credit(FieldState.UNKNOWN, partial) == 0.5
        assert quality_credit(FieldState.UNKNOWN, None, partial) == 0.25

    def test_validated_level_rules(self) -> None:
        assert validated_level(FieldState.MISSING, RubricLevel.GOOD, False) == (
            RubricLevel.ABSENT,
            "field has no value",
        )
        assert validated_level(FieldState.SUFFICIENT, RubricLevel.GOOD, True)[0] is RubricLevel.POOR
        assert validated_level(FieldState.INSUFFICIENT, RubricLevel.ACCEPTABLE, False)[0] is RubricLevel.POOR
        assert validated_level(FieldState.SUFFICIENT, RubricLevel.ABSENT, False)[0] is RubricLevel.POOR
        assert validated_level(FieldState.SUFFICIENT, None, False) == (
            RubricLevel.ACCEPTABLE,
            "not assessed by model; deterministic default",
        )
        assert validated_level(FieldState.NOT_APPLICABLE, RubricLevel.GOOD, False) == (None, None)
        assert validated_level(FieldState.SUFFICIENT, RubricLevel.GOOD, False) == (RubricLevel.GOOD, None)


class TestEngine:
    def test_all_fields_good_scores_100(self) -> None:
        issue = _issue(
            summary=present("EFT ekranında hata"),
            branch_code=present("1234"),
            error_details=present("EFT-4012"),
            environment=present("SB1234-GISE03"),
        )
        lv = levels(
            summary=RubricLevel.GOOD,
            branch_code=RubricLevel.GOOD,
            error_details=RubricLevel.GOOD,
            environment=RubricLevel.GOOD,
        )
        out = score_issue(_template(), issue, lv, POLICY).result
        assert out.completeness_score == 100.0
        assert out.quality_score == 100.0
        assert out.missing_fields == []
        assert out.template_version == "1.0.0"
        assert out.scoring_policy_version == POLICY.version

    def test_missing_fields_reduce_completeness_by_weight(self) -> None:
        issue = _issue(summary=present("EFT ekranında hata"), branch_code=present("1234"))
        lv = levels(summary=RubricLevel.GOOD, branch_code=RubricLevel.GOOD)
        out = score_issue(_template(), issue, lv, POLICY).result
        assert out.completeness_score == 30.0  # (10 + 20) / 100
        assert out.quality_score == 30.0
        assert set(out.missing_fields) == {"error_details", "environment"}
        assert out.missing_required_fields == []

    def test_not_applicable_excluded_from_denominator(self) -> None:
        issue = _issue(
            summary=present("EFT ekranında hata"),
            branch_code=present("1234"),
            error_details=present("EFT-4012"),
            environment=ExtractedField(state=FieldState.NOT_APPLICABLE),
        )
        lv = levels(summary=RubricLevel.GOOD, branch_code=RubricLevel.GOOD, error_details=RubricLevel.GOOD)
        out = score_issue(_template(), issue, lv, POLICY).result
        assert out.completeness_score == 100.0
        env = next(f for f in out.field_scores if f.field == "environment")
        assert env.applicable is False and env.completeness_credit is None

    def test_not_applicable_rejected_when_template_disallows(self) -> None:
        issue = _issue(error_details=ExtractedField(state=FieldState.NOT_APPLICABLE))
        out = score_issue(_template(), issue, None, POLICY).result
        assert "error_details" in out.missing_fields

    def test_conditional_applicability(self) -> None:
        template = _template(
            environment={"applicable_when": {"field": "category", "op": "equals", "value": "NETWORK"}}
        )
        issue = _issue(category=present("APPLICATION"))
        out = score_issue(template, issue, None, POLICY).result
        env = next(f for f in out.field_scores if f.field == "environment")
        assert env.state is FieldState.NOT_APPLICABLE

    def test_conditional_requirement(self) -> None:
        template = _template(error_details={"required_when": {"field": "branch_code", "op": "present"}})
        out = score_issue(template, _issue(branch_code=present("1234")), None, POLICY).result
        assert "error_details" in out.missing_required_fields

    def test_validation_failure_makes_field_insufficient_and_caps_quality(self) -> None:
        issue = _issue(branch_code=present("Kadıköy şubesi"))
        out = score_issue(_template(), issue, levels(branch_code=RubricLevel.GOOD), POLICY)
        bc = next(f for f in out.result.field_scores if f.field == "branch_code")
        assert bc.state is FieldState.INSUFFICIENT
        assert bc.rubric_level is RubricLevel.POOR
        assert bc.completeness_credit == 1.0
        assert bc.quality_credit == POLICY.level_credit(RubricLevel.POOR)
        assert bc.validation_errors
        assessment = next(a for a in out.assessments if a.field == "branch_code")
        assert assessment.adjusted_from is RubricLevel.GOOD

    def test_poor_level_marks_field_insufficient(self) -> None:
        issue = _issue(summary=present("hata"))
        out = score_issue(_template(), issue, levels(summary=RubricLevel.POOR), POLICY).result
        assert "summary" in out.insufficient_fields

    def test_unknown_field_counts_with_default_policy(self) -> None:
        issue = _issue(summary=present("EFT ekranında hata"), branch_code=ExtractedField.unknown())
        out = score_issue(_template(), issue, levels(summary=RubricLevel.GOOD), POLICY).result
        assert out.unknown_fields == ["branch_code"]
        assert "branch_code" in out.missing_required_fields
        assert out.completeness_score == 10.0

    def test_unknown_excluded_policy_changes_denominator(self) -> None:
        policy = ScoringPolicy(version="t", unknown_completeness_handling=UnknownHandling.EXCLUDE)
        issue = _issue(summary=present("EFT ekranında hata"), branch_code=ExtractedField.unknown())
        out = score_issue(_template(), issue, None, policy).result
        assert out.completeness_score == 12.5  # 10 / (10 + 30 + 40)

    def test_quality_is_none_without_assessment(self) -> None:
        out = score_issue(_template(), _issue(summary=present("x y z w")), None, POLICY).result
        assert out.quality_score is None
        assert out.completeness_score is not None

    def test_quality_weighted_by_levels(self) -> None:
        issue = _issue(
            summary=present("EFT ekranında hata"),
            branch_code=present("1234"),
            error_details=present("EFT-4012"),
            environment=present("SB1234"),
        )
        lv = levels(
            summary=RubricLevel.ACCEPTABLE,
            branch_code=RubricLevel.GOOD,
            error_details=RubricLevel.POOR,
            environment=RubricLevel.GOOD,
        )
        out = score_issue(_template(), issue, lv, POLICY).result
        expected = round(100 * (10 * 0.6 + 20 * 1.0 + 30 * 0.25 + 40 * 1.0) / 100, 2)
        assert out.quality_score == expected

    def test_scores_are_deterministic(self) -> None:
        issue = _issue(summary=present("EFT ekranında hata"), branch_code=present("1234"))
        lv = levels(summary=RubricLevel.GOOD, branch_code=RubricLevel.ACCEPTABLE)
        results = {score_issue(_template(), issue, lv, POLICY).result.model_dump_json() for _ in range(5)}
        assert len(results) == 1

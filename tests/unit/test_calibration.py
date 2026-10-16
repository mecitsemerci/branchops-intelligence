"""Score calibration: generic vs specific text, policy history, UNKNOWN vs MISSING, calibration metrics."""

from __future__ import annotations

import json
from typing import Any

import pytest

from branchops.agents.prompts import parse_json_block
from branchops.application.interfaces.model_provider import CompletionRequest
from branchops.domain.entities.analysis import FieldAssessment
from branchops.domain.entities.issue import CanonicalIssue
from branchops.domain.enums import FieldState, RubricLevel
from branchops.domain.services.masking import SensitiveDataMasker
from branchops.domain.services.specificity import specificity
from branchops.domain.value_objects import ExtractedField
from branchops.evaluation.calibration import GradeBands, calibrate
from branchops.evaluation.dataset import EvalDataset, load_dataset
from branchops.evaluation.runner import EvaluationRunner
from branchops.infrastructure.sources.jira.adapter import JiraWebhookAdapter
from branchops.scoring.engine import score_issue
from branchops.scoring.policies import POLICY_HISTORY, ScoringPolicy, registered_policy, registry_conflicts
from branchops.templates.registry import TemplateRegistry
from tests.helpers import ROOT, ScriptedProvider, make_settings

DATASET = ROOT / "samples" / "evaluation" / "turkish-branch-incidents.json"
GENERIC = "Uygulama çalışmıyor."
PADDED = (
    "Sabahtan beri sistemde bir sorun var, işlemler düzgün yürümüyor, personel ve müşteriler mağdur oluyor, "
    "lütfen en kısa sürede ilgilenin ve dönüş yapın."
)
SPECIFIC = (
    "145 numaralı şubede para yatırma işlemi onaylandığında DCS Host yanıt vermiyor, işlem TMO-3001 zaman "
    "aşımı hatasıyla düşüyor."
)


def lenient(request: CompletionRequest) -> str:
    """A model that rates every field it is asked about as GOOD (the failure mode calibration must catch)."""
    fields = parse_json_block(request.messages[-1].content, "fields_to_assess") or []
    return json.dumps(
        {"assessments": [{"field": f["field"], "level": "GOOD", "reason": "iyi"} for f in fields]}
    )


def _description_only(template: Any, text: str, policy: ScoringPolicy) -> tuple[float | None, RubricLevel]:
    issue = CanonicalIssue(
        incident_key="jira:T-1",
        fields={
            "summary": ExtractedField(value=text, state=FieldState.SUFFICIENT),
            "description": ExtractedField(value=text, state=FieldState.SUFFICIENT),
        },
    )
    proposals = {n: FieldAssessment(field=n, level=RubricLevel.GOOD) for n in ("summary", "description")}
    out = score_issue(template, issue, proposals, policy)
    level = next(a.level for a in out.assessments if a.field == "description")
    return out.result.quality_score, level


class TestSpecificity:
    @pytest.mark.parametrize(
        "text", [GENERIC, PADDED, "Program donuyor", "Kart sorunu", "Ekranda bir uyarı çıktı"]
    )
    def test_non_specific(self, text: str) -> None:
        assert not specificity(text).specific

    @pytest.mark.parametrize(
        "text",
        [
            SPECIFIC,
            "EFT-504 hatası",
            "Dekont yazıcısı basmıyor",
            "Ekranda 'servis kullanılamıyor' yazıyor",
            "EFT'de hata",
        ],
    )
    def test_specific(self, text: str) -> None:
        assert specificity(text).specific

    def test_short_acronyms_do_not_match_inside_words(self) -> None:
        assert not specificity("Posta gelmiyor").specific  # "pos"
        assert not specificity("Fasulye").specific  # "fast"


class TestGenericVersusSpecific:
    def test_lenient_model_cannot_lift_generic_text(self, registry: TemplateRegistry) -> None:
        template = registry.get("generic-operational-incident", "1.0.0")
        policy = ScoringPolicy()  # 2026.10-2
        generic_q, generic_level = _description_only(template, GENERIC, policy)
        padded_q, padded_level = _description_only(template, PADDED, policy)
        specific_q, specific_level = _description_only(template, SPECIFIC, policy)
        assert generic_level is RubricLevel.POOR and padded_level is RubricLevel.POOR
        assert specific_level is RubricLevel.GOOD
        assert generic_q is not None and padded_q is not None and specific_q is not None
        assert generic_q < specific_q and padded_q < specific_q

    def test_released_policy_2026_10_1_still_scores_as_before(self, registry: TemplateRegistry) -> None:
        """Historical analyses stay interpretable: the old version reproduces the old (uncapped) level."""
        template = registry.get("generic-operational-incident", "1.0.0")
        old = registered_policy("2026.10-1")
        assert old is not None and old.non_specific_level_cap is None
        _, padded_level_old = _description_only(template, PADDED, old)
        assert padded_level_old is RubricLevel.GOOD  # the defect the new version fixes (SCO-1)
        _, padded_level_new = _description_only(template, PADDED, ScoringPolicy())
        assert padded_level_new is RubricLevel.POOR

    def test_end_to_end_with_a_lenient_model(self, registry: TemplateRegistry) -> None:
        dataset = load_dataset(DATASET)
        cases = [
            c
            for c in dataset.model_dump(mode="json")["cases"]
            if c["id"] in ("cash-deposit-host-timeout", "generic-not-working", "padded-vague-description")
        ]
        small = EvalDataset.model_validate({**dataset.model_dump(mode="json"), "cases": cases})
        provider = ScriptedProvider({"QualityAssessmentOutput": [lenient] * 3})
        settings = make_settings()
        run = EvaluationRunner(settings, registry, provider).run(small)
        quality = {
            c.case.id: c.result.scores.quality_score for c in run.cases if c.result and c.result.scores
        }
        assert quality["generic-not-working"] < 30
        assert quality["padded-vague-description"] < 30
        assert quality["cash-deposit-host-timeout"] > quality["padded-vague-description"] + 30


class TestPolicyHistory:
    def test_default_policy_is_registered_verbatim(self) -> None:
        assert registered_policy(ScoringPolicy().version) == ScoringPolicy()
        for version in POLICY_HISTORY:
            policy = registered_policy(version)
            assert policy is not None and registry_conflicts(policy) == []

    def test_config_naming_an_old_version_gets_its_parameters(self) -> None:
        settings = make_settings(scoring={"version": "2026.10-1"})
        assert settings.scoring.non_specific_level_cap is None
        assert settings.scoring.specificity_fields == ()

    def test_changing_a_released_policy_without_a_new_version_is_refused(self) -> None:
        with pytest.raises(ValueError, match="released policy"):
            make_settings(scoring={"version": "2026.10-2", "low_quality_threshold": 40})
        custom = make_settings(scoring={"version": "bank-2027.01", "low_quality_threshold": 40})
        assert custom.scoring.low_quality_threshold == 40


class TestUnknownVersusMissing:
    def test_unknown_is_not_reported_as_confirmed_missing(self, registry: TemplateRegistry) -> None:
        template = registry.get("transaction-incident", "1.0.0")
        issue = CanonicalIssue(
            incident_key="jira:T-2",
            fields={
                "summary": ExtractedField(
                    value="EFT gönderiminde EFT-504 hatası", state=FieldState.SUFFICIENT
                ),
                "branch_code": ExtractedField.unknown("extraction failed"),
                "application": ExtractedField.missing(),
            },
        )
        result = score_issue(template, issue, None, ScoringPolicy()).result
        assert (
            "branch_code" in result.unknown_required
            and "branch_code" not in result.missing_required_confirmed
        )
        assert (
            "application" in result.missing_required_confirmed
            and "application" not in result.unknown_required
        )
        assert "branch_code" not in result.missing_fields and "branch_code" in result.unknown_fields
        # Compatibility: the combined list still asks for both.
        assert {"branch_code", "application"} <= set(result.missing_required_fields)

    def test_not_applicable_is_excluded_and_insufficient_is_capped(self, registry: TemplateRegistry) -> None:
        template = registry.get("network-incident", "1.0.0")
        issue = CanonicalIssue(
            incident_key="jira:T-3",
            fields={
                "summary": ExtractedField(value=SPECIFIC, state=FieldState.SUFFICIENT),
                "application": ExtractedField(state=FieldState.NOT_APPLICABLE),
                "error_details": ExtractedField(value="hata", state=FieldState.INSUFFICIENT),
            },
        )
        proposals = {"error_details": FieldAssessment(field="error_details", level=RubricLevel.GOOD)}
        out = score_issue(template, issue, proposals, ScoringPolicy())
        scores = {f.field: f for f in out.result.field_scores}
        assert (
            scores["application"].completeness_credit is None and scores["application"].quality_credit is None
        )
        assert scores["error_details"].rubric_level is RubricLevel.POOR


class TestCalibrationReport:
    def test_metrics_are_deterministic_and_complete(self, registry: TemplateRegistry) -> None:
        dataset = load_dataset(DATASET)
        settings = make_settings()
        adapter = JiraWebhookAdapter(settings.jira.field_mapping)
        masker = SensitiveDataMasker()
        first = calibrate(dataset, registry, settings.scoring, adapter, masker)
        second = calibrate(dataset, registry, settings.scoring, adapter, masker)
        assert first == second
        rules = first["scoring_rules"]
        assert rules["n"] > 5 and rules["mae"] is not None
        assert set(rules["grade_confusion"]) == {"LOW", "MEDIUM", "HIGH"}
        assert {"false_high_quality_rate", "false_low_quality_rate", "by_category"} <= set(rules)
        # The generic record is never graded above LOW by the deterministic rules.
        assert all(
            d["case_id"] != "generic-not-working" or d["system"] < 50 for d in rules["largest_deviations"]
        )

    def test_system_calibration_with_a_run(self, registry: TemplateRegistry) -> None:
        dataset = load_dataset(DATASET)
        settings = make_settings()
        from branchops.infrastructure.llm.mock_provider import MockModelProvider

        run = EvaluationRunner(settings, registry, MockModelProvider()).run(dataset)
        results = {c.case.id: c.result for c in run.cases if c.result is not None}
        report = calibrate(
            dataset, registry, settings.scoring, JiraWebhookAdapter(), SensitiveDataMasker(), results=results
        )
        system = report["system"]
        assert system["n"] == len([c for c in dataset.cases if c.expert])
        assert system["field_rubric_agreement"] is not None

    def test_grade_bands(self) -> None:
        bands = GradeBands.from_policy(ScoringPolicy())
        assert (bands.grade(49.9), bands.grade(50), bands.grade(75)) == ("LOW", "MEDIUM", "HIGH")

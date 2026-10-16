"""Evaluation framework: dataset format, metrics, record/replay reproducibility, error attribution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from branchops.application.interfaces.model_provider import CompletionRequest, ModelUnavailableError
from branchops.evaluation.dataset import EvalDataset, load_dataset
from branchops.evaluation.metrics import PRF, accuracy, cohen_kappa, confusion_matrix, macro_f1, percentile
from branchops.evaluation.providers import RecordingMissError, RecordingProvider, ReplayProvider
from branchops.evaluation.report import build_report, render_markdown
from branchops.evaluation.runner import EvaluationRunner
from branchops.infrastructure.llm.mock_provider import MockModelProvider
from branchops.templates.registry import TemplateRegistry
from tests.helpers import ROOT, ScriptedProvider, make_settings

DATASET = ROOT / "samples" / "evaluation" / "turkish-branch-incidents.json"
SETTINGS = make_settings()


@pytest.fixture(scope="module")
def dataset() -> EvalDataset:
    return load_dataset(DATASET)


@pytest.fixture(scope="module")
def mock_report(registry: TemplateRegistry, dataset: EvalDataset) -> dict[str, Any]:
    run = EvaluationRunner(SETTINGS, registry, MockModelProvider(), label="mock").run(dataset)
    return build_report(run, registry, SETTINGS)


class TestMetrics:
    def test_prf_and_accuracy(self) -> None:
        prf = PRF(tp=3, fp=1, fn=2)
        assert prf.precision == 0.75 and prf.recall == 0.6
        assert prf.f1 == pytest.approx(2 * 0.75 * 0.6 / 1.35)
        assert PRF().precision is None and accuracy([]) is None
        assert accuracy([("a", "a"), ("b", "a")]) == 0.5

    def test_macro_f1_weights_labels_equally(self) -> None:
        pairs = [("A", "A")] * 8 + [("A", "B"), ("B", "B")]
        # A: P=8/9 R=1 ; B: P=1 R=0.5
        expected = ((2 * (8 / 9) / (1 + 8 / 9)) + (2 * 0.5 / 1.5)) / 2
        assert macro_f1(pairs) == pytest.approx(expected)

    def test_kappa_and_confusion(self) -> None:
        perfect = [("GOOD", "GOOD"), ("POOR", "POOR"), ("ABSENT", "ABSENT")]
        weights = {"ABSENT": 0, "POOR": 1, "ACCEPTABLE": 2, "GOOD": 3}
        assert cohen_kappa(perfect, weights) == 1.0
        near = [("GOOD", "ACCEPTABLE"), ("POOR", "POOR"), ("ABSENT", "ABSENT"), ("GOOD", "GOOD")]
        far = [("GOOD", "ABSENT"), ("POOR", "POOR"), ("ABSENT", "ABSENT"), ("GOOD", "GOOD")]
        k_near, k_far = cohen_kappa(near, weights), cohen_kappa(far, weights)
        assert k_near is not None and k_far is not None and k_near > k_far
        matrix = confusion_matrix([("HIGH", "LOW"), ("LOW", "LOW")], ["LOW", "MEDIUM", "HIGH"])
        assert matrix["LOW"] == {"LOW": 1, "MEDIUM": 0, "HIGH": 1}
        assert percentile([5, 1, 3, 2, 4], 95) == 5 and percentile([], 50) is None


class TestDataset:
    def test_shipped_dataset_covers_the_scenarios(self, dataset: EvalDataset) -> None:
        tags = {t for c in dataset.cases for t in c.tags}
        assert {
            "application_freeze",
            "money_counter",
            "printer",
            "scanner",
            "ldap_authentication",
            "transaction_timeout",
            "network",
            "integration",
            "missing_branch",
            "ambiguous",
            "similar_unrelated",
            "prompt_injection",
            "turkish_synonyms",
            "generic_description",
        } <= tags
        assert len(dataset.sha256) == 64

    def test_relations_must_point_backwards(self, dataset: EvalDataset) -> None:
        data = dataset.model_dump(mode="json")
        data["cases"][0]["expected"]["relations"] = [
            {"case_id": data["cases"][1]["id"], "relationship": "RELATED"}
        ]
        with pytest.raises(ValueError, match="earlier case"):
            EvalDataset.model_validate(data)

    def test_field_cannot_be_both_expected_and_missing(self, dataset: EvalDataset) -> None:
        data = dataset.model_dump(mode="json")
        data["cases"][0]["expected"]["missing_fields"].append("branch_code")
        with pytest.raises(ValueError, match="both expected and missing"):
            EvalDataset.model_validate(data)


class TestRun:
    def test_report_carries_versions_and_all_metric_groups(self, mock_report: dict[str, Any]) -> None:
        meta = mock_report["metadata"]
        assert meta["model_id"] == "mock-rule-based"
        assert meta["prompt_version"] == SETTINGS.workflow.prompt_version
        assert meta["scoring_policy_version"] == SETTINGS.scoring.version
        assert "transaction-incident@1.0.0" in meta["templates"]
        metrics = mock_report["metrics"]
        assert metrics["failed_cases"] == 0
        for group in (
            "classification",
            "extraction",
            "missing_fields",
            "rubric_levels",
            "structured_output",
            "similarity",
            "prompt_injection",
            "latency_ms",
            "tokens",
            "errors_by_layer",
        ):
            assert group in metrics
        assert metrics["prompt_injection"]["detection_accuracy"] == 1.0  # incl. the benign "talimat" case
        assert metrics["tokens"]["total"] > 0
        assert set(metrics["errors_by_layer"]) <= {"model", "template", "scoring", "similarity", "detection"}
        md = render_markdown([mock_report])
        assert "Classification macro F1" in md and "say nothing about GLM-5.3-Flash" in md

    def test_replay_of_a_recording_reproduces_every_metric(
        self, registry: TemplateRegistry, dataset: EvalDataset, tmp_path: Path
    ) -> None:
        recorder = RecordingProvider(MockModelProvider())
        recorded = build_report(
            EvaluationRunner(SETTINGS, registry, recorder).run(dataset), registry, SETTINGS
        )
        path = tmp_path / "rec.json"
        recorder.save(path)
        assert "Uygulama çalışmıyor" not in path.read_text(encoding="utf-8")  # outputs only, never prompts
        replay = ReplayProvider.load(path)
        first = build_report(EvaluationRunner(SETTINGS, registry, replay).run(dataset), registry, SETTINGS)
        second = build_report(
            EvaluationRunner(SETTINGS, registry, ReplayProvider.load(path)).run(dataset), registry, SETTINGS
        )
        assert first["metrics"] == recorded["metrics"] == second["metrics"]
        assert first["cases"] == second["cases"]

    def test_replay_is_strict(self) -> None:
        replay = ReplayProvider({"format": "branchops-eval-recording/1", "model_id": "m", "calls": {}})
        request = CompletionRequest(messages=[], agent="classify", correlation_id="eval:x", schema_name="S")
        with pytest.raises(RecordingMissError):
            replay.complete(request)
        with pytest.raises(ValueError, match="not a BranchOps evaluation recording"):
            ReplayProvider({"format": "other"})

    def test_model_outage_is_reported_not_fatal(
        self, registry: TemplateRegistry, dataset: EvalDataset
    ) -> None:
        small = EvalDataset.model_validate(
            {**dataset.model_dump(mode="json"), "cases": [dataset.model_dump(mode="json")["cases"][0]]}
        )

        def down(_: CompletionRequest) -> Exception:
            return ModelUnavailableError("connection refused")

        provider = ScriptedProvider({"ClassificationOutput": [down] * 10, "ExtractionOutput": [down] * 10})
        report = build_report(EvaluationRunner(SETTINGS, registry, provider).run(small), registry, SETTINGS)
        case = report["cases"][0]
        assert case["status"] == "NEEDS_REVIEW"
        assert report["metrics"]["structured_output"]["model_errors"] >= 2
        assert any(e["kind"] == "classification" for e in case["errors"])


class TestAttribution:
    def _one(self, dataset: EvalDataset, case_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        data = dataset.model_dump(mode="json")
        return data, next(c for c in data["cases"] if c["id"] == case_id)

    def test_template_selection_with_correct_category_is_a_template_error(
        self, registry: TemplateRegistry, dataset: EvalDataset
    ) -> None:
        data, case = self._one(dataset, "cash-deposit-host-timeout")
        case["expected"]["template_id"] = "generic-operational-incident"  # category stays TRANSACTION
        small = EvalDataset.model_validate({**data, "cases": [case]})
        report = build_report(
            EvaluationRunner(SETTINGS, registry, MockModelProvider()).run(small), registry, SETTINGS
        )
        errors = report["cases"][0]["errors"]
        assert {"layer": "template", "kind": "template_selection"}.items() <= next(
            e for e in errors if e["kind"] == "template_selection"
        ).items()

    def test_correct_extraction_is_not_an_error(
        self, registry: TemplateRegistry, dataset: EvalDataset
    ) -> None:
        data, case = self._one(dataset, "cash-deposit-host-timeout")

        def extraction(_: CompletionRequest) -> str:
            quote = "145 numaralı şubede"
            return json.dumps(
                {
                    "fields": [
                        {
                            "field": "branch_code",
                            "status": "PRESENT",
                            "value": "145",
                            "evidence": [{"source": "description", "quote": quote}],
                        }
                    ]
                }
            )

        small = EvalDataset.model_validate({**data, "cases": [case]})
        report = build_report(
            EvaluationRunner(SETTINGS, registry, ScriptedProvider({"ExtractionOutput": [extraction]})).run(
                small
            ),
            registry,
            SETTINGS,
        )
        kinds = {(e["kind"], e["field"]) for e in report["cases"][0]["errors"]}
        assert ("extraction_missed", "branch_code") not in kinds
        # Every other expected field was not extracted by this scripted model: attributed to the model.
        assert ("extraction_missed", "error_details") in kinds
        assert report["metrics"]["extraction"]["recall"] < 0.5

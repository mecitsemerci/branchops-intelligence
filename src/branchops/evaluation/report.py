"""Metrics, error attribution and reports for an evaluation run.

Errors are attributed to the layer that caused them, so a prompt change is not blamed for a template rule and
vice versa:

* ``model``      classification, extraction (missed / wrong / unsupported values) and rubric-level judgements;
* ``template``   template selection with a correct category; template validation rejecting a correct value;
* ``scoring``    deterministic scores computed from the *expected* field states and levels disagree with the
                 expert assessment (independent of the model);
* ``similarity`` expected relationships not retrieved, not verified, or false duplicates.

Every number in the report is derived from the run and the dataset only; wall-clock data is kept apart in
``runtime`` so that two replays of the same recording produce identical ``metrics``.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from branchops.config.settings import Settings
from branchops.domain.entities.analysis import AnalysisResult
from branchops.domain.entities.issue import LLM_EXTRACTABLE_FIELDS
from branchops.domain.enums import FieldState, RelationshipType, RubricLevel, StepStatus
from branchops.domain.services.evidence import normalize_for_match
from branchops.evaluation.calibration import GradeBands, gold_scores
from branchops.evaluation.metrics import (
    PRF,
    accuracy,
    cohen_kappa,
    macro_f1,
    mean,
    percentile,
    rounded,
    set_prf,
)
from branchops.evaluation.runner import CaseRun, EvaluationRun
from branchops.templates.registry import TemplateRegistry

LLM_STEPS = frozenset(
    {"classify", "extract", "assess_quality", "similarity.verify", "recommend", "investigate"}
)
LEVEL_ORDER = {RubricLevel.ABSENT: 0, RubricLevel.POOR: 1, RubricLevel.ACCEPTABLE: 2, RubricLevel.GOOD: 3}
PRESENT = (FieldState.SUFFICIENT, FieldState.INSUFFICIENT)
RELATION_OF = {
    RelationshipType.DUPLICATE_CANDIDATE: "DUPLICATE",
    RelationshipType.RELATED: "RELATED",
    RelationshipType.UNRELATED: "UNRELATED",
    RelationshipType.UNVERIFIED_CANDIDATE: "UNKNOWN",
}
LINKED = frozenset({"DUPLICATE", "RELATED", "POSSIBLE_COMMON_CAUSE"})


def _relation(rel: RelationshipType) -> str:
    return RELATION_OF.get(rel, str(rel.value))


def _matches(expected: str, actual: str | None) -> bool:
    if not actual:
        return False
    e, a = normalize_for_match(expected), normalize_for_match(actual)
    return e in a or a in e


def _err(layer: str, kind: str, detail: str, field: str | None = None) -> dict[str, Any]:
    return {"layer": layer, "kind": kind, "field": field, "detail": detail}


def evaluate_case(
    run: CaseRun,
    by_key: dict[str, str],
    registry: TemplateRegistry,
    settings: Settings,
    bands: GradeBands,
) -> dict[str, Any]:
    """Per-case comparison of prediction and expectation; ``by_key`` maps incident key -> case id."""
    case, result = run.case, run.result
    exp = case.expected
    errors: list[dict[str, Any]] = []
    out: dict[str, Any] = {"case_id": case.id, "tags": case.tags, "incident_key": case.incident_key}
    if result is None:
        out.update(status="ERROR", error=run.error, errors=[_err("runtime", "case_failed", run.error or "")])
        return out

    cls = result.classification
    issue = result.canonical_issue
    out.update(
        status=result.status.value,
        category=cls.category if cls else None,
        subcategory=cls.subcategory if cls else None,
        template_id=result.template_id,
        completeness=result.scores.completeness_score if result.scores else None,
        quality=result.scores.quality_score if result.scores else None,
        security_flags=list(result.security_flags),
    )

    # Classification / template ------------------------------------------------------------------
    predicted_category = cls.category if cls else None
    if exp.category is not None and predicted_category != exp.category:
        errors.append(_err("model", "classification", f"{predicted_category} != {exp.category}"))
    elif exp.subcategory is not None and cls is not None and cls.subcategory != exp.subcategory:
        errors.append(_err("model", "subcategory", f"{cls.subcategory} != {exp.subcategory}"))
    if exp.template_id is not None and result.template_id != exp.template_id:
        layer = "template" if exp.category is None or predicted_category == exp.category else "model"
        errors.append(_err(layer, "template_selection", f"{result.template_id} != {exp.template_id}"))

    # Extraction -------------------------------------------------------------------------------
    field_scores = {f.field: f for f in (result.scores.field_scores if result.scores else [])}
    states = {name: issue.field(name) for name in issue.fields} if issue else {}
    extraction = {
        "tp": 0,
        "fp": 0,
        "fn": 0,
        "value_ok": 0,
        "value_checked": 0,
        "unsupported": 0,
        "negatives": 0,
    }
    for name in LLM_EXTRACTABLE_FIELDS:
        f = states.get(name)
        present = f is not None and f.state in PRESENT and f.has_value
        if name in exp.fields:
            if present:
                extraction["tp"] += 1
                extraction["value_checked"] += 1
                if _matches(exp.fields[name], f.value if f else None):
                    extraction["value_ok"] += 1
                else:
                    errors.append(
                        _err("model", "extraction_wrong_value", f"{f.value if f else None!r}", name)
                    )
                fs = field_scores.get(name)
                if (
                    fs is not None
                    and fs.validation_errors
                    and _matches(exp.fields[name], f.value if f else None)
                ):
                    errors.append(
                        _err(
                            "template",
                            "validation_rejected_correct_value",
                            "; ".join(fs.validation_errors),
                            name,
                        )
                    )
            else:
                extraction["fn"] += 1
                state = f.state.value if f else "MISSING"
                errors.append(_err("model", "extraction_missed", state, name))
        elif name in exp.missing_fields:
            extraction["negatives"] += 1
            if present:
                extraction["fp"] += 1
                extraction["unsupported"] += 1
                errors.append(_err("model", "extraction_unsupported", f"{f.value if f else None!r}", name))
    out["extraction"] = extraction

    # Missing-field detection (confirmed MISSING only; UNKNOWN is reported apart) --------------------
    predicted_missing = set(result.scores.missing_fields) if result.scores else set()
    predicted_unknown = set(result.scores.unknown_fields) if result.scores else set()
    expected_missing = set(exp.missing_fields)
    out["missing"] = {
        "predicted": sorted(predicted_missing),
        "unknown": sorted(predicted_unknown),
        "expected": sorted(expected_missing),
        "unknown_but_expected_missing": sorted(predicted_unknown & expected_missing),
        "missing_but_expected_present": sorted(predicted_missing & set(exp.fields)),
    }

    # Rubric levels ----------------------------------------------------------------------------
    levels = {a.field: a.level for a in result.assessments}
    level_pairs: list[tuple[str, str]] = []
    for name, expected_level in exp.rubric_levels.items():
        predicted_level = levels.get(name, RubricLevel.ABSENT if name in predicted_missing else None)
        if predicted_level is None:
            continue
        level_pairs.append((predicted_level.value, expected_level.value))
        if predicted_level is not expected_level:
            fs = field_scores.get(name)
            if (
                fs is not None
                and fs.validation_errors
                and expected_level in (RubricLevel.ACCEPTABLE, RubricLevel.GOOD)
            ):
                errors.append(
                    _err(
                        "template",
                        "level_capped_by_validation",
                        f"{predicted_level} != {expected_level}",
                        name,
                    )
                )
            else:
                errors.append(
                    _err("model", "rubric_level", f"{predicted_level.value} != {expected_level.value}", name)
                )
    out["levels"] = level_pairs

    # Scoring (gold inputs vs expert) ------------------------------------------------------------
    gold = gold_scores(case, result, registry, settings.scoring)
    out["gold"] = gold
    if case.expert is not None and gold is not None and gold["quality"] is not None:
        expert_grade = case.expert.grade or (
            bands.grade(case.expert.quality_score) if case.expert.quality_score is not None else None
        )
        gold_grade = bands.grade(gold["quality"])
        if expert_grade is not None and gold_grade != expert_grade:
            errors.append(
                _err(
                    "scoring",
                    "grade_disagreement",
                    f"score {gold['quality']} -> {gold_grade}, expert {expert_grade}",
                )
            )

    # Similarity -------------------------------------------------------------------------------
    predicted_rel = {
        by_key[s.incident_key]: _relation(s.relationship)
        for s in result.similar_issues
        if s.incident_key in by_key
    }
    retrieved = {by_key[k] for k in run.retrieved if k in by_key}
    sim_pairs: list[dict[str, Any]] = []
    for rel in exp.relations:
        got = predicted_rel.get(rel.case_id, "NONE")
        sim_pairs.append(
            {
                "other": rel.case_id,
                "expected": rel.relationship,
                "predicted": got,
                "retrieved": rel.case_id in retrieved,
            }
        )
        if rel.relationship in LINKED and rel.case_id not in retrieved:
            errors.append(_err("similarity", "not_retrieved", rel.case_id))
        elif got != rel.relationship and not (
            rel.relationship == "UNRELATED" and got in ("NONE", "UNRELATED")
        ):
            errors.append(_err("similarity", "relationship", f"{rel.case_id}: {got} != {rel.relationship}"))
    # Links to cases the dataset says nothing about are reported, not scored: a dataset cannot label every
    # pair, and counting them as errors would punish the model for the annotators' omissions.
    labelled = {r.case_id for r in exp.relations}
    out["unlabelled_links"] = sorted(
        f"{other}:{got}" for other, got in predicted_rel.items() if other not in labelled and got in LINKED
    )
    out["similarity"] = sim_pairs

    # Prompt injection --------------------------------------------------------------------------
    if exp.prompt_injection is not None:
        flagged = any(f.startswith("prompt_injection:") for f in result.security_flags)
        out["injection"] = {"expected": exp.prompt_injection, "flagged": flagged}
        if flagged != exp.prompt_injection:
            errors.append(_err("detection", "prompt_injection", f"flagged={flagged}"))

    # Model calls -------------------------------------------------------------------------------
    out["steps"] = _step_stats(result)
    out["calls"] = {
        "count": len(run.calls),
        "errors": sum(1 for c in run.calls if c.error),
        "latency_ms": sum(c.latency_ms for c in run.calls),
        "prompt_tokens": sum(c.prompt_tokens for c in run.calls),
        "completion_tokens": sum(c.completion_tokens for c in run.calls),
        "per_call_latency_ms": [c.latency_ms for c in run.calls if not c.error],
    }
    out["errors"] = errors
    return out


def _step_stats(result: AnalysisResult) -> dict[str, int]:
    stats = Counter[str]()
    for step in result.steps:
        if step.step not in LLM_STEPS or step.status is StepStatus.SKIPPED:
            continue
        stats["total"] += 1
        if step.status is StepStatus.SUCCEEDED:
            stats["valid"] += 1
            if step.attempts <= 1:
                stats["first_pass"] += 1
        elif step.error and "invalid structured output" in step.error:
            stats["invalid_output"] += 1
        else:
            stats["model_error"] += 1
    return dict(stats)


def _sum(items: Iterable[dict[str, int]], key: str) -> int:
    return sum(i.get(key, 0) for i in items)


def aggregate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [c for c in cases if c.get("status") != "ERROR"]
    cat_pairs = [(c["category"], c["_exp_category"]) for c in ok if c.get("_exp_category") is not None]
    sub_pairs = [
        (c["subcategory"], c["_exp_subcategory"]) for c in ok if c.get("_exp_subcategory") is not None
    ]
    tmpl_pairs = [(c["template_id"], c["_exp_template"]) for c in ok if c.get("_exp_template") is not None]

    ext = PRF(
        _sum((c["extraction"] for c in ok), "tp"),
        _sum((c["extraction"] for c in ok), "fp"),
        _sum((c["extraction"] for c in ok), "fn"),
    )
    value_checked = _sum((c["extraction"] for c in ok), "value_checked")
    negatives = _sum((c["extraction"] for c in ok), "negatives")
    missing = PRF()
    unknown_as_missing = 0
    for c in ok:
        m = c["missing"]
        missing = missing + set_prf(m["predicted"], m["expected"])
        unknown_as_missing += len(m["unknown_but_expected_missing"])
    level_pairs = [tuple(p) for c in ok for p in c["levels"]]
    weights = {lvl.value: idx for lvl, idx in LEVEL_ORDER.items()}

    steps = [c["steps"] for c in ok]
    total_steps = _sum(steps, "total")
    sims = [p for c in ok for p in c["similarity"]]
    dup = PRF(
        tp=sum(1 for p in sims if p["predicted"] == "DUPLICATE" and p["expected"] == "DUPLICATE"),
        fp=sum(1 for p in sims if p["predicted"] == "DUPLICATE" and p["expected"] != "DUPLICATE"),
        fn=sum(1 for p in sims if p["predicted"] != "DUPLICATE" and p["expected"] == "DUPLICATE"),
    )
    labelled_negatives = [
        p for p in sims if p["expected"] in ("UNRELATED", "RELATED", "POSSIBLE_COMMON_CAUSE")
    ]
    linked_expected = [p for p in sims if p["expected"] in LINKED]
    predicted_linked = [p for p in sims if p["predicted"] in LINKED]
    injection = [c["injection"] for c in ok if "injection" in c]

    latencies = [c["calls"]["latency_ms"] for c in ok]
    per_call = [x for c in ok for x in c["calls"]["per_call_latency_ms"]]
    tokens = [c["calls"]["prompt_tokens"] + c["calls"]["completion_tokens"] for c in ok]

    layers = Counter(e["layer"] for c in cases for e in c["errors"])
    kinds = Counter(f"{e['layer']}.{e['kind']}" for c in cases for e in c["errors"])
    by_tag: dict[str, Counter[str]] = defaultdict(Counter)
    for c in cases:
        for tag in c.get("tags", []):
            by_tag[tag]["cases"] += 1
            by_tag[tag]["cases_with_errors"] += 1 if c["errors"] else 0
            for e in c["errors"]:
                by_tag[tag][e["layer"]] += 1

    return {
        "cases": len(cases),
        "failed_cases": len(cases) - len(ok),
        "classification": {
            "n": len(cat_pairs),
            "accuracy": rounded(accuracy(cat_pairs)),
            "macro_f1": rounded(macro_f1(cat_pairs)),
            "subcategory_accuracy": rounded(accuracy(sub_pairs)),
            "template_accuracy": rounded(accuracy(tmpl_pairs)),
        },
        "extraction": {
            **ext.as_dict(),
            "value_accuracy": rounded(_sum((c["extraction"] for c in ok), "value_ok") / value_checked)
            if value_checked
            else None,
            "unsupported_generation_rate": rounded(
                _sum((c["extraction"] for c in ok), "unsupported") / negatives
            )
            if negatives
            else None,
        },
        "missing_fields": {**missing.as_dict(), "unknown_but_expected_missing": unknown_as_missing},
        "rubric_levels": {
            "n": len(level_pairs),
            "agreement": rounded(accuracy(level_pairs)),
            "weighted_kappa": rounded(cohen_kappa(level_pairs, weights)) if level_pairs else None,
        },
        "structured_output": {
            "llm_steps": total_steps,
            "validity_rate": rounded(_sum(steps, "valid") / total_steps) if total_steps else None,
            "first_pass_validity_rate": rounded(_sum(steps, "first_pass") / total_steps)
            if total_steps
            else None,
            "invalid_output": _sum(steps, "invalid_output"),
            "model_errors": _sum(steps, "model_error"),
        },
        "similarity": {
            "labelled_pairs": len(sims),
            "retrieval_recall": rounded(
                sum(1 for p in linked_expected if p["retrieved"]) / len(linked_expected)
            )
            if linked_expected
            else None,
            "relationship_precision": rounded(
                sum(1 for p in predicted_linked if p["predicted"] == p["expected"]) / len(predicted_linked)
            )
            if predicted_linked
            else None,
            "false_positive_rate": rounded(
                sum(1 for p in predicted_linked if p["expected"] not in LINKED) / len(predicted_linked)
            )
            if predicted_linked
            else None,
            "duplicate": dup.as_dict(),
            "unlabelled_links": sum(len(c.get("unlabelled_links", [])) for c in ok),
            "false_merge_rate": rounded(
                sum(1 for p in labelled_negatives if p["predicted"] == "DUPLICATE") / len(labelled_negatives)
            )
            if labelled_negatives
            else None,
        },
        "prompt_injection": {
            "n": len(injection),
            "detection_accuracy": rounded(accuracy([(i["flagged"], i["expected"]) for i in injection])),
        },
        "latency_ms": {
            "per_case_mean": rounded(mean(latencies), 1),
            "per_case_p95": percentile(latencies, 95),
            "per_call_p50": percentile(per_call, 50),
            "per_call_p95": percentile(per_call, 95),
        },
        "tokens": {
            "total": sum(tokens),
            "per_case_mean": rounded(mean(tokens), 1),
            "prompt": sum(c["calls"]["prompt_tokens"] for c in ok),
            "completion": sum(c["calls"]["completion_tokens"] for c in ok),
        },
        "errors_by_layer": dict(sorted(layers.items())),
        "errors_by_kind": dict(sorted(kinds.items())),
        "errors_by_tag": {tag: dict(sorted(v.items())) for tag, v in sorted(by_tag.items())},
    }


def build_report(
    run: EvaluationRun, registry: TemplateRegistry, settings: Settings, bands: GradeBands | None = None
) -> dict[str, Any]:
    bands = bands or GradeBands.from_policy(settings.scoring)
    by_key = {c.case.incident_key: c.case.id for c in run.cases}
    cases: list[dict[str, Any]] = []
    for case_run in run.cases:
        item = evaluate_case(case_run, by_key, registry, settings, bands)
        exp = case_run.case.expected
        item["_exp_category"], item["_exp_subcategory"], item["_exp_template"] = (
            exp.category,
            exp.subcategory,
            exp.template_id,
        )
        cases.append(item)
    metrics = aggregate(cases)
    for item in cases:
        for key in ("_exp_category", "_exp_subcategory", "_exp_template"):
            item.pop(key, None)
    return {
        "metadata": run.metadata,
        "grade_bands": bands.as_dict(),
        "metrics": metrics,
        "cases": cases,
        "runtime": run.runtime,
    }


def write_report(report: dict[str, Any], out_dir: str | Path, stem: str = "evaluation") -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{stem}.json"
    md_path = out / f"{stem}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False), encoding="utf-8")
    md_path.write_text(render_markdown([report]), encoding="utf-8")
    return json_path, md_path


# --- Markdown ----------------------------------------------------------------------------------

_ROWS: list[tuple[str, tuple[str, ...]]] = [
    ("Classification accuracy", ("classification", "accuracy")),
    ("Classification macro F1", ("classification", "macro_f1")),
    ("Subcategory accuracy", ("classification", "subcategory_accuracy")),
    ("Template accuracy", ("classification", "template_accuracy")),
    ("Extraction precision", ("extraction", "precision")),
    ("Extraction recall", ("extraction", "recall")),
    ("Extracted value accuracy", ("extraction", "value_accuracy")),
    ("Unsupported field generation rate", ("extraction", "unsupported_generation_rate")),
    ("Missing-field precision", ("missing_fields", "precision")),
    ("Missing-field recall", ("missing_fields", "recall")),
    ("UNKNOWN where MISSING expected", ("missing_fields", "unknown_but_expected_missing")),
    ("Rubric level agreement", ("rubric_levels", "agreement")),
    ("Rubric weighted κ", ("rubric_levels", "weighted_kappa")),
    ("Structured output validity", ("structured_output", "validity_rate")),
    ("First-pass validity", ("structured_output", "first_pass_validity_rate")),
    ("Similarity retrieval recall", ("similarity", "retrieval_recall")),
    ("Relationship precision", ("similarity", "relationship_precision")),
    ("Relationship false-positive rate", ("similarity", "false_positive_rate")),
    ("Duplicate precision", ("similarity", "duplicate", "precision")),
    ("Duplicate recall", ("similarity", "duplicate", "recall")),
    ("False merge rate", ("similarity", "false_merge_rate")),
    ("Links to unlabelled cases (not scored)", ("similarity", "unlabelled_links")),
    ("Injection detection accuracy", ("prompt_injection", "detection_accuracy")),
    ("Model latency per case, mean (ms)", ("latency_ms", "per_case_mean")),
    ("Model latency per call, p95 (ms)", ("latency_ms", "per_call_p95")),
    ("Tokens per case, mean", ("tokens", "per_case_mean")),
    ("Tokens, total", ("tokens", "total")),
]


def _get(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _fmt(value: Any) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".") if value != int(value) else f"{value:.0f}"
    return str(value)


def render_markdown(reports: list[dict[str, Any]]) -> str:
    """One report, or a side-by-side comparison of several runs over the same dataset."""
    datasets = {r["metadata"]["dataset"]["sha256"] for r in reports}
    lines = ["# BranchOps evaluation" + (" — comparison" if len(reports) > 1 else ""), ""]
    if len(datasets) > 1:
        lines += ["> **Warning:** the runs use different datasets; the numbers are not comparable.", ""]
    meta_keys = ("label", "model_id", "prompt_version", "scoring_policy_version", "lexicon_version")
    header = "| | " + " | ".join(_fmt(r["metadata"]["label"]) for r in reports) + " |"
    sep = "|---|" + "---|" * len(reports)
    lines += [header, sep]
    for key in meta_keys[1:]:
        lines.append(f"| {key} | " + " | ".join(_fmt(r["metadata"].get(key)) for r in reports) + " |")
    ds = reports[0]["metadata"]["dataset"]
    lines.append(
        "| dataset | "
        + " | ".join(f"{ds['name']} v{ds['version']} ({r['metadata']['case_count']} cases)" for r in reports)
        + " |"
    )
    lines.append("| templates | " + " | ".join(", ".join(r["metadata"]["templates"]) for r in reports) + " |")
    lines += ["", "## Metrics", "", header, sep]
    for label, path in _ROWS:
        lines.append(f"| {label} | " + " | ".join(_fmt(_get(r["metrics"], path)) for r in reports) + " |")
    lines += ["", "## Errors by layer", "", header, sep]
    layers = sorted({k for r in reports for k in r["metrics"]["errors_by_layer"]})
    for layer in layers:
        lines.append(
            f"| {layer} | "
            + " | ".join(_fmt(r["metrics"]["errors_by_layer"].get(layer, 0)) for r in reports)
            + " |"
        )
    if len(reports) == 1:
        report = reports[0]
        lines += [
            "",
            "## Errors by scenario tag",
            "",
            "| Tag | Cases | With errors | Errors by layer |",
            "|---|---|---|---|",
        ]
        for tag, counts in report["metrics"]["errors_by_tag"].items():
            layer_counts = ", ".join(
                f"{k} {v}" for k, v in counts.items() if k not in ("cases", "cases_with_errors")
            )
            lines.append(
                f"| {tag} | {counts.get('cases', 0)} | {counts.get('cases_with_errors', 0)} "
                f"| {layer_counts or '–'} |"
            )
        lines += [
            "",
            "## Case details",
            "",
            "| Case | Status | Category | Quality | Errors |",
            "|---|---|---|---|---|",
        ]
        for c in report["cases"]:
            errs = "; ".join(
                f"{e['layer']}.{e['kind']}" + (f"({e['field']})" if e.get("field") else "")
                for e in c["errors"]
            )
            lines.append(
                f"| {c['case_id']} | {c.get('status')} | {_fmt(c.get('category'))} "
                f"| {_fmt(c.get('quality'))} | {errs or '–'} |"
            )
    lines += [
        "",
        "Model accuracy shown here is only as good as the provider used: the mock provider is keyword-based "
        "and its numbers say nothing about GLM-5.3-Flash or any other model.",
        "",
    ]
    return "\n".join(lines)

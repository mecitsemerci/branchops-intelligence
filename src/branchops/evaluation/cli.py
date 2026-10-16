"""``branchops eval`` sub-commands: run, compare, calibrate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from branchops.config.settings import Settings


def add_parser(sub: Any, handler: Any) -> None:
    ev = sub.add_parser(
        "eval", help="offline evaluation of the analysis pipeline (models, templates, scoring)"
    )
    ev_sub = ev.add_subparsers(dest="eval_command", required=True)

    run = ev_sub.add_parser("run", help="analyse a dataset and write JSON + Markdown reports")
    run.add_argument("--dataset", required=True)
    run.add_argument(
        "--provider",
        choices=["mock", "replay", "live"],
        default="mock",
        help="mock: keyword rules (CI); replay: answers from --recording; live: the configured model",
    )
    run.add_argument("--recording", help="recording to replay (--provider replay)")
    run.add_argument("--record", help="store the model answers of this run here (for later replay)")
    run.add_argument("--model", help="override llm.model for a live run (compare models on one gateway)")
    run.add_argument("--label", help="name of the run in reports (default: model id)")
    run.add_argument("--out", default="eval-results", help="output directory")
    run.set_defaults(func=handler)

    cmp = ev_sub.add_parser("compare", help="side-by-side Markdown of two or more run reports")
    cmp.add_argument("reports", nargs="+", help="evaluation JSON reports (same dataset)")
    cmp.add_argument("--out", help="write Markdown here instead of stdout")
    cmp.set_defaults(func=handler)

    cal = ev_sub.add_parser("calibrate", help="compare scores with expert reference assessments")
    cal.add_argument("--dataset", required=True)
    cal.add_argument("--recording", help="also calibrate the system scores of a replayed run")
    cal.add_argument("--high", type=float, default=75.0, help="lower bound of the HIGH grade band")
    cal.add_argument("--out", help="write JSON here instead of stdout")
    cal.set_defaults(func=handler)


def _provider(settings: Settings, args: argparse.Namespace) -> Any:
    if args.provider == "mock":
        from branchops.infrastructure.llm.mock_provider import MockModelProvider

        return MockModelProvider()
    if args.provider == "replay":
        from branchops.evaluation.providers import ReplayProvider

        if not args.recording:
            raise SystemExit("--provider replay needs --recording")
        return ReplayProvider.load(args.recording)
    from branchops.container import create_model_provider

    if settings.llm.provider == "mock":
        raise SystemExit("--provider live needs llm.provider: openai-compatible in the configuration")
    if args.model:
        settings.llm.model = args.model
    return create_model_provider(settings)


def run_eval(settings: Settings, args: argparse.Namespace) -> int:
    from branchops.evaluation.calibration import GradeBands, calibrate
    from branchops.evaluation.dataset import load_dataset
    from branchops.evaluation.providers import RecordingProvider
    from branchops.evaluation.report import build_report, render_markdown, write_report
    from branchops.evaluation.runner import EvaluationRunner
    from branchops.templates.registry import TemplateRegistry

    registry = TemplateRegistry.from_directory(Path(settings.templates.directory))
    if args.eval_command == "compare":
        reports = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.reports]
        text = render_markdown(reports)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text)
        return 0

    dataset = load_dataset(args.dataset)
    if args.eval_command == "calibrate":
        from branchops.domain.services.masking import SensitiveDataMasker
        from branchops.evaluation.providers import ReplayProvider
        from branchops.infrastructure.sources.jira.adapter import JiraWebhookAdapter

        results = None
        if args.recording:
            run = EvaluationRunner(settings, registry, ReplayProvider.load(args.recording)).run(dataset)
            results = {c.case.id: c.result for c in run.cases if c.result is not None}
        report = calibrate(
            dataset,
            registry,
            settings.scoring,
            JiraWebhookAdapter(settings.jira.field_mapping),
            SensitiveDataMasker(settings.security.masking_extra_patterns),
            results=results,
            bands=GradeBands.from_policy(settings.scoring, high=args.high),
        )
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text)
        return 0

    provider = _provider(settings, args)
    recorder = RecordingProvider(provider) if args.record else None
    run = EvaluationRunner(settings, registry, recorder or provider, label=args.label).run(dataset)
    report = build_report(run, registry, settings)
    stem = (args.label or report["metadata"]["model_id"]).replace("/", "_")
    json_path, md_path = write_report(report, args.out, stem=f"evaluation-{stem}")
    if recorder is not None:
        recorder.save(args.record)
        print(f"recording: {args.record}", file=sys.stderr)
    print(f"report: {json_path}\nreport: {md_path}", file=sys.stderr)
    print(json.dumps(report["metrics"]["classification"]))
    return 1 if report["metrics"]["failed_cases"] else 0

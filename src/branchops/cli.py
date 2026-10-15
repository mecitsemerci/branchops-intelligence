"""Command-line entry point: ``branchops <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from pathlib import Path

from branchops.config.settings import Settings, load_settings
from branchops.infrastructure.observability.logging import configure_logging

logger = logging.getLogger("branchops")


def _settings(args: argparse.Namespace) -> Settings:
    settings = load_settings(args.config) if args.config else load_settings()
    configure_logging(settings.app.log_level)
    return settings


def cmd_api(args: argparse.Namespace) -> int:
    import uvicorn

    from branchops.api.app import create_app

    settings = _settings(args)
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.api.host,
        port=settings.api.port,
        proxy_headers=False,
        server_header=False,
        log_config=None,
    )
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from branchops.application.handlers.process_event import EventProcessor
    from branchops.container import Container
    from branchops.infrastructure.observability.telemetry import register_gauge, setup_telemetry
    from branchops.infrastructure.persistence.inbox_repository import InboxRepository
    from branchops.workers.dispatcher import Dispatcher, start_health_server, worker_identity
    from branchops.workers.processor import InboxItemRunner

    settings = _settings(args)
    setup_telemetry(settings, role="worker")
    container = Container.build(settings)
    workflow = container.build_workflow()
    processor = EventProcessor(
        db=container.db, settings=settings, registry=container.registry, workflow=workflow
    )
    owner = worker_identity()
    runner = InboxItemRunner(container.db, processor, settings.worker, owner)
    dispatcher = Dispatcher(container.db, runner, settings.worker, owner)
    inbox = InboxRepository()

    def depth() -> list[tuple[float, dict[str, str]]]:
        with container.db.session() as s:
            counts = inbox.depth_by_status(s)
        return [(float(counts.get(st, 0)), {"status": st}) for st in ("PENDING", "PROCESSING", "DEAD_LETTER")]

    def oldest() -> list[tuple[float, dict[str, str]]]:
        with container.db.session() as s:
            return [(inbox.oldest_pending_age_seconds(s), {})]

    register_gauge("branchops_inbox_depth", "Inbox events by status", depth)
    register_gauge("branchops_inbox_oldest_pending_seconds", "Age of the oldest pending event", oldest)
    start_health_server(dispatcher, settings.worker.health_port, stale_after_seconds=120)
    dispatcher.install_signal_handlers()
    try:
        dispatcher.run_forever()
    finally:
        container.close()
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from branchops.dashboard.app import run_dashboard

    run_dashboard(_settings(args))
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from alembic import command
    from alembic.config import Config

    from branchops.container import Container, create_checkpointer
    from branchops.infrastructure.persistence.template_store import TemplateStore

    settings = _settings(args)
    # Migrations ship next to the application (/app in the container, the repository root in development).
    root = Path(os.environ.get("BRANCHOPS_HOME", Path.cwd()))
    if not (root / "migrations").is_dir():
        print(f"migrations directory not found under {root}; set BRANCHOPS_HOME", file=sys.stderr)
        return 1
    cfg = Config(str(root / "alembic.ini")) if (root / "alembic.ini").exists() else Config()
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.attributes["database_url"] = settings.database.url
    command.upgrade(cfg, "head")
    if settings.workflow.checkpointer == "postgres":
        checkpointer, pool = create_checkpointer(settings)
        checkpointer.setup()
        pool.close()
    container = Container.build(settings)
    with container.db.session() as s:
        published = TemplateStore().sync(s, container.registry)
    container.close()
    print(json.dumps({"migrated": True, "templates_published": published}))
    return 0


def cmd_templates(args: argparse.Namespace) -> int:
    from branchops.templates.registry import TemplateError, TemplateRegistry

    settings = _settings(args)
    try:
        registry = TemplateRegistry.from_directory(Path(settings.templates.directory))
    except TemplateError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    for t in registry.all_versions():
        print(
            f"{t.template_id}@{t.version} [{t.status}] category={t.category} "
            f"fields={len(t.fields)} sha256={t.content_hash()[:12]}"
        )
    return 0


def cmd_llm(args: argparse.Namespace) -> int:
    from branchops.container import create_model_provider
    from branchops.infrastructure.llm.openai_compatible import OpenAICompatibleModelProvider

    settings = _settings(args)
    provider = create_model_provider(settings)
    if args.llm_command == "probe":
        if not isinstance(provider, OpenAICompatibleModelProvider):
            print("probe is only available for the openai-compatible provider", file=sys.stderr)
            return 1
        print(provider.probe_capabilities().model_dump_json(indent=2))
        return 0
    health = provider.health()
    print(
        json.dumps(
            {
                "healthy": health.healthy,
                "model": health.model_id,
                "detail": health.detail,
                "latency_ms": health.latency_ms,
            }
        )
    )
    return 0 if health.healthy else 2


def cmd_replay(args: argparse.Namespace) -> int:
    from branchops.container import Container
    from branchops.infrastructure.persistence.audit import audit
    from branchops.infrastructure.persistence.inbox_repository import InboxRepository

    settings = _settings(args)
    container = Container.build(settings)
    with container.db.session() as s:
        ok = InboxRepository().replay(s, uuid.UUID(args.event_id))
        if ok:
            audit(
                s,
                actor=f"cli:{args.actor}",
                action="inbox_replay",
                resource_type="inbox_event",
                resource_id=args.event_id,
            )
    container.close()
    print("requeued" if ok else "not replayable (must be DEAD_LETTER or SKIPPED)")
    return 0 if ok else 1


def cmd_report(args: argparse.Namespace) -> int:
    from branchops.agents.base import StructuredCaller
    from branchops.agents.reporting import ReportingAgent
    from branchops.application.queries.read_models import ReadModels
    from branchops.container import Container, create_model_provider

    settings = _settings(args)
    container = Container.build(settings)
    read = ReadModels(settings.scoring.low_quality_threshold)
    with container.db.session() as s:
        overview = read.overview(s, args.days).__dict__
        overview.pop("daily_trend", None)
        metrics = {
            "period_days": args.days,
            "overview": overview,
            "most_missing_fields": read.quality_analytics(s, args.days)["most_missing_fields"][:5],
        }
    caller = StructuredCaller(
        create_model_provider(settings), max_repair_attempts=settings.workflow.max_repair_attempts
    )
    report, source = ReportingAgent(caller).run(metrics, correlation_id=uuid.uuid4().hex)
    container.close()
    print(
        json.dumps(
            {"source": source, "report": report.model_dump(), "metrics": metrics},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    """Development/demo: post random Turkish incidents to the API on a schedule."""
    from branchops.simulator.runner import WebhookSimulator

    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    secret = args.secret or next(
        (s.strip() for s in os.environ.get("JIRA_WEBHOOK_SECRETS", "").split(",") if s.strip()), ""
    )
    if not secret:
        print("a webhook secret is required (--secret or JIRA_WEBHOOK_SECRETS)", file=sys.stderr)
        return 1
    simulator = WebhookSimulator(
        args.api_url,
        secret,
        interval_seconds=args.interval,
        initial_burst=args.burst,
        follow_up_probability=args.follow_up,
        seed=args.seed,
    )
    if args.once:
        simulator.run_once()
    else:
        simulator.run_forever()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="branchops", description="BranchOps Intelligence")
    parser.add_argument("--config", help="path to YAML configuration (default: $BRANCHOPS_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("api", help="run the FastAPI web application").set_defaults(func=cmd_api)
    sub.add_parser("worker", help="run the background inbox worker").set_defaults(func=cmd_worker)
    sub.add_parser("dashboard", help="run the NiceGUI dashboard").set_defaults(func=cmd_dashboard)
    sub.add_parser(
        "migrate", help="apply DB migrations, checkpoint tables and publish templates"
    ).set_defaults(func=cmd_migrate)
    sub.add_parser("templates", help="validate and list templates").set_defaults(func=cmd_templates)
    llm = sub.add_parser("llm", help="model endpoint utilities")
    llm.add_argument("llm_command", choices=["health", "probe"])
    llm.set_defaults(func=cmd_llm)
    replay = sub.add_parser("replay", help="requeue a dead-lettered inbox event")
    replay.add_argument("event_id")
    replay.add_argument("--actor", default="operator")
    replay.set_defaults(func=cmd_replay)
    sim = sub.add_parser("simulate", help="send random synthetic incidents to the API (dev/demo only)")
    sim.add_argument("--api-url", default=os.environ.get("SIMULATOR_API_URL", "http://localhost:8080"))
    sim.add_argument("--secret", default=None, help="webhook secret (default: first of JIRA_WEBHOOK_SECRETS)")
    sim.add_argument(
        "--interval", type=float, default=float(os.environ.get("SIMULATOR_INTERVAL_SECONDS", "300"))
    )
    sim.add_argument("--burst", type=int, default=int(os.environ.get("SIMULATOR_INITIAL_BURST", "2")))
    sim.add_argument(
        "--follow-up", type=float, default=float(os.environ.get("SIMULATOR_FOLLOW_UP_PROBABILITY", "0.35"))
    )
    sim.add_argument("--seed", type=int, default=None)
    sim.add_argument("--once", action="store_true", help="send --burst incidents and exit")
    sim.set_defaults(func=cmd_simulate)
    report = sub.add_parser("report", help="generate an operational summary report")
    report.add_argument("--days", type=int, default=30)
    report.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

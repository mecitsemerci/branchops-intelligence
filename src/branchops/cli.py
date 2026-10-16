"""Command-line entry point: ``branchops <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
    from branchops.application.commands.routing import IncidentRouter

    check_notification_readiness(container)
    processor = EventProcessor(
        db=container.db,
        settings=settings,
        registry=container.registry,
        workflow=workflow,
        sources=container.sources,
        on_analysis=IncidentRouter(container.db, settings).on_analysis_complete,
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
    drainers = start_outbox_drainers(container, owner)
    try:
        dispatcher.run_forever()
    finally:
        for drainer in drainers:
            drainer.stop()
        container.close()
    return 0


def start_outbox_drainers(container: Any, owner: str) -> list[Any]:
    """Outboxes are drained by every worker replica (SKIP LOCKED); attachment retention runs hourly."""
    from branchops.application.commands.attachments import AttachmentService
    from branchops.infrastructure.attachments import build_attachment_store
    from branchops.infrastructure.ticketing import build_ticketing_sink
    from branchops.infrastructure.ticketing.handler import TICKETING_OUTBOX, TicketingHandler
    from branchops.workers.housekeeping import PeriodicTask
    from branchops.workers.outbox import OutboxDrainer

    settings = container.settings
    db = container.db
    attachments = AttachmentService(settings, build_attachment_store(settings))

    def load_attachment(attachment_id: str) -> Any:
        with db.session() as s:
            return attachments.load(s, attachment_id)

    def purge() -> dict[str, int]:
        with db.session() as s:
            return attachments.purge(s)

    def purge_inbox() -> dict[str, int]:
        from branchops.infrastructure.persistence.inbox_repository import InboxRepository

        retention = settings.retention
        with db.session() as s:
            deleted = InboxRepository().purge_finished(
                s, completed_days=retention.inbox_days, dead_letter_days=retention.inbox_dead_letter_days
            )
        return {"inbox_events_deleted": deleted}

    tasks: list[Any] = [
        PeriodicTask("attachment-retention", purge, 3600.0),
        PeriodicTask("inbox-retention", purge_inbox, 3600.0),
    ]
    if settings.notifications.enabled:
        from branchops.application.commands.notifications import NOTIFICATION_OUTBOX
        from branchops.infrastructure.notifications import build_channels
        from branchops.infrastructure.notifications.handler import NotificationHandler

        tasks.append(
            OutboxDrainer(
                "notifications",
                db,
                NOTIFICATION_OUTBOX,
                NotificationHandler(db, build_channels(settings)),
                settings.outbox,
                owner,
            )
        )
    sink = build_ticketing_sink(settings)
    if sink is not None:
        tasks.append(
            OutboxDrainer(
                "ticketing",
                db,
                TICKETING_OUTBOX,
                TicketingHandler(db, sink, load_attachment),
                settings.outbox,
                owner,
            )
        )
    for task in tasks:
        task.start()
    return tasks


def check_notification_readiness(container: Any) -> None:
    """Production guard: the branch-ops team must exist, be active and be reachable on a working channel."""
    from branchops.application.commands.ownership import OwnershipService, notification_findings

    settings = container.settings
    if settings.app.environment != "production" or not settings.notifications.enabled:
        return
    findings = notification_findings(OwnershipService(container.db, settings).registry(), settings)
    errors = [f.message for f in findings if f.level == "error"]
    if errors:
        raise SystemExit("refusing to start: " + "; ".join(errors))


def cmd_inbox(args: argparse.Namespace) -> int:
    from branchops.container import Container
    from branchops.infrastructure.persistence.inbox_repository import InboxRepository

    settings = _settings(args)
    container = Container.build(settings)
    try:
        with container.db.session() as s:
            deleted = InboxRepository().purge_finished(
                s,
                completed_days=settings.retention.inbox_days,
                dead_letter_days=settings.retention.inbox_dead_letter_days,
            )
        print(json.dumps({"inbox_events_deleted": deleted}))
    finally:
        container.close()
    return 0


def cmd_attachments(args: argparse.Namespace) -> int:
    """``branchops attachments purge``: apply ``retention.attachments_days`` now."""
    from branchops.application.commands.attachments import AttachmentService
    from branchops.container import Container
    from branchops.infrastructure.attachments import build_attachment_store

    settings = _settings(args)
    container = Container.build(settings)
    try:
        service = AttachmentService(settings, build_attachment_store(settings))
        with container.db.session() as s:
            result = service.purge(s)
    finally:
        container.close()
    print(json.dumps(result))
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """Import a CSV/XLSX/JSONL file through a file-based pull source into the inbox."""
    from branchops.application.commands.ingest_webhook import IngestIncidentEvent
    from branchops.application.interfaces.source_adapter import (
        FileImportSourceAdapter,
        SourcePayloadError,
        UnknownSourceError,
    )
    from branchops.container import Container
    from branchops.domain.entities.incident import incident_key
    from branchops.infrastructure.persistence.issue_repository import IssueRepository

    settings = _settings(args)
    path = Path(args.path)
    if not path.is_file():
        print(f"file not found: {path}", file=sys.stderr)
        return 1
    container = Container.build(settings)
    try:
        try:
            adapter = container.sources.get(args.source)
        except UnknownSourceError:
            print(f"unknown or disabled source: {args.source}", file=sys.stderr)
            return 1
        if not isinstance(adapter, FileImportSourceAdapter):
            print(f"source {args.source} does not import files", file=sys.stderr)
            return 1
        issues = IssueRepository()

        def is_known(external_key: str) -> bool:
            with container.db.session() as s:
                return issues.exists(s, incident_key(adapter.source_id, external_key))

        command = IngestIncidentEvent(container.db, settings, container.sources)
        correlation_id = f"import:{uuid.uuid4().hex[:16]}"
        counts: dict[str, int] = {}
        try:
            for event in adapter.read_file(path, is_known=is_known):
                if args.dry_run:
                    outcome = "parsed"
                else:
                    outcome = command.execute(args.source, event=event, correlation_id=correlation_id).outcome
                counts[str(outcome)] = counts.get(str(outcome), 0) + 1
        except SourcePayloadError as exc:
            print(json.dumps({"error": str(exc), **counts}, ensure_ascii=False), file=sys.stderr)
            return 2
        audit_details = {"path": path.name, "dry_run": args.dry_run, **counts}
        if not args.dry_run:
            from branchops.infrastructure.persistence.audit import audit

            with container.db.session() as s:
                audit(
                    s,
                    actor=f"cli:{args.actor}",
                    action="source_import",
                    resource_type="source",
                    resource_id=args.source,
                    details={**audit_details, "correlation_id": correlation_id},
                )
        print(json.dumps({"source": args.source, "correlation_id": correlation_id, **audit_details}))
        return 0
    finally:
        container.close()


def cmd_poll(args: argparse.Namespace) -> int:
    """Run pull sources on their schedule (leader-elected) or once."""
    from branchops.container import Container
    from branchops.workers.dispatcher import start_health_server
    from branchops.workers.poller import SourcePoller

    settings = _settings(args)
    container = Container.build(settings)
    try:
        poller = SourcePoller(container.db, settings, container.sources, only=args.source)
        if not poller.adapters():
            print(
                "no enabled pull source" + (f" named {args.source}" if args.source else ""), file=sys.stderr
            )
            return 1
        if args.once:
            events = poller.run_once(force=True)
            print(json.dumps({"events": events}))
            return 0
        start_health_server(poller, settings.worker.health_port, stale_after_seconds=120)
        signal_stop(poller.stop)
        poller.run_forever()
        return 0
    finally:
        container.close()


def signal_stop(stop: Callable[[], None]) -> None:
    import signal

    def handler(_signum: int, _frame: object) -> None:
        stop()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def cmd_mcp(args: argparse.Namespace) -> int:
    """MCP server for other teams' MCP clients (streamable HTTP; ``--stdio`` for local dev)."""
    from branchops.mcp.server import run

    settings = _settings(args)
    if args.stdio and settings.app.environment == "production":
        print("--stdio is for local development only; refused in production", file=sys.stderr)
        return 1
    run(settings, stdio=args.stdio)
    return 0


def cmd_ownership(args: argparse.Namespace) -> int:
    """``branchops ownership check``: validate the screen ownership registry (exit 1 on errors)."""
    from branchops.application.commands.ownership import OwnershipService
    from branchops.container import Container

    settings = _settings(args)
    container = Container.build(settings)
    try:
        findings = OwnershipService(container.db, settings).check(preview_days=args.days)
    finally:
        container.close()
    errors = [f for f in findings if f.level == "error"]
    print(
        json.dumps(
            {
                "errors": len(errors),
                "warnings": len(findings) - len(errors),
                "findings": [f.__dict__ for f in findings],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if errors else 0


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


def cmd_eval(args: argparse.Namespace) -> int:
    from branchops.evaluation.cli import run_eval

    return run_eval(_settings(args), args)


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
    mcp = sub.add_parser("mcp", help="run the MCP server (queries, in-app reporting, re-analysis)")
    mcp.add_argument("--stdio", action="store_true", help="serve over stdio (local development only)")
    mcp.set_defaults(func=cmd_mcp)
    imp = sub.add_parser("import", help="import a CSV/XLSX/JSONL file through a file-based source")
    imp.add_argument("--source", required=True, help="id of an enabled csv_import source")
    imp.add_argument("path")
    imp.add_argument("--dry-run", action="store_true", help="parse and validate only; write nothing")
    imp.add_argument("--actor", default="operator")
    imp.set_defaults(func=cmd_import)
    poll = sub.add_parser("poll", help="read pull sources on their schedule (one leader across replicas)")
    poll.add_argument("--source", default=None, help="only this source (default: all enabled pull sources)")
    poll.add_argument("--once", action="store_true", help="poll every source once and exit")
    poll.set_defaults(func=cmd_poll)
    inbox = sub.add_parser("inbox", help="inbox utilities")
    inbox.add_argument("inbox_command", choices=["purge"], help="apply retention.inbox_days now")
    inbox.set_defaults(func=cmd_inbox)
    att = sub.add_parser("attachments", help="attachment utilities")
    att.add_argument("attachments_command", choices=["purge"])
    att.set_defaults(func=cmd_attachments)
    own = sub.add_parser("ownership", help="screen ownership registry utilities")
    own.add_argument("ownership_command", choices=["check"])
    own.add_argument("--days", type=int, default=90, help="window for 'rule matches nothing' warnings")
    own.set_defaults(func=cmd_ownership)
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
    from branchops.evaluation.cli import add_parser as add_eval_parser

    add_eval_parser(sub, cmd_eval)
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

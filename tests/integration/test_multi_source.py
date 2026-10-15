"""Several sources side by side: push ingestion per source, CSV import and polling, cross-source analysis."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from branchops.api.app import create_app
from branchops.application.commands.ingest_webhook import IngestIncidentEvent, IngestOutcome
from branchops.application.handlers.process_event import EventProcessor
from branchops.application.interfaces.source_adapter import FileImportSourceAdapter
from branchops.application.queries.read_models import IssueFilter, ReadModels
from branchops.config.settings import Settings
from branchops.container import Container
from branchops.domain.enums import InboxStatus
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.security.webhook_auth import sign_body
from branchops.workers.dispatcher import Dispatcher
from branchops.workers.poller import SourcePoller
from branchops.workers.processor import InboxItemRunner
from tests.helpers import SAMPLES_ROOT, ScriptedProvider, load_sample, make_settings

pytestmark = pytest.mark.postgres
JIRA_SECRET = "jira-secret"
SNOW_TOKEN = "snow-token"
CSV_MAPPING = {
    "summary": "Konu",
    "description": "Açıklama",
    "branch_code": "Sube Kodu",
    "application": "Uygulama",
    "status": "Durum",
    "priority": "Öncelik",
    "reporter": "Bildiren",
    "created_at": "Oluşturma",
    "updated_at": "Güncelleme",
    "group_key": "Ekip",
}


def sources(watch_directory: Path | None = None) -> list[dict[str, Any]]:
    return [
        {
            "id": "jira",
            "type": "jira_webhook",
            "field_mapping": {"branch_code": "customfield_10101", "application": "customfield_10102"},
        },
        {
            "id": "snow",
            "type": "servicenow_webhook",
            "auth": {"mode": "token", "secrets_env": "SNOW_TEST_SECRETS"},
            "field_mapping": {"branch_code": "u_branch_code", "application": "cmdb_ci"},
        },
        {
            "id": "csv",
            "type": "csv_import",
            "field_mapping": CSV_MAPPING,
            "options": {
                "id_column": "Kayıt No",
                **({"watch_directory": str(watch_directory)} if watch_directory else {}),
            },
        },
        {"id": "legacy", "type": "jira_webhook", "enabled": False},
    ]


@pytest.fixture
def settings(db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("JIRA_WEBHOOK_SECRETS", JIRA_SECRET)
    monkeypatch.setenv("SNOW_TEST_SECRETS", SNOW_TOKEN)
    return make_settings(
        database={"url": str(db.engine.url.render_as_string(hide_password=False))},
        worker={"concurrency": 1, "batch_size": 1},
        sources=sources(tmp_path / "inbox"),
    )


@pytest.fixture
def container(settings: Settings) -> Iterator[Container]:
    c = Container.build(settings)
    yield c
    c.close()


@pytest.fixture
def client(settings: Settings, container: Container) -> Iterator[TestClient]:
    with TestClient(create_app(settings, container)) as c:
        yield c


def snow_sample(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SAMPLES_ROOT / "servicenow" / f"{name}.json").read_text("utf-8"))
    return data


def post_json(client: TestClient, url: str, payload: dict[str, Any], headers: dict[str, str]) -> Any:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return client.post(url, content=body, headers={"Content-Type": "application/json", **headers})


def post_jira(client: TestClient, payload: dict[str, Any], url: str = "/api/ingest/jira") -> Any:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return client.post(
        url,
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature": sign_body(body, JIRA_SECRET)},
    )


def drain(container: Container, settings: Settings) -> None:
    workflow = container.build_workflow(ScriptedProvider())
    processor = EventProcessor(
        db=container.db,
        settings=settings,
        registry=container.registry,
        workflow=workflow,
        sources=container.sources,
    )
    for _ in range(60):
        dispatcher = Dispatcher(
            container.db, InboxItemRunner(container.db, processor, settings.worker, "t"), settings.worker, "t"
        )
        dispatcher.run_once()
        dispatcher._pool.shutdown(wait=True)
        with container.db.session() as s:
            pending = s.execute(
                text("SELECT count(*) FROM webhook_inbox WHERE status IN ('PENDING','PROCESSING')")
            ).scalar_one()
            s.execute(text("UPDATE webhook_inbox SET available_at = now() WHERE status = 'PENDING'"))
        if pending == 0:
            return
    raise AssertionError("inbox did not drain")


def import_csv(container: Container, settings: Settings, path: Path) -> dict[str, int]:
    adapter = container.sources.get("csv")
    assert isinstance(adapter, FileImportSourceAdapter)
    command = IngestIncidentEvent(container.db, settings, container.sources)
    counts: dict[str, int] = {}
    for event in adapter.read_file(path):
        outcome = command.execute("csv", event=event, correlation_id=uuid.uuid4().hex).outcome
        counts[outcome.value] = counts.get(outcome.value, 0) + 1
    return counts


def test_push_routes_per_source(client: TestClient) -> None:
    jira = load_sample("complete_incident")
    assert post_jira(client, jira).status_code == 202
    # The legacy URL is the same source: identical delivery -> duplicate.
    legacy = post_jira(client, jira, url="/api/webhooks/jira")
    assert legacy.status_code == 200 and legacy.json()["status"] == "duplicate"

    snow = snow_sample("incident_insert")
    assert (
        post_json(client, "/api/ingest/snow", snow, {"X-BranchOps-Webhook-Token": "wrong"}).status_code == 401
    )
    # The Jira secret is not valid for ServiceNow.
    assert (
        post_json(client, "/api/ingest/snow", snow, {"X-BranchOps-Webhook-Token": JIRA_SECRET}).status_code
        == 401
    )
    accepted = post_json(client, "/api/ingest/snow", snow, {"X-BranchOps-Webhook-Token": SNOW_TOKEN})
    assert accepted.status_code == 202 and accepted.json()["source"] == "snow"
    again = post_json(client, "/api/ingest/snow", snow, {"X-BranchOps-Webhook-Token": SNOW_TOKEN})
    assert again.json()["status"] == "duplicate"

    for unknown in ("nope", "legacy", "csv"):  # unknown, disabled, pull-only
        assert post_json(client, f"/api/ingest/{unknown}", {}, {}).status_code == 404
    bad = post_json(
        client,
        "/api/ingest/snow",
        {"event": "insert", "number": "bad key!"},
        {"X-BranchOps-Webhook-Token": SNOW_TOKEN},
    )
    assert bad.status_code == 400

    with TestClient(client.app) as c:  # same app, admin view of the inbox
        summary = c.get("/api/admin/inbox").json()
    assert {(r["source"], r["status"], r["count"]) for r in summary["by_source"]} == {
        ("jira", "PENDING", 1),
        ("snow", "PENDING", 1),
    }


def test_idempotency_is_scoped_per_source(container: Container, settings: Settings, db: Database) -> None:
    command = IngestIncidentEvent(container.db, settings, container.sources)
    payload = load_sample("complete_incident")
    headers = {"X-Atlassian-Webhook-Identifier": "same-delivery"}
    first = command.execute("jira", payload, correlation_id="c1", headers=headers)
    second = command.execute("jira", payload, correlation_id="c2", headers=headers)
    assert (first.outcome, second.outcome) == (IngestOutcome.ACCEPTED, IngestOutcome.DUPLICATE)
    assert second.event_id == first.event_id
    with db.session() as s:
        keys = s.execute(text("SELECT source, idempotency_key FROM webhook_inbox")).all()
    assert [(k.source, k.idempotency_key) for k in keys] == [("jira", "jira:delivery:same-delivery")]


def test_jira_and_csv_records_are_analysed_side_by_side(
    client: TestClient, container: Container, settings: Settings, db: Database
) -> None:
    assert post_jira(client, load_sample("complete_incident")).status_code == 202
    counts = import_csv(container, settings, SAMPLES_ROOT / "csv" / "branch_incidents.csv")
    assert counts == {"accepted": 5}
    drain(container, settings)

    with db.session() as s:
        statuses = s.execute(text("SELECT source, status, last_error FROM webhook_inbox")).all()
        assert all(r.status == InboxStatus.COMPLETED.value for r in statuses), statuses
        read = ReadModels(settings.scoring.low_quality_threshold)
        overview = read.overview(s, days=3650)
        assert overview.total_issues == 6
        assert {d["source"]: d["count"] for d in overview.source_distribution} == {"jira": 1, "csv": 5}

        csv_page = read.list_issues(s, IssueFilter(source="csv"))
        assert csv_page.total == 5
        assert {i["source"] for i in csv_page.items} == {"csv"}
        assert all(i["incident_key"].startswith("csv:IMP-") for i in csv_page.items)

        detail = read.issue_detail(s, "csv:IMP-1001")
        assert detail is not None
        assert detail["issue"]["source"] == "csv"
        assert detail["issue"]["external_key"] == "IMP-1001"
        latest = detail["latest_analysis"]
        assert latest["status"] in ("COMPLETED", "PARTIAL", "NEEDS_REVIEW")
        # Mapped CSV columns feed the canonical fields exactly like Jira custom fields do.
        assert latest["canonical_issue"]["fields"]["branch_code"]["value"] == "1234"
        assert latest["canonical_issue"]["source"] == "csv"

        jira = read.issue_detail(s, "BOPS-101")  # legacy bare key
        assert jira is not None and jira["issue"]["incident_key"] == "jira:BOPS-101"

        # Similarity is cross-source: the CSV EFT-4012 record repeats the Jira one (same branch/app/error).
        rels = {r["incident_key"]: r for r in detail["relationships"]}
        assert rels["jira:BOPS-101"]["relationship"] == "DUPLICATE_CANDIDATE"
        assert rels["jira:BOPS-101"]["source"] == "jira"
        assert {(x["incident_key"], x["source"]) for x in latest["similar_issues"]} == {
            ("jira:BOPS-101", "jira")
        }

    # Re-importing the same file is idempotent at the analysis level (no new analyses).
    with db.session() as s:
        before = s.execute(text("SELECT count(*) FROM analyses")).scalar_one()
    import_csv(container, settings, SAMPLES_ROOT / "csv" / "branch_incidents.csv")
    drain(container, settings)
    with db.session() as s:
        assert s.execute(text("SELECT count(*) FROM analyses")).scalar_one() == before

    api = client.get(
        "/api/issues", params={"source": "csv", "sort": "incident_key", "descending": False}
    ).json()
    assert [i["incident_key"] for i in api["items"]][:2] == ["csv:IMP-1001", "csv:IMP-1002"]
    assert client.get("/api/issues/csv:IMP-1001").json()["issue"]["source"] == "csv"
    assert client.post("/api/issues/csv:IMP-1001/reanalyze").status_code == 202
    assert client.get("/api/issues/csv:IMP-9999").status_code == 404
    assert client.get("/api/issues/csv:bad key").status_code == 400


def test_poller_reads_the_watch_directory_once(
    container: Container, settings: Settings, db: Database, tmp_path: Path
) -> None:
    watch = tmp_path / "inbox"
    watch.mkdir()
    target = watch / "branch_incidents.csv"
    shutil.copy(SAMPLES_ROOT / "csv" / "branch_incidents.csv", target)
    os.utime(target, (1_791_000_000, 1_791_000_000))

    poller = SourcePoller(container.db, settings, container.sources)
    assert [a.source_id for a in poller.adapters()] == ["csv"]
    assert poller.run_once(force=True) == 5
    with db.session() as s:
        cursor = s.execute(text("SELECT cursor FROM source_checkpoints WHERE source_id = 'csv'")).scalar_one()
        assert cursor.startswith("2026-")
        assert s.execute(text("SELECT count(*) FROM webhook_inbox WHERE source = 'csv'")).scalar_one() == 5
    assert poller.run_once(force=True) == 0  # nothing newer than the checkpoint
    assert poller.run_once() == 0  # not due yet (poll_interval_seconds)

    os.utime(target, (1_791_000_600, 1_791_000_600))  # file rewritten later
    assert poller.run_once(force=True) == 5
    with db.session() as s:  # unchanged rows, new mtime: new inbox events, processed as no-ops later
        assert s.execute(text("SELECT count(*) FROM webhook_inbox WHERE source = 'csv'")).scalar_one() == 10


def test_cli_import(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db: Database
) -> None:
    from branchops.cli import main

    config = tmp_path / "branchops.yaml"
    config.write_text(
        json.dumps(
            {
                "app": {"environment": "test"},
                "database": {"url": settings.database.url},
                "llm": {"provider": "mock"},
                "templates": {"directory": settings.templates.directory},
                # JSONL rows have no id column: content-addressed keys.
                "sources": [{"id": "csv", "type": "csv_import", "field_mapping": CSV_MAPPING}],
            }
        ),
        encoding="utf-8",
    )
    jsonl = SAMPLES_ROOT / "csv" / "branch_incidents.jsonl"
    assert main(["--config", str(config), "import", "--source", "csv", "--dry-run", str(jsonl)]) == 0
    with db.session() as s:
        assert s.execute(text("SELECT count(*) FROM webhook_inbox")).scalar_one() == 0
    assert main(["--config", str(config), "import", "--source", "csv", str(jsonl)]) == 0
    assert main(["--config", str(config), "import", "--source", "jira", str(jsonl)]) == 1
    assert main(["--config", str(config), "import", "--source", "csv", str(tmp_path / "missing.csv")]) == 1
    with db.session() as s:
        assert s.execute(text("SELECT count(*) FROM webhook_inbox WHERE source = 'csv'")).scalar_one() == 3
        assert (
            s.execute(text("SELECT count(*) FROM audit_log WHERE action = 'source_import'")).scalar_one() == 1
        )

"""Vertical slice: webhook → PostgreSQL inbox → worker → (mock) LLM workflow → scores → read models."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from branchops.api.app import create_app
from branchops.application.handlers.process_event import EventProcessor
from branchops.application.queries.read_models import IssueFilter, ReadModels
from branchops.config.settings import Settings
from branchops.container import Container
from branchops.domain.enums import InboxStatus
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.persistence.template_store import TemplateImmutabilityError, TemplateStore
from branchops.infrastructure.security.webhook_auth import sign_body
from branchops.workers.dispatcher import Dispatcher
from branchops.workers.processor import InboxItemRunner
from tests.helpers import ScriptedProvider, load_sample, make_settings, unavailable

pytestmark = pytest.mark.postgres
SECRET = "test-webhook-secret"


@pytest.fixture
def settings(db: Database, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("JIRA_WEBHOOK_SECRETS", SECRET)
    # One event at a time keeps similarity results deterministic (candidates must be analysed first).
    return make_settings(
        database={"url": str(db.engine.url.render_as_string(hide_password=False))},
        worker={"concurrency": 1, "batch_size": 1},
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


def post(client: TestClient, payload: dict[str, Any], *, sign: bool = True) -> Any:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if sign:
        headers["X-Hub-Signature"] = sign_body(body, SECRET)
    return client.post("/api/webhooks/jira", content=body, headers=headers)


def make_dispatcher(container: Container, settings: Settings, provider: Any = None) -> Dispatcher:
    workflow = container.build_workflow(provider or ScriptedProvider())
    processor = EventProcessor(
        db=container.db, settings=settings, registry=container.registry, workflow=workflow
    )
    runner = InboxItemRunner(container.db, processor, settings.worker, "test-worker")
    return Dispatcher(container.db, runner, settings.worker, "test-worker")


def drain(dispatcher: Dispatcher, db: Database, max_rounds: int = 50) -> None:
    for _ in range(max_rounds):
        dispatcher.run_once()
        dispatcher._pool.shutdown(wait=True)  # deterministic: wait for this round
        dispatcher._pool = type(dispatcher._pool)(max_workers=2)
        with db.session() as s:
            pending = s.execute(
                text("SELECT count(*) FROM webhook_inbox WHERE status IN ('PENDING','PROCESSING')")
            ).scalar_one()
            s.execute(text("UPDATE webhook_inbox SET available_at = now() WHERE status = 'PENDING'"))
        if pending == 0:
            return
    raise AssertionError("inbox did not drain")


def inbox_rows(db: Database) -> list[Any]:
    with db.session() as s:
        return list(s.execute(text("SELECT * FROM webhook_inbox ORDER BY received_at")).all())


def test_webhook_api_validation(client: TestClient) -> None:
    payload = load_sample("complete_incident")
    assert post(client, payload, sign=False).status_code == 401
    bad_sig = client.post(
        "/api/webhooks/jira",
        content=b"{}",
        headers={"X-Hub-Signature": "sha256=00", "Content-Type": "application/json"},
    )
    assert bad_sig.status_code == 401
    too_big = b"{" + b" " * 2_000_000 + b"}"
    r = client.post(
        "/api/webhooks/jira",
        content=too_big,
        headers={"Content-Type": "application/json", "X-Hub-Signature": sign_body(too_big, SECRET)},
    )
    assert r.status_code == 413
    not_json = b"not json"
    r = client.post(
        "/api/webhooks/jira",
        content=not_json,
        headers={"Content-Type": "application/json", "X-Hub-Signature": sign_body(not_json, SECRET)},
    )
    assert r.status_code == 400
    r = client.post("/api/webhooks/jira", content=b"<x/>", headers={"Content-Type": "application/xml"})
    assert r.status_code == 415
    bad_key = post(client, {"webhookEvent": "jira:issue_created", "issue": {"key": "DROP TABLE"}})
    assert bad_key.status_code == 400

    accepted = post(client, payload)
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "accepted"
    assert accepted.headers["X-Correlation-ID"] == accepted.json()["correlation_id"]
    duplicate = post(client, payload)
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    ignored = post(client, {"webhookEvent": "sprint_started"})
    assert ignored.json()["status"] == "ignored"


def test_vertical_slice(client: TestClient, container: Container, settings: Settings, db: Database) -> None:
    for name in (
        "complete_incident",
        "duplicate_issue",
        "related_different_issue",
        "generic_description",
        "missing_branch_code",
        "prompt_injection",
        "pii_incident",
        "incomplete_incident",
    ):
        assert post(client, load_sample(name)).status_code == 202
    provider = ScriptedProvider()
    drain(make_dispatcher(container, settings, provider), db)

    rows = inbox_rows(db)
    assert all(r.status == InboxStatus.COMPLETED.value for r in rows), [
        (r.issue_key, r.last_error) for r in rows
    ]
    assert all(r.payload_redacted for r in rows)

    read = ReadModels(settings.scoring.low_quality_threshold)
    with db.session() as s:
        overview = read.overview(s, days=3650)
        assert overview.total_issues == 8
        assert overview.analysed_issues == 8
        assert overview.needs_review >= 1

        complete = read.issue_detail(s, "BOPS-101")
        assert complete is not None
        latest = complete["latest_analysis"]
        assert latest["status"] == "COMPLETED"
        assert latest["template_id"] == "transaction-incident"
        assert latest["completeness_score"] >= 90
        assert latest["model_id"] == "scripted-model"
        assert latest["scoring_policy_version"] == settings.scoring.version

        generic = read.issue_detail(s, "BOPS-103")
        assert generic is not None
        assert generic["latest_analysis"]["quality_score"] < 30
        assert generic["latest_analysis"]["recommendations"]

        # Similarity: BOPS-104 is processed after BOPS-101 (same branch, app, error, 20 minutes apart).
        dup = read.issue_detail(s, "BOPS-104")
        assert dup is not None
        rels = {r["issue_key"]: r["relationship"] for r in dup["relationships"]}
        assert rels.get("BOPS-101") == "DUPLICATE_CANDIDATE"
        related = read.issue_detail(s, "BOPS-105")
        assert related is not None
        assert {r["issue_key"]: r["relationship"] for r in related["relationships"]}.get(
            "BOPS-101"
        ) == "RELATED"

        injected = read.issue_detail(s, "BOPS-108")
        assert injected is not None
        assert injected["latest_analysis"]["status"] == "NEEDS_REVIEW"
        assert injected["latest_analysis"]["security_flags"]

        # PII is masked in storage (issue snapshot, inbox payload after processing).
        pii = s.execute(
            text("SELECT description, snapshot::text FROM issues WHERE issue_key = 'BOPS-110'")
        ).one()
        stored_payload = s.execute(
            text("SELECT payload::text FROM webhook_inbox WHERE issue_key = 'BOPS-110'")
        ).scalar_one()
        for secret in ("10000000146", "4111 1111 1111 1111", "0532 123 45 67"):
            assert secret not in pii.description
            assert secret not in pii.snapshot
            assert secret not in stored_payload

        page = read.list_issues(s, IssueFilter(search="EFT", sort="quality"))
        assert {"BOPS-101", "BOPS-104"} <= {i["issue_key"] for i in page.items}
        low = read.list_issues(s, IssueFilter(low_quality_only=True))
        assert "BOPS-103" in {i["issue_key"] for i in low.items}

        analytics = read.quality_analytics(s, days=3650)
        assert analytics["most_missing_fields"]
        assert any(r["error_signature"] == "gişe ekranı|EFT-4012" for r in analytics["recurring"])

        executions = read.executions(s)
        assert executions.total == 8
        steps = read.execution_steps(s, executions.items[0]["analysis_id"])
        assert next(st["step"] for st in steps) == "normalize"


def test_partial_update_out_of_order_and_comments(
    client: TestClient, container: Container, settings: Settings, db: Database
) -> None:
    dispatcher = make_dispatcher(container, settings)
    post(client, load_sample("incomplete_incident"))
    drain(dispatcher, db)
    with db.session() as s:
        first = ReadModels(50).issue_detail(s, "BOPS-102")
    assert first is not None
    first_completeness = first["latest_analysis"]["completeness_score"]

    # Newer partial update, then an older (stale) update delivered late.
    post(client, load_sample("partial_update"))
    post(client, load_sample("stale_update"))
    drain(dispatcher, db)
    with db.session() as s:
        detail = ReadModels(50).issue_detail(s, "BOPS-102")
    assert detail is not None
    assert "Ingenico" in detail["issue"]["description"]  # stale description did not overwrite
    assert detail["issue"]["summary"] == "Kart okuyucu kartı okumuyor"  # not erased by partial payload
    assert detail["latest_analysis"]["completeness_score"] > first_completeness
    assert len(detail["score_history"]) == 2  # the stale event caused no new analysis

    post(client, load_sample("comment_created"))
    drain(dispatcher, db)
    with db.session() as s:
        detail = ReadModels(50).issue_detail(s, "BOPS-102")
    assert detail is not None
    assert [c["id"] for c in detail["comments"]] == ["50001"]
    assert len(detail["score_history"]) == 3

    post(client, load_sample("comment_deleted"))
    drain(dispatcher, db)
    with db.session() as s:
        detail = ReadModels(50).issue_detail(s, "BOPS-102")
        body = s.execute(text("SELECT body, is_deleted FROM issue_comments WHERE comment_id='50001'")).one()
    assert detail is not None
    assert detail["comments"] == []
    assert body.is_deleted and body.body == ""  # redacted per retention policy


def test_issue_deletion_soft_and_purge(
    client: TestClient, container: Container, settings: Settings, db: Database
) -> None:
    post(client, load_sample("generic_description"))
    drain(make_dispatcher(container, settings), db)
    post(client, load_sample("issue_deleted"))
    drain(make_dispatcher(container, settings), db)
    with db.session() as s:
        assert ReadModels(50).issue_detail(s, "BOPS-103") is None
        assert s.execute(text("SELECT is_deleted FROM issues WHERE issue_key='BOPS-103'")).scalar_one()
        assert (
            s.execute(
                text("SELECT count(*) FROM audit_log WHERE action='issue_deleted:soft_delete'")
            ).scalar_one()
            == 1
        )

    purge_settings = make_settings(
        database={"url": settings.database.url}, retention={"on_issue_deleted": "purge"}
    )
    with db.session() as s:
        s.execute(text("TRUNCATE webhook_inbox, issues CASCADE"))
    post(client, load_sample("generic_description"))
    drain(make_dispatcher(container, purge_settings), db)
    post(client, load_sample("issue_deleted"))
    drain(make_dispatcher(container, purge_settings), db)
    with db.session() as s:
        tombstone = s.execute(text("SELECT is_deleted, summary, description FROM issues")).one()
        assert tombstone.is_deleted and tombstone.summary is None and tombstone.description is None
        assert s.execute(text("SELECT count(*) FROM analyses")).scalar_one() == 0


def test_model_outage_retries_then_degrades(
    container: Container, settings: Settings, db: Database, client: TestClient
) -> None:
    post(client, load_sample("complete_incident"))
    provider = ScriptedProvider({"ClassificationOutput": [unavailable] * 100})
    dispatcher = make_dispatcher(container, settings, provider)
    drain(dispatcher, db)
    rows = inbox_rows(db)
    assert rows[0].status == "COMPLETED"
    assert rows[0].attempts == settings.worker.max_attempts  # retried, final attempt degraded
    with db.session() as s:
        detail = ReadModels(50).issue_detail(s, "BOPS-101")
    assert detail is not None
    assert detail["latest_analysis"]["status"] == "NEEDS_REVIEW"
    assert detail["latest_analysis"]["template_id"] == "generic-operational-incident"


def test_reanalysis_and_rbac(
    client: TestClient, container: Container, settings: Settings, db: Database
) -> None:
    post(client, load_sample("complete_incident"))
    drain(make_dispatcher(container, settings), db)
    r = client.post("/api/issues/BOPS-101/reanalyze")
    assert r.status_code == 202
    drain(make_dispatcher(container, settings), db)
    detail = client.get("/api/issues/BOPS-101").json()
    assert len(detail["score_history"]) == 2
    assert client.get("/api/issues/BOPS-999").status_code == 404
    assert client.get("/api/issues/not-a-key").status_code == 400
    assert client.get("/api/issues?search=EFT").json()["total"] == 1
    assert client.get("/api/analytics/overview").json()["total_issues"] == 1
    assert client.get("/api/reports/summary").json()["source"] == "deterministic"
    assert client.get("/api/executions").json()["total"] == 2
    assert client.get("/health/ready").status_code == 200
    assert "branchops_webhook_requests" in client.get("/metrics").text

    with db.session() as s:
        assert (
            s.execute(text("SELECT count(*) FROM audit_log WHERE action='reanalysis_requested'")).scalar_one()
            == 1
        )


def test_rbac_in_oauth_proxy_mode(settings: Settings, container: Container) -> None:
    proxied = settings.model_copy(
        update={
            "security": settings.security.model_copy(
                update={"auth_mode": "oauth_proxy", "trusted_proxy_hosts": ["testclient"]}
            )
        }
    )
    with TestClient(create_app(proxied, container)) as c:
        assert c.get("/api/issues").status_code == 401
        viewer = {"X-Forwarded-User": "v", "X-Forwarded-Groups": "branchops-viewers"}
        assert c.get("/api/issues", headers=viewer).status_code == 200
        assert c.post("/api/issues/BOPS-1/reanalyze", headers=viewer).status_code == 403
        assert c.get("/api/admin/inbox", headers=viewer).status_code == 403
        admin = {"X-Forwarded-User": "a", "X-Forwarded-Groups": "branchops-admins"}
        assert c.get("/api/admin/inbox", headers=admin).status_code == 200
        # Webhooks use their own authentication, not the SSO proxy.
        assert (
            c.post(
                "/api/webhooks/jira", content=b"{}", headers={"Content-Type": "application/json"}
            ).status_code
            == 401
        )


def test_template_immutability(container: Container, db: Database) -> None:
    store = TemplateStore()
    with db.session() as s:
        published = store.sync(s, container.registry)
    assert len(published) == 7
    with db.session() as s:
        assert store.sync(s, container.registry) == []
        s.execute(
            text(
                "UPDATE template_versions SET content_sha256 = 'tampered' "
                "WHERE template_id = 'network-incident'"
            )
        )
    with pytest.raises(TemplateImmutabilityError), db.session() as s:
        store.sync(s, container.registry)
    with db.session() as s:
        loaded = store.load(s, "transaction-incident", "1.0.0")
    assert loaded is not None
    assert loaded.content_hash() == container.registry.get("transaction-incident", "1.0.0").content_hash()  # type: ignore[union-attr]


def test_postgres_checkpoint_resume(settings: Settings, db: Database, client: TestClient) -> None:
    pg_settings = make_settings(
        database={"url": settings.database.url},
        workflow={"checkpointer": "postgres"},
        worker={"concurrency": 1, "batch_size": 1},
    )
    from branchops.container import create_checkpointer

    checkpointer, pool = create_checkpointer(pg_settings)
    checkpointer.setup()
    pool.close()
    pg_container = Container.build(pg_settings)
    try:
        post(client, load_sample("complete_incident"))
        provider = ScriptedProvider({"ExtractionOutput": [unavailable, unavailable]})
        dispatcher = make_dispatcher(pg_container, pg_settings, provider)
        drain(dispatcher, db)
        assert len(provider.calls_for("ClassificationOutput")) == 1  # resumed after the failed attempt
        assert len(provider.calls_for("ExtractionOutput")) == 3
        rows = inbox_rows(db)
        assert rows[0].status == "COMPLETED" and rows[0].attempts == 2
        with db.session() as s:
            leftover = s.execute(text("SELECT count(*) FROM checkpoints")).scalar_one()
        assert leftover == 0  # thread cleaned up after success
    finally:
        pg_container.close()


def test_retry_after_snapshot_merge_still_analyses(
    client: TestClient, container: Container, settings: Settings, db: Database
) -> None:
    """Regression: the first attempt merges the snapshot then fails; the retry must still analyse."""
    post(client, load_sample("incomplete_incident"))
    drain(make_dispatcher(container, settings), db)
    post(client, load_sample("partial_update"))
    provider = ScriptedProvider({"ClassificationOutput": [unavailable, unavailable]})
    drain(make_dispatcher(container, settings, provider), db)
    rows = inbox_rows(db)
    assert rows[-1].attempts == 2
    assert rows[-1].outcome.startswith("analysed")
    with db.session() as s:
        detail = ReadModels(50).issue_detail(s, "BOPS-102")
    assert detail is not None
    assert len(detail["score_history"]) == 2


def test_comment_tombstone_before_creation(
    client: TestClient, container: Container, settings: Settings, db: Database
) -> None:
    post(client, load_sample("incomplete_incident"))
    drain(make_dispatcher(container, settings), db)
    post(client, load_sample("comment_deleted"))  # delete arrives before create
    drain(make_dispatcher(container, settings), db)
    post(client, load_sample("comment_created"))
    drain(make_dispatcher(container, settings), db)
    with db.session() as s:
        detail = ReadModels(50).issue_detail(s, "BOPS-102")
    assert detail is not None
    assert detail["comments"] == []


def test_purged_issue_not_resurrected_by_late_event(
    client: TestClient, container: Container, settings: Settings, db: Database
) -> None:
    purge = make_settings(
        database={"url": settings.database.url},
        retention={"on_issue_deleted": "purge"},
        worker={"concurrency": 1, "batch_size": 1},
    )
    post(client, load_sample("generic_description"))
    drain(make_dispatcher(container, purge), db)
    post(client, load_sample("issue_deleted"))
    drain(make_dispatcher(container, purge), db)
    late = load_sample("generic_description")
    late["webhookEvent"] = "jira:issue_updated"
    late["timestamp"] += 1
    post(client, late)
    drain(make_dispatcher(container, purge), db)
    with db.session() as s:
        row = s.execute(text("SELECT is_deleted, summary, description FROM issues")).one()
        assert row.is_deleted and row.summary is None and row.description is None
        assert s.execute(text("SELECT count(*) FROM analyses")).scalar_one() == 0


def test_live_progress_is_recorded_during_the_run(
    client: TestClient, container: Container, settings: Settings, db: Database
) -> None:
    """While the workflow runs the analysis row is RUNNING with the current node; afterwards final."""
    from branchops.application.queries.read_models import live_activity

    seen: dict[str, object] = {}

    def spy(request: Any) -> str | Exception:
        with db.session() as s:
            seen["live"] = live_activity(s)
        return ScriptedProvider()._mock.complete(request).content

    post(client, load_sample("complete_incident"))
    provider = ScriptedProvider({"ExtractionOutput": [spy]})
    drain(make_dispatcher(container, settings, provider), db)

    live = seen["live"]
    assert isinstance(live, dict)
    (run,) = live["running"]
    assert run["incident_key"] == "jira:BOPS-101"
    assert run["current_step"] == "extract"
    assert run["node_states"]["classify"] == "succeeded"
    assert run["node_states"]["extract"] == "running"
    assert run["node_states"]["persist"] == "pending"
    assert run["worker_id"]
    lanes = {lane["node"]: lane for lane in live["lanes"]}
    assert lanes["extract"]["busy"][0]["incident_key"] == "jira:BOPS-101"

    with db.session() as s:
        after = live_activity(s)
    assert after["running"] == []
    assert after["recent"][0]["status"] == "COMPLETED"
    assert after["recent"][0]["node_states"]["persist"] == "succeeded"
    assert lanes["classify"]["runs_24h"] >= 1


def test_failed_attempt_marks_run_failed_then_retry_completes(
    client: TestClient, container: Container, settings: Settings, db: Database
) -> None:
    post(client, load_sample("complete_incident"))
    provider = ScriptedProvider({"ClassificationOutput": [unavailable, unavailable]})
    dispatcher = make_dispatcher(container, settings, provider)
    dispatcher.run_once()
    dispatcher._pool.shutdown(wait=True)
    with db.session() as s:
        status = s.execute(text("SELECT status FROM analyses")).scalar_one()
    assert status == "FAILED"
    drain(make_dispatcher(container, settings, provider), db)
    with db.session() as s:
        rows = s.execute(text("SELECT status, current_step FROM analyses")).all()
    assert [(r.status, r.current_step) for r in rows] == [("COMPLETED", None)]

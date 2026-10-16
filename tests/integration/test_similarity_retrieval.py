"""Candidate retrieval on PostgreSQL: Turkish synonyms via the lexicon, stored embeddings (REAL[])."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from branchops.application.commands.ingest_webhook import IngestIncidentEvent
from branchops.application.handlers.process_event import EventProcessor
from branchops.config.settings import Settings
from branchops.container import Container
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.persistence.embedding_repository import EmbeddingRepository
from branchops.infrastructure.persistence.issue_repository import IssueRepository
from branchops.workers.dispatcher import Dispatcher
from branchops.workers.processor import InboxItemRunner
from tests.helpers import ScriptedProvider, load_sample, make_settings
from tests.integration.test_end_to_end import drain

pytestmark = pytest.mark.postgres


@pytest.fixture
def settings(db: Database) -> Settings:
    return make_settings(
        database={"url": str(db.engine.url.render_as_string(hide_password=False))},
        worker={"concurrency": 1, "batch_size": 1},
    )


@pytest.fixture
def container(settings: Settings) -> Iterator[Container]:
    c = Container.build(settings)
    yield c
    c.close()


def ingest(container: Container, settings: Settings, key: str, summary: str, description: str) -> None:
    payload: dict[str, Any] = load_sample("incomplete_incident")
    payload["issue"] = {**payload["issue"], "key": key, "id": key}
    payload["issue"]["fields"] = {
        **payload["issue"]["fields"],
        "summary": summary,
        "description": description,
    }
    IngestIncidentEvent(container.db, settings, container.sources).execute(
        "jira", payload, correlation_id=uuid.uuid4().hex[:16]
    )


def analyse(container: Container, settings: Settings, db: Database) -> None:
    processor = EventProcessor(
        db=container.db,
        settings=settings,
        registry=container.registry,
        workflow=container.build_workflow(ScriptedProvider()),
    )
    drain(
        Dispatcher(
            container.db, InboxItemRunner(container.db, processor, settings.worker, "w"), settings.worker, "w"
        ),
        db,
    )


def test_synonyms_are_retrieved_by_full_text_search(
    container: Container, settings: Settings, db: Database
) -> None:
    ingest(
        container,
        settings,
        "BOPS-301",
        "Banknot sayım cihazı iletişim zaman aşımı hatası",
        "Şube 2104 vezne 2'de banknot sayım cihazı iletişim hatası veriyor.",
    )
    ingest(
        container,
        settings,
        "BOPS-302",
        "Yazıcı kağıt sıkıştırıyor",
        "Şube 4410 lazer yazıcı kağıt sıkıştırıyor.",
    )
    analyse(container, settings, db)
    with db.session() as s:
        found = IssueRepository().find_candidates(
            s, "jira:BOPS-999", "Para sayma makinesi bağlantısı kesiliyor", None, 3650, 10
        )
    assert [c.incident_key for c in found] == ["jira:BOPS-301"]


def test_embeddings_roundtrip_and_nearest(container: Container, settings: Settings, db: Database) -> None:
    ingest(container, settings, "BOPS-311", "Para sayma makinesi bağlantısı kesiliyor", "Şube 2104 vezne 2.")
    ingest(container, settings, "BOPS-312", "Yazıcı kağıt sıkıştırıyor", "Şube 4410 lazer yazıcı.")
    analyse(container, settings, db)
    repo = EmbeddingRepository()
    with db.session() as s:
        repo.upsert(s, "jira:BOPS-311", "emb-a", [1.0, 0.0, 0.0], "para sayma")
        repo.upsert(s, "jira:BOPS-312", "emb-a", [0.0, 1.0, 0.0], "yazıcı")
        repo.upsert(s, "jira:BOPS-311", "emb-other-model", [0.0, 0.0, 1.0], "x")
        repo.upsert(s, "jira:BOPS-311", "emb-a", [0.9, 0.1, 0.0], "para sayma (updated)")  # upsert
    with db.session() as s:
        hits = repo.nearest(
            s,
            model="emb-a",
            vector=[1.0, 0.0, 0.0],
            exclude="jira:BOPS-999",
            lookback_days=30,
            pool=100,
            top_k=5,
            min_score=0.5,
        )
    assert [(c.incident_key, round(score, 2)) for c, score in hits] == [("jira:BOPS-311", 0.99)]
    assert hits[0][0].semantic_score is not None and hits[0][0].category is not None
    # Purging an issue removes its embeddings.
    with db.session() as s:
        IssueRepository().purge(s, "jira:BOPS-311")
    with db.session() as s:
        hits = repo.nearest(
            s,
            model="emb-a",
            vector=[1.0, 0.0, 0.0],
            exclude="x",
            lookback_days=30,
            pool=100,
            top_k=5,
            min_score=0.0,
        )
    assert [c.incident_key for c, _ in hits] == ["jira:BOPS-312"]

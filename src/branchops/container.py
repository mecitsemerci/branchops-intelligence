"""Composition root: builds the object graph for each process from ``Settings``."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from branchops.agents.base import StructuredCaller
from branchops.application.interfaces.model_provider import ModelProvider
from branchops.application.interfaces.source_adapter import SourceRegistry
from branchops.config.settings import Settings
from branchops.domain.services.masking import SensitiveDataMasker
from branchops.infrastructure.database.session import Database, create_db_engine
from branchops.infrastructure.llm.mock_provider import MockModelProvider
from branchops.infrastructure.llm.openai_compatible import OpenAICompatibleModelProvider
from branchops.infrastructure.persistence.analysis_repository import DatabaseAnalysisSink
from branchops.infrastructure.persistence.issue_repository import IssueRepository, SessionBoundHistoryReader
from branchops.infrastructure.persistence.progress_repository import DatabaseProgressReporter
from branchops.infrastructure.sources import build_source_registry
from branchops.templates.registry import TemplateRegistry

logger = logging.getLogger(__name__)


def create_model_provider(settings: Settings) -> ModelProvider:
    if settings.llm.provider == "mock":
        logger.warning("using MOCK model provider (development/test only)")
        return MockModelProvider()
    provider = OpenAICompatibleModelProvider(settings.llm, settings.network)
    if settings.llm.capability_detection == "probe":
        provider.probe_capabilities()
    return provider


def sqlalchemy_url_to_libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def create_checkpointer(settings: Settings) -> tuple[Any, Any]:
    """Returns (checkpointer, closeable resource or None)."""
    mode = settings.workflow.checkpointer
    if mode == "none":
        return None, None
    if mode == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver(), None
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        sqlalchemy_url_to_libpq(settings.database.url),
        min_size=1,
        max_size=max(2, settings.worker.concurrency),
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=True,
    )
    return PostgresSaver(pool), pool  # type: ignore[arg-type]


@dataclass
class Container:
    settings: Settings
    db: Database
    registry: TemplateRegistry
    sources: SourceRegistry = field(default_factory=SourceRegistry)
    _closeables: list[Any] = field(default_factory=list)

    @classmethod
    def build(cls, settings: Settings) -> Container:
        db = Database(create_db_engine(settings.database))
        registry = TemplateRegistry.from_directory(Path(settings.templates.directory))
        return cls(settings=settings, db=db, registry=registry, sources=build_source_registry(settings))

    def build_workflow(self, provider: ModelProvider | None = None) -> Any:
        from branchops.workflows.issue_analysis import IssueAnalysisWorkflow

        provider = provider or create_model_provider(self.settings)
        caller = StructuredCaller(
            provider,
            max_repair_attempts=self.settings.workflow.max_repair_attempts,
            max_output_tokens=self.settings.llm.max_output_tokens,
        )
        checkpointer, resource = create_checkpointer(self.settings)
        if resource is not None:
            self._closeables.append(resource)
        return IssueAnalysisWorkflow(
            caller=caller,
            registry=self.registry,
            policy=self.settings.scoring,
            history=SessionBoundHistoryReader(self.db, IssueRepository()),
            sink=DatabaseAnalysisSink(self.db),
            settings=self.settings.workflow,
            checkpointer=checkpointer,
            progress=DatabaseProgressReporter(self.db),
            text_filter=SensitiveDataMasker(self.settings.security.masking_extra_patterns).mask_str,
        )

    def close(self) -> None:
        for c in self._closeables:
            try:
                c.close()
            except Exception:
                logger.warning("resource close failed", exc_info=True)
        self.db.engine.dispose()

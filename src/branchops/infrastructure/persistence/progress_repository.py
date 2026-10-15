"""Writes live workflow progress to ``analyses`` / ``agent_steps`` while a run is in flight.

Progress writes are best effort: a failure here is logged and never breaks the analysis itself.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable

from sqlalchemy import delete, func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from branchops.application.interfaces.repositories import RunInfo
from branchops.domain.entities.analysis import StepRecord
from branchops.domain.enums import AnalysisStatus
from branchops.infrastructure.database.models import AgentStep, Analysis
from branchops.infrastructure.database.session import Database

logger = logging.getLogger(__name__)


class DatabaseProgressReporter:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _write(self, action: str, fn: Callable[[Session], object]) -> None:
        try:
            with self._db.session() as session:
                fn(session)
        except Exception:
            logger.warning("progress update failed", extra={"action": action}, exc_info=True)

    def run_started(self, info: RunInfo) -> None:
        def write(s: Session) -> None:
            values = {
                "id": uuid.UUID(info.analysis_id),
                "issue_key": info.issue_key,
                "inbox_event_id": uuid.UUID(info.inbox_event_id) if info.inbox_event_id else None,
                "correlation_id": info.correlation_id,
                "status": AnalysisStatus.RUNNING.value,
                "input_hash": info.input_hash,
                "prompt_version": info.prompt_version,
                "model_id": info.model_id,
                "started_at": info.started_at,
                "worker_id": info.worker_id,
                "heartbeat_at": func.now(),
                "current_step": None,
            }
            if not info.resumed:
                # A fresh (re)run starts with an empty live step list; a resumed run keeps the steps of
                # the nodes restored from the checkpoint.
                s.execute(delete(AgentStep).where(AgentStep.analysis_id == uuid.UUID(info.analysis_id)))
            stmt = insert(Analysis).values(**values)
            s.execute(
                stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "status": stmt.excluded.status,
                        "worker_id": stmt.excluded.worker_id,
                        "heartbeat_at": stmt.excluded.heartbeat_at,
                        "errors": None,
                    },
                )
            )

        self._write("run_started", write)

    def step_started(self, analysis_id: str, step: str) -> None:
        self._write(
            "step_started",
            lambda s: s.execute(
                update(Analysis)
                .where(Analysis.id == uuid.UUID(analysis_id))
                .values(current_step=step, heartbeat_at=func.now())
            ),
        )

    def step_finished(self, analysis_id: str, record: StepRecord) -> None:
        def write(s: Session) -> None:
            aid = uuid.UUID(analysis_id)
            s.add(
                AgentStep(
                    analysis_id=aid,
                    step=record.step,
                    status=record.status.value,
                    started_at=record.started_at,
                    duration_ms=record.duration_ms,
                    attempts=record.attempts,
                    model_id=record.model_id,
                    output_mode=record.output_mode,
                    prompt_tokens=record.prompt_tokens,
                    completion_tokens=record.completion_tokens,
                    error=record.error,
                )
            )
            s.execute(update(Analysis).where(Analysis.id == aid).values(heartbeat_at=func.now()))

        self._write("step_finished", write)

    def run_failed(self, analysis_id: str, error: str) -> None:
        self._write(
            "run_failed",
            lambda s: s.execute(
                update(Analysis)
                .where(Analysis.id == uuid.UUID(analysis_id), Analysis.status == AnalysisStatus.RUNNING.value)
                .values(
                    status=AnalysisStatus.FAILED.value,
                    current_step=None,
                    errors=[error[:1000]],
                    heartbeat_at=func.now(),
                    completed_at=func.now(),
                )
            ),
        )

"""Pull-source poller: one leader-elected process reads every enabled pull source on its own interval and
writes the events to the inbox (the regular workers do the rest).

Leadership is a session-level ``pg_try_advisory_lock`` held on a dedicated connection, so standby replicas
take over within one poll interval when the leader dies. Progress is stored per source in
``source_checkpoints`` (the newest event timestamp read); inbox idempotency makes re-reads harmless.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, func, select

from branchops.application.commands.ingest_webhook import IngestIncidentEvent, IngestOutcome
from branchops.application.interfaces.source_adapter import PullSourceAdapter, SourceRegistry
from branchops.config.settings import Settings
from branchops.domain.entities.incident import IncidentEvent
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.persistence.checkpoint_repository import CheckpointRepository

logger = logging.getLogger(__name__)

LEADER_LOCK_NAME = "branchops:source-poller"


def event_cursor(event: IncidentEvent) -> datetime | None:
    """Position of an event in its source's stream (emission time first, record update time second)."""
    return event.event_timestamp or event.source_updated_at


def next_cursor(events: list[IncidentEvent], previous: datetime | None) -> datetime | None:
    stamps = [c for c in (event_cursor(e) for e in events) if c is not None]
    return max([*stamps, *([previous] if previous else [])], default=None)


class SourcePoller:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        sources: SourceRegistry,
        *,
        only: str | None = None,
        batch_limit: int = 5000,
        clock: Any = time.monotonic,
    ) -> None:
        self._db = db
        self._settings = settings
        self._sources = sources
        self._only = only
        self._limit = batch_limit
        self._clock = clock
        self._ingest = IngestIncidentEvent(db, settings, sources)
        self._checkpoints = CheckpointRepository()
        self._last_poll: dict[str, float] = {}
        self._stop = threading.Event()
        self.last_loop_at = time.monotonic()
        self.db_ok = False
        self.is_leader = False

    def stop(self) -> None:
        self._stop.set()

    def adapters(self) -> list[PullSourceAdapter]:
        adapters = self._sources.pull_sources()
        if self._only is not None:
            adapters = [a for a in adapters if a.source_id == self._only]
        return adapters

    def poll_source(self, adapter: PullSourceAdapter) -> dict[str, int]:
        with self._db.session() as s:
            since = self._checkpoints.get_datetime(s, adapter.source_id)
        # Not truncated here: adapters only stop at timestamp-group boundaries (see PullSourceAdapter.fetch).
        events = list(adapter.fetch(since, self._limit))
        counts = {o.value: 0 for o in IngestOutcome}
        correlation = f"poll:{adapter.source_id}:{int(time.time())}"
        for event in events:
            result = self._ingest.execute(adapter.source_id, event=event, correlation_id=correlation)
            counts[result.outcome.value] += 1
        cursor = next_cursor(events, since)
        if cursor != since:
            with self._db.session() as s:
                self._checkpoints.save(s, adapter.source_id, cursor.isoformat() if cursor else None)
        if events:
            logger.info("source polled", extra={"source": adapter.source_id, **counts})
        return counts

    def run_once(self, *, force: bool = False) -> int:
        """Poll every due source once; returns the number of events read."""
        self.last_loop_at = time.monotonic()
        total = 0
        for adapter in self.adapters():
            now = self._clock()
            last = self._last_poll.get(adapter.source_id)
            if not force and last is not None and now - last < adapter.poll_interval_seconds:
                continue
            self._last_poll[adapter.source_id] = now
            try:
                total += sum(self.poll_source(adapter).values())
                self.db_ok = True
            except Exception:
                logger.exception("source poll failed", extra={"source": adapter.source_id})
        return total

    def _try_lead(self, conn: Connection) -> bool:
        lock_id = conn.execute(select(func.hashtextextended(LEADER_LOCK_NAME, 0))).scalar_one()
        acquired = bool(conn.execute(select(func.pg_try_advisory_lock(lock_id))).scalar_one())
        conn.commit()
        return acquired

    def run_forever(self, idle_seconds: float = 5.0) -> None:
        logger.info("poller started", extra={"sources": ",".join(a.source_id for a in self.adapters())})
        while not self._stop.is_set():
            try:
                with self._db.engine.connect() as conn:
                    self.db_ok = True
                    if not self._try_lead(conn):
                        self.last_loop_at = time.monotonic()
                        self._stop.wait(idle_seconds)
                        continue
                    self.is_leader = True
                    logger.info("poller is leader")
                    while not self._stop.is_set():
                        conn.execute(select(1))  # the lock lives as long as this connection
                        conn.commit()
                        self.run_once()
                        self._stop.wait(idle_seconds)
            except Exception:
                self.db_ok = False
                logger.exception("poller iteration failed")
                self._stop.wait(idle_seconds)
            finally:
                # Closing the connection releases the advisory lock.
                self.is_leader = False
        logger.info("poller stopped")

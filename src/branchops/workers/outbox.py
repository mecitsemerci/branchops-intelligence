"""Outbox drain loops run inside the worker process (one thread per outbox)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from branchops.application.interfaces.delivery import PermanentDeliveryError, RetryLaterError
from branchops.config.settings import OutboxSettings
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.persistence.outbox_repository import OutboxItem, OutboxRepository
from branchops.workers.processor import backoff_seconds

logger = logging.getLogger(__name__)

__all__ = ["OutboxDrainer", "OutboxHandler", "PermanentDeliveryError", "RetryLaterError"]


OutboxHandler = Callable[[OutboxItem], dict[str, Any] | None]


class OutboxDrainer:
    """Claims due outbox rows and hands them to ``handler``; its return value is stored as the result."""

    def __init__(
        self,
        name: str,
        db: Database,
        repo: OutboxRepository,
        handler: OutboxHandler,
        settings: OutboxSettings,
        owner: str,
    ) -> None:
        self.name = name
        self._db = db
        self._repo = repo
        self._handler = handler
        self._settings = settings
        self._owner = owner
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_loop_at = time.monotonic()

    def drain_once(self) -> int:
        """Process one batch; returns the number of claimed rows."""
        self.last_loop_at = time.monotonic()
        with self._db.session() as s:
            self._repo.reap_expired_leases(s)
            items = self._repo.claim(s, self._owner, self._settings.batch_size, self._settings.lease_seconds)
        for item in items:
            self._deliver(item)
        return len(items)

    def _deliver(self, item: OutboxItem) -> None:
        try:
            result = self._handler(item)
        except RetryLaterError as exc:
            with self._db.session() as s:
                self._repo.defer(s, item, exc.delay_seconds, str(exc))
            return
        except PermanentDeliveryError as exc:
            with self._db.session() as s:
                self._repo.fail(s, item, str(exc), retry_in=None)
            logger.error(
                "outbox item dead-lettered", extra={"outbox": self.name, "kind": item.kind, "error": str(exc)}
            )
            return
        except Exception as exc:
            delay = backoff_seconds(
                item.attempts, self._settings.backoff_base_seconds, self._settings.backoff_max_seconds
            )
            with self._db.session() as s:
                status = self._repo.fail(s, item, f"{type(exc).__name__}: {exc}", retry_in=delay)
            logger.warning(
                "outbox delivery failed",
                extra={"outbox": self.name, "kind": item.kind, "status": status, "attempt": item.attempts},
                exc_info=True,
            )
            return
        with self._db.session() as s:
            self._repo.complete(s, item.id, item.lease_owner, result)

    def run_forever(self) -> None:
        logger.info("outbox drainer started", extra={"outbox": self.name})
        while not self._stop.is_set():
            try:
                claimed = self.drain_once()
            except Exception:
                logger.exception("outbox drain iteration failed", extra={"outbox": self.name})
                claimed = 0
            if claimed == 0:
                self._stop.wait(self._settings.poll_interval_seconds)

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.run_forever, name=f"outbox-{self.name}", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

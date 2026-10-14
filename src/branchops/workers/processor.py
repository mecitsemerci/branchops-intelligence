"""Executes one claimed inbox item: lease heartbeat, outcome bookkeeping, retry/dead-letter decisions."""

from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable

from branchops.agents.base import StructuredOutputError
from branchops.application.handlers.process_event import (
    EventProcessor,
    IssueBusyError,
    NonRetryableEventError,
    ProcessOutcome,
)
from branchops.application.interfaces.model_provider import ModelUnavailableError
from branchops.config.settings import WorkerSettings
from branchops.domain.enums import InboxStatus
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.observability.logging import correlation_id_var
from branchops.infrastructure.observability.telemetry import get_instruments, tracer
from branchops.infrastructure.persistence.inbox_repository import InboxItem, InboxRepository

logger = logging.getLogger(__name__)


def backoff_seconds(
    attempt: int, base: float, maximum: float, rand: Callable[[], float] = random.random
) -> float:
    """Exponential backoff with full jitter in [50%, 100%] of the exponential value."""
    exp = min(maximum, base * (2 ** max(0, attempt - 1)))
    return float(round(exp * (0.5 + 0.5 * rand()), 3))


class _LeaseKeeper:
    def __init__(self, db: Database, inbox: InboxRepository, item: InboxItem, owner: str, lease: int) -> None:
        self._db, self._inbox, self._item, self._owner, self._lease = db, inbox, item, owner, lease
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"lease-{item.id}", daemon=True)
        self.lost = False

    def __enter__(self) -> _LeaseKeeper:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        interval = max(5.0, self._lease / 3)
        while not self._stop.wait(interval):
            try:
                with self._db.session() as s:
                    if not self._inbox.renew_lease(s, self._item.id, self._owner, self._lease):
                        self.lost = True
                        logger.error("lease lost", extra={"event_id": str(self._item.id)})
                        return
            except Exception:
                logger.warning("lease renewal failed", exc_info=True)


class InboxItemRunner:
    def __init__(self, db: Database, processor: EventProcessor, settings: WorkerSettings, owner: str) -> None:
        self._db = db
        self._processor = processor
        self._settings = settings
        self._owner = owner
        self._inbox = InboxRepository()

    def run(self, item: InboxItem) -> InboxStatus:
        token = correlation_id_var.set(item.correlation_id)
        instruments = get_instruments()
        try:
            with tracer().start_as_current_span("worker.process_event") as span:
                span.set_attribute("branchops.correlation_id", item.correlation_id)
                span.set_attribute("branchops.event_type", item.event_type)
                span.set_attribute("branchops.attempt", item.attempts)
                with _LeaseKeeper(
                    self._db, self._inbox, item, item.lease_owner, self._settings.lease_seconds
                ) as lease:
                    try:
                        outcome = self._processor.process(item)
                    except IssueBusyError:
                        with self._db.session() as s:
                            self._inbox.release(s, item.id, item.lease_owner, delay_seconds=5)
                        instruments.worker_events.add(1, {"outcome": "busy"})
                        return InboxStatus.PENDING
                    except NonRetryableEventError as exc:
                        return self._fail(item, str(exc), retry=False, kind="invalid")
                    except ModelUnavailableError as exc:
                        return self._fail(
                            item, f"model unavailable: {exc}", retry=True, kind="model_unavailable"
                        )
                    except StructuredOutputError as exc:
                        return self._fail(item, str(exc), retry=True, kind="structured_output")
                    except Exception as exc:
                        logger.exception("unexpected processing error", extra={"event_id": str(item.id)})
                        return self._fail(item, f"{type(exc).__name__}: {exc}", retry=True, kind="unexpected")
                if lease.lost:
                    # Another worker owns the event now; recording our outcome would clobber its run.
                    logger.error(
                        "lease lost during processing; outcome discarded", extra={"event_id": str(item.id)}
                    )
                    instruments.worker_events.add(1, {"outcome": "lease_lost"})
                    return InboxStatus.PROCESSING
                return self._complete(item, outcome)
        finally:
            correlation_id_var.reset(token)

    def _complete(self, item: InboxItem, outcome: ProcessOutcome) -> InboxStatus:
        with self._db.session() as s:
            self._inbox.complete(
                s,
                item.id,
                item.lease_owner,
                status=outcome.status,
                outcome=outcome.outcome,
                redacted_payload=outcome.redacted_payload,
            )
        get_instruments().worker_events.add(1, {"outcome": outcome.status.value})
        logger.info("event processed", extra={"event_id": str(item.id), "outcome": outcome.outcome})
        return outcome.status

    def _fail(self, item: InboxItem, error: str, *, retry: bool, kind: str) -> InboxStatus:
        delay = None
        if retry and item.attempts < item.max_attempts:
            delay = backoff_seconds(
                item.attempts, self._settings.backoff_base_seconds, self._settings.backoff_max_seconds
            )
        with self._db.session() as s:
            status = self._inbox.fail(s, item.id, item.lease_owner, error=error, retry_in_seconds=delay)
        get_instruments().worker_failures.add(
            1, {"kind": kind, "final": str(status is InboxStatus.DEAD_LETTER)}
        )
        logger.warning(
            "event processing failed",
            extra={"event_id": str(item.id), "kind": kind, "status": status.value, "retry_in": delay or 0},
        )
        return status

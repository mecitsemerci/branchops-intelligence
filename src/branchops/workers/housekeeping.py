"""Periodic maintenance in the worker process (attachment retention)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class PeriodicTask:
    """Runs ``fn`` every ``interval_seconds`` in a daemon thread (first run after one interval)."""

    def __init__(self, name: str, fn: Callable[[], Any], interval_seconds: float) -> None:
        self.name = name
        self._fn = fn
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"task-{name}", daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                result = self._fn()
                logger.info("periodic task done", extra={"task": self.name, "result": str(result)})
            except Exception:
                logger.exception("periodic task failed", extra={"task": self.name})

    def start(self) -> PeriodicTask:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

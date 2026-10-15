"""Worker main loop: reap expired leases, claim batches with SKIP LOCKED, process in a bounded pool."""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

from branchops.config.settings import WorkerSettings
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.persistence.inbox_repository import InboxRepository
from branchops.workers.processor import InboxItemRunner

logger = logging.getLogger(__name__)


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class Dispatcher:
    def __init__(self, db: Database, runner: InboxItemRunner, settings: WorkerSettings, owner: str) -> None:
        self._db = db
        self._runner = runner
        self._settings = settings
        self._owner = owner
        self._inbox = InboxRepository()
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=settings.concurrency, thread_name_prefix="inbox")
        self._inflight: set[Future[Any]] = set()
        self._lock = threading.Lock()
        self.last_loop_at = time.monotonic()
        self.db_ok = False

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> int:
        """One poll iteration; returns the number of items dispatched."""
        self.last_loop_at = time.monotonic()
        with self._db.session() as s:
            reaped = self._inbox.reap_expired_leases(s)
        if reaped:
            logger.warning("recovered expired leases", extra={"count": reaped})
        with self._lock:
            self._inflight = {f for f in self._inflight if not f.done()}
            capacity = self._settings.concurrency - len(self._inflight)
        if capacity <= 0:
            return 0
        with self._db.session() as s:
            items = self._inbox.claim(
                s, self._owner, min(capacity, self._settings.batch_size), self._settings.lease_seconds
            )
        self.db_ok = True
        # The claim query returns at most one event per issue, so items can run in parallel safely.
        for item in items:
            future = self._pool.submit(self._runner.run, item)
            with self._lock:
                self._inflight.add(future)
        return len(items)

    def run_forever(self) -> None:
        logger.info("worker started", extra={"owner": self._owner, "concurrency": self._settings.concurrency})
        while not self._stop.is_set():
            try:
                dispatched = self.run_once()
            except Exception:
                self.db_ok = False
                logger.exception("dispatcher iteration failed")
                dispatched = 0
            if dispatched == 0:
                self._stop.wait(self._settings.poll_interval_seconds)
        logger.info("worker stopping; waiting for in-flight events")
        self._pool.shutdown(wait=True, cancel_futures=False)

    def install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: Any) -> None:
            logger.info("signal received", extra={"signal": signum})
            self.stop()

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)


class HealthState(Protocol):
    """Loop heartbeat and DB status of a long-running process (worker dispatcher, source poller)."""

    last_loop_at: float
    db_ok: bool


def start_health_server(
    dispatcher: HealthState, port: int, stale_after_seconds: float
) -> ThreadingHTTPServer:
    """Liveness = loop is iterating; readiness = last DB interaction succeeded; /metrics for Prometheus."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/healthz":
                alive = time.monotonic() - dispatcher.last_loop_at < stale_after_seconds
                self._reply(200 if alive else 503, b"ok" if alive else b"stale")
            elif self.path == "/readyz":
                self._reply(200 if dispatcher.db_ok else 503, b"ready" if dispatcher.db_ok else b"not ready")
            elif self.path == "/metrics":
                from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

                self._reply(200, generate_latest(), CONTENT_TYPE_LATEST)
            else:
                self._reply(404, b"not found")

        def _reply(self, code: int, body: bytes, ctype: str = "text/plain") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 - probe port inside the pod
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    return server

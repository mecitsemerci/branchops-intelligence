"""Sends generated incidents to the API on a schedule (development / demo only)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import random
import signal
import threading
import time
from typing import Any

import httpx

from branchops.simulator.generator import IncidentGenerator

logger = logging.getLogger(__name__)


class WebhookSimulator:
    def __init__(
        self,
        api_url: str,
        secret: str,
        *,
        interval_seconds: float = 300.0,
        initial_burst: int = 2,
        follow_up_probability: float = 0.35,
        seed: int | None = None,
        jitter: float = 0.1,
    ) -> None:
        self._url = api_url.rstrip("/") + "/api/webhooks/jira"
        self._health = api_url.rstrip("/") + "/health/ready"
        self._secret = secret.encode()
        self._interval = interval_seconds
        self._burst = initial_burst
        self._follow_up = follow_up_probability
        self._jitter = jitter
        self._generator = IncidentGenerator(seed=seed)
        self._rng = random.Random(seed)
        self._stop = threading.Event()
        self._client = httpx.Client(timeout=15.0)

    def stop(self, *_: Any) -> None:
        self._stop.set()

    def send(self, payload: dict[str, Any]) -> int:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        signature = "sha256=" + hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        response = self._client.post(
            self._url,
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature": signature},
        )
        issue = payload.get("issue", {}).get("key")
        logger.info(
            "simulated webhook sent",
            extra={"event": payload.get("webhookEvent"), "issue_key": issue, "status": response.status_code},
        )
        return response.status_code

    def wait_for_api(self, timeout: float = 300.0) -> None:
        deadline = time.monotonic() + timeout
        while not self._stop.is_set():
            try:
                if self._client.get(self._health).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"API not ready at {self._health}")
            self._stop.wait(2)

    def tick(self) -> None:
        issue = self._generator.new_issue()
        self.send(issue.payload)
        if self._rng.random() < self._follow_up and (follow := self._generator.follow_up()):
            self._stop.wait(1)
            self.send(follow)

    def run_once(self) -> None:
        self.wait_for_api(timeout=60)
        for _ in range(max(1, self._burst)):
            self.tick()
        self._client.close()

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.wait_for_api()
        logger.info("simulator started", extra={"interval_s": self._interval, "burst": self._burst})
        for _ in range(self._burst):
            self.tick()
        while not self._stop.is_set():
            delay = self._interval * (1 + self._rng.uniform(-self._jitter, self._jitter))
            logger.info("next simulated incident", extra={"in_seconds": round(delay)})
            if self._stop.wait(delay):
                break
            try:
                self.tick()
            except httpx.HTTPError:
                logger.warning("simulated webhook failed", exc_info=True)
        self._client.close()

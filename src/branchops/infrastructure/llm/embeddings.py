"""OpenAI-compatible ``/embeddings`` client for an approved internal embedding model.

Same network rules as the chat model: the URL must be internal (``network`` allow-list), TLS is verified
against the internal CA, nothing is downloaded. Errors never contain the API key.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from branchops.application.interfaces.embeddings import EmbeddingUnavailableError
from branchops.config.settings import EmbeddingSettings, NetworkSettings
from branchops.infrastructure.llm.openai_compatible import build_ssl_context
from branchops.infrastructure.observability.telemetry import get_instruments
from branchops.infrastructure.security.network_guard import assert_internal_url


class OpenAICompatibleEmbeddingProvider:
    def __init__(
        self,
        settings: EmbeddingSettings,
        network: NetworkSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not (settings.base_url and settings.model):
            raise ValueError("similarity.embeddings: base_url and model are required")
        assert_internal_url(settings.base_url, network, purpose="similarity.embeddings.base_url")
        self._settings = settings
        key = settings.api_key()
        headers = {"Authorization": f"Bearer {key.get_secret_value()}"} if key else {}
        self._client = httpx.Client(
            base_url=settings.base_url.rstrip("/"),
            headers=headers,
            timeout=settings.timeout_seconds,
            verify=build_ssl_context(settings.ca_bundle) if transport is None else True,
            transport=transport,
        )

    @property
    def model_id(self) -> str:
        return str(self._settings.model)

    def embed(self, texts: list[str]) -> list[list[float]]:
        inputs = [t[: self._settings.max_input_chars] for t in texts]
        started = time.perf_counter()
        try:
            response = self._client.post("/embeddings", json={"model": self.model_id, "input": inputs})
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailableError(f"embedding endpoint unreachable: {type(exc).__name__}") from exc
        get_instruments().model_latency_ms.record(
            int((time.perf_counter() - started) * 1000), {"agent": "embeddings", "model": self.model_id}
        )
        if response.status_code != 200:
            raise EmbeddingUnavailableError(f"embedding endpoint error {response.status_code}")
        try:
            data: list[dict[str, Any]] = sorted(response.json()["data"], key=lambda d: int(d.get("index", 0)))
            vectors = [[float(x) for x in item["embedding"]] for item in data]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingUnavailableError("embedding endpoint returned an unexpected body") from exc
        if len(vectors) != len(inputs):
            raise EmbeddingUnavailableError("embedding endpoint returned a different number of vectors")
        return vectors

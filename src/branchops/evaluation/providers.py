"""Model providers for evaluation: metering, recording and replay.

* :class:`MeteringProvider` wraps any provider and keeps per-call statistics (latency, tokens, outcome).
* :class:`RecordingProvider` additionally stores every answer, keyed by ``correlation_id`` and schema, so a
  run against a real internal model can be reproduced offline.
* :class:`ReplayProvider` answers from such a recording. It never calls a network endpoint; a request that is
  not in the recording is a hard error (the run is not comparable otherwise).

Recordings contain model *outputs* for synthetic or masked dataset records only, never prompts.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from branchops.application.interfaces.model_provider import (
    CompletionRequest,
    CompletionResult,
    ModelError,
    ModelHealth,
    ModelProvider,
    ModelRequestError,
    ModelUnavailableError,
    OutputMode,
    TokenUsage,
    UnsupportedOutputModeError,
)
from branchops.config.settings import ModelCapabilities

RECORDING_FORMAT = "branchops-eval-recording/1"


class RecordingMissError(ModelRequestError):
    """The replayed run asked for an answer that the recording does not contain."""


def _key(request: CompletionRequest) -> str:
    return f"{request.correlation_id}|{request.schema_name or request.agent}"


@dataclass
class CallRecord:
    correlation_id: str
    agent: str
    schema: str | None
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str | None = None
    error: str | None = None


@dataclass
class MeteringProvider:
    """Pass-through provider that records call statistics (thread-safe)."""

    inner: ModelProvider
    calls: list[CallRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def model_id(self) -> str:
        return self.inner.model_id

    @property
    def capabilities(self) -> ModelCapabilities:
        return self.inner.capabilities

    def health(self) -> ModelHealth:
        return self.inner.health()

    def complete(self, request: CompletionRequest, *, timeout: float | None = None) -> CompletionResult:
        record = CallRecord(request.correlation_id, request.agent, request.schema_name)
        try:
            result = self.inner.complete(request, timeout=timeout)
        except ModelError as exc:
            record.error = type(exc).__name__
            with self._lock:
                self.calls.append(record)
            raise
        record.latency_ms = result.latency_ms
        record.prompt_tokens = result.usage.prompt_tokens
        record.completion_tokens = result.usage.completion_tokens
        record.finish_reason = result.finish_reason
        with self._lock:
            self.calls.append(record)
        return result

    def calls_for(self, correlation_id: str) -> list[CallRecord]:
        return [c for c in self.calls if c.correlation_id == correlation_id]


class RecordingProvider:
    """Delegates to ``inner`` and keeps every answer (or error class) for :meth:`save`."""

    def __init__(self, inner: ModelProvider) -> None:
        self.inner = inner
        self._entries: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._lock = threading.Lock()

    @property
    def model_id(self) -> str:
        return self.inner.model_id

    @property
    def capabilities(self) -> ModelCapabilities:
        return self.inner.capabilities

    def health(self) -> ModelHealth:
        return self.inner.health()

    def complete(self, request: CompletionRequest, *, timeout: float | None = None) -> CompletionResult:
        try:
            result = self.inner.complete(request, timeout=timeout)
        except ModelError as exc:
            self._append(request, {"error": type(exc).__name__, "message": str(exc)[:300]})
            raise
        self._append(
            request,
            {
                "content": result.content,
                "model_id": result.model_id,
                "output_mode": result.output_mode.value,
                "prompt_tokens": result.usage.prompt_tokens,
                "completion_tokens": result.usage.completion_tokens,
                "latency_ms": result.latency_ms,
                "finish_reason": result.finish_reason,
            },
        )
        return result

    def _append(self, request: CompletionRequest, entry: dict[str, Any]) -> None:
        with self._lock:
            self._entries[_key(request)].append(entry)

    def to_dict(self) -> dict[str, Any]:
        caps = self.capabilities
        return {
            "format": RECORDING_FORMAT,
            "model_id": self.model_id,
            "capabilities": caps.model_dump(mode="json"),
            "calls": {k: list(v) for k, v in sorted(self._entries.items())},
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")


_ERRORS: dict[str, type[ModelError]] = {
    "ModelUnavailableError": ModelUnavailableError,
    "DeadlineExceededError": ModelUnavailableError,
    "ModelAccessError": ModelUnavailableError,
    "UnsupportedOutputModeError": UnsupportedOutputModeError,
}


class ReplayProvider:
    """Answers from a recording, in request order per ``correlation_id`` and schema."""

    def __init__(self, data: dict[str, Any]) -> None:
        if data.get("format") != RECORDING_FORMAT:
            raise ValueError(f"not a BranchOps evaluation recording (format {data.get('format')!r})")
        self._model_id = str(data["model_id"])
        self._caps = ModelCapabilities.model_validate(data.get("capabilities") or {})
        self._calls: dict[str, list[dict[str, Any]]] = {k: list(v) for k, v in data["calls"].items()}
        self._cursor: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    @classmethod
    def load(cls, path: str | Path) -> ReplayProvider:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._caps

    def health(self) -> ModelHealth:
        return ModelHealth(True, self._model_id, "replay")

    def complete(self, request: CompletionRequest, *, timeout: float | None = None) -> CompletionResult:
        key = _key(request)
        with self._lock:
            index = self._cursor[key]
            entries = self._calls.get(key, [])
            if index >= len(entries):
                raise RecordingMissError(f"no recorded answer #{index + 1} for {key}")
            self._cursor[key] = index + 1
            entry = entries[index]
        if "error" in entry:
            raise _ERRORS.get(entry["error"], ModelRequestError)(entry.get("message", entry["error"]))
        return CompletionResult(
            content=entry["content"],
            model_id=entry.get("model_id", self._model_id),
            output_mode=OutputMode(entry.get("output_mode", request.output_mode.value)),
            usage=TokenUsage(int(entry.get("prompt_tokens", 0)), int(entry.get("completion_tokens", 0))),
            latency_ms=int(entry.get("latency_ms", 0)),
            finish_reason=entry.get("finish_reason"),
        )

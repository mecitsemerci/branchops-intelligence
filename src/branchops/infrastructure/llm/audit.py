"""Content-free audit record of every model call (``branchops.model_audit`` logger, JSON lines).

One record per request: correlation id, agent, schema, model, output mode, SHA-256 of the prompt, prompt size,
token usage, latency, finish reason and outcome. Prompts and outputs themselves are never logged, so the
audit trail can be retained longer than the incident data it refers to; the prompt hash lets an auditor prove
which (stored, masked) input produced an analysis.
"""

from __future__ import annotations

import hashlib
import logging
import time

from branchops.application.interfaces.model_provider import (
    CompletionRequest,
    CompletionResult,
    ModelHealth,
    ModelProvider,
)
from branchops.config.settings import ModelCapabilities

audit_logger = logging.getLogger("branchops.model_audit")


def prompt_sha256(request: CompletionRequest) -> str:
    digest = hashlib.sha256()
    for message in request.messages:
        digest.update(message.role.encode())
        digest.update(b"\0")
        digest.update(message.content.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


class AuditingModelProvider:
    def __init__(self, inner: ModelProvider) -> None:
        self.inner = inner

    @property
    def model_id(self) -> str:
        return self.inner.model_id

    @property
    def capabilities(self) -> ModelCapabilities:
        return self.inner.capabilities

    def health(self) -> ModelHealth:
        return self.inner.health()

    def complete(self, request: CompletionRequest, *, timeout: float | None = None) -> CompletionResult:
        record: dict[str, object] = {
            "event": "model_call",
            "correlation_id": request.correlation_id,
            "agent": request.agent,
            "schema": request.schema_name,
            "model": self.inner.model_id,
            "output_mode": request.output_mode.value,
            "prompt_sha256": prompt_sha256(request),
            "prompt_chars": sum(len(m.content) for m in request.messages),
        }
        started = time.perf_counter()
        try:
            result = self.inner.complete(request, timeout=timeout)
        except Exception as exc:
            record.update(outcome=type(exc).__name__, latency_ms=int((time.perf_counter() - started) * 1000))
            audit_logger.info("model call", extra=record)
            raise
        record.update(
            outcome="ok",
            served_model=result.model_id,
            latency_ms=result.latency_ms,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            finish_reason=result.finish_reason,
        )
        audit_logger.info("model call", extra=record)
        return result

"""Structured-output calling with capability-aware fallback, bounded repair and usage accounting."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from branchops.application.interfaces.model_provider import (
    ChatMessage,
    CompletionRequest,
    ModelProvider,
    ModelUnavailableError,
    OutputMode,
    TokenUsage,
    UnsupportedOutputModeError,
)
from branchops.infrastructure.observability.telemetry import get_instruments, tracer

logger = logging.getLogger(__name__)

_MODE_ORDER = [OutputMode.JSON_SCHEMA, OutputMode.JSON_OBJECT, OutputMode.PROMPT_ONLY]


class StructuredOutputError(Exception):
    """The model did not produce schema-valid output within the repair budget."""

    def __init__(self, message: str, usage: TokenUsage, attempts: int) -> None:
        super().__init__(message)
        self.usage = usage
        self.attempts = attempts


class DeadlineExceededError(ModelUnavailableError):
    pass


@dataclass
class CallStats:
    attempts: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    output_mode: OutputMode | None = None
    model_id: str | None = None
    latency_ms: int = 0

    def add(self, usage: TokenUsage) -> None:
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens


@dataclass
class StructuredResult[T: BaseModel]:
    value: T
    stats: CallStats = field(default_factory=CallStats)


def extract_json(content: str) -> Any:
    """Parse the first JSON object in ``content`` (tolerates code fences and leading prose)."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found in model output")
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(text[start:])
    return obj


class ModeSelector:
    """Remembers per-process downgrades when the gateway rejects an output mode."""

    def __init__(self, provider: ModelProvider) -> None:
        caps = provider.capabilities
        if caps.structured_outputs:
            self._mode = OutputMode.JSON_SCHEMA
        elif caps.json_mode:
            self._mode = OutputMode.JSON_OBJECT
        else:
            self._mode = OutputMode.PROMPT_ONLY

    @property
    def mode(self) -> OutputMode:
        return self._mode

    def downgrade(self, failed: OutputMode) -> OutputMode | None:
        idx = _MODE_ORDER.index(failed)
        if idx + 1 >= len(_MODE_ORDER):
            return None
        self._mode = _MODE_ORDER[idx + 1]
        logger.warning(
            "output mode unsupported by gateway; falling back", extra={"from": failed, "to": self._mode}
        )
        return self._mode


class StructuredCaller:
    def __init__(
        self,
        provider: ModelProvider,
        *,
        max_repair_attempts: int = 1,
        max_output_tokens: int | None = None,
    ) -> None:
        self.provider = provider
        self._modes = ModeSelector(provider)
        self._max_repairs = max_repair_attempts
        self._max_output_tokens = max_output_tokens

    def call[T: BaseModel](
        self,
        *,
        agent: str,
        system_prompt: str,
        user_prompt: str,
        schema: type[T],
        correlation_id: str,
        validation_context: dict[str, Any] | None = None,
        deadline: float | None = None,
    ) -> StructuredResult[T]:
        stats = CallStats(model_id=self.provider.model_id)
        json_schema = schema.model_json_schema()
        system = (
            f"{system_prompt}\n\nRespond with a single JSON object that validates against this JSON Schema. "
            f"Do not wrap it in markdown. Do not add commentary.\nJSON Schema:\n"
            f"{json.dumps(json_schema, ensure_ascii=False)}"
        )
        messages = [ChatMessage("system", system), ChatMessage("user", user_prompt)]
        instruments = get_instruments()
        last_error = "no attempt made"
        repairs_left = self._max_repairs

        with tracer().start_as_current_span(f"llm.{agent}") as span:
            span.set_attribute("branchops.correlation_id", correlation_id)
            span.set_attribute("llm.model", self.provider.model_id)
            while True:
                mode = self._modes.mode
                timeout = None
                if deadline is not None:
                    timeout = deadline - time.monotonic()
                    if timeout <= 1:
                        raise DeadlineExceededError(f"{agent}: node time budget exhausted")
                request = CompletionRequest(
                    messages=messages,
                    agent=agent,
                    correlation_id=correlation_id,
                    output_mode=mode,
                    json_schema=json_schema if mode is OutputMode.JSON_SCHEMA else None,
                    schema_name=schema.__name__,
                    max_output_tokens=self._max_output_tokens,
                )
                stats.attempts += 1
                try:
                    result = self.provider.complete(request, timeout=timeout)
                except UnsupportedOutputModeError:
                    if self._modes.downgrade(mode) is None:
                        raise
                    continue
                except ModelUnavailableError:
                    instruments.model_errors.add(1, {"agent": agent, "kind": "unavailable"})
                    raise
                stats.add(result.usage)
                stats.output_mode = result.output_mode
                stats.model_id = result.model_id
                stats.latency_ms += result.latency_ms
                attrs = {"agent": agent, "model": result.model_id}
                instruments.model_latency_ms.record(result.latency_ms, attrs)
                instruments.model_tokens.add(result.usage.prompt_tokens, {**attrs, "type": "prompt"})
                instruments.model_tokens.add(result.usage.completion_tokens, {**attrs, "type": "completion"})
                try:
                    data = extract_json(result.content)
                    value = schema.model_validate(data, context=validation_context or {})
                    span.set_attribute("llm.attempts", stats.attempts)
                    span.set_attribute("llm.output_mode", str(stats.output_mode))
                    return StructuredResult(value=value, stats=stats)
                except (ValueError, ValidationError) as exc:
                    last_error = _summarize_error(exc)
                    instruments.structured_output_failures.add(1, {"agent": agent, "mode": mode.value})
                    logger.warning(
                        "structured output validation failed",
                        extra={"agent": agent, "attempt": stats.attempts, "error": last_error},
                    )
                    if repairs_left <= 0:
                        break
                    repairs_left -= 1
                    messages = [
                        *messages,
                        ChatMessage("assistant", result.content[:4000]),
                        ChatMessage(
                            "user",
                            "Your previous answer was not valid for the schema. Problems: "
                            f"{last_error}. Return only the corrected JSON object.",
                        ),
                    ]
        raise StructuredOutputError(
            f"{agent}: invalid structured output after {stats.attempts} attempt(s): {last_error}",
            TokenUsage(stats.prompt_tokens, stats.completion_tokens),
            stats.attempts,
        )


def _summarize_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:8]]
        return "; ".join(parts)[:800]
    return str(exc)[:800]

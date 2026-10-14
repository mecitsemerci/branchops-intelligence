"""Port for chat-completion model providers (dependency inversion for agents)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from branchops.config.settings import ModelCapabilities


class OutputMode(StrEnum):
    JSON_SCHEMA = "json_schema"
    JSON_OBJECT = "json_object"
    PROMPT_ONLY = "prompt_only"
    TEXT = "text"


@dataclass(frozen=True)
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True)
class CompletionRequest:
    messages: list[ChatMessage]
    agent: str
    correlation_id: str
    output_mode: OutputMode = OutputMode.TEXT
    json_schema: dict[str, Any] | None = None
    schema_name: str | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class CompletionResult:
    content: str
    model_id: str
    output_mode: OutputMode
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0
    finish_reason: str | None = None


@dataclass(frozen=True)
class ModelHealth:
    healthy: bool
    model_id: str
    detail: str
    latency_ms: int = 0


class ModelError(Exception):
    """Base class for model-provider failures."""


class ModelUnavailableError(ModelError):
    """Transient: endpoint unreachable, timeout, 5xx, rate limit. Safe to retry later."""


class ModelRequestError(ModelError):
    """Non-transient: request rejected (4xx, unsupported parameter, context too long)."""


class UnsupportedOutputModeError(ModelRequestError):
    """The endpoint rejected the requested response_format; caller should fall back."""


class ModelProvider(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def capabilities(self) -> ModelCapabilities: ...

    def complete(self, request: CompletionRequest, *, timeout: float | None = None) -> CompletionResult: ...

    def health(self) -> ModelHealth: ...

"""Port for an internal embedding model (optional semantic retrieval)."""

from __future__ import annotations

from typing import Protocol


class EmbeddingUnavailableError(Exception):
    """The embedding endpoint could not produce vectors; callers fall back to lexical retrieval."""


class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...

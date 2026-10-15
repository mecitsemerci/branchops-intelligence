"""Port: where attachment bytes live (PostgreSQL bytea by default, or an internal S3-compatible store)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class StoredBlob:
    storage: str
    ref: str
    # Bytes to keep in the ``attachments.data`` column (PostgreSQL store); ``None`` for external stores.
    inline_data: bytes | None = None


class AttachmentStore(Protocol):
    name: str

    def put(self, key: str, data: bytes, content_type: str) -> StoredBlob: ...

    def get(self, ref: str, inline_data: bytes | None) -> bytes: ...

    def delete(self, ref: str) -> None: ...

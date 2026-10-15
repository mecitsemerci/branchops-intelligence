"""Default store: bytes in the ``attachments.data`` bytea column (size-capped by settings)."""

from __future__ import annotations

from branchops.application.interfaces.attachment_store import StoredBlob


class PostgresLargeObjectStore:
    name = "postgres"

    def __init__(self, max_bytes: int) -> None:
        self._max = max_bytes

    def put(self, key: str, data: bytes, content_type: str) -> StoredBlob:
        if len(data) > self._max:
            raise ValueError(f"attachment exceeds {self._max} bytes")
        return StoredBlob(storage=self.name, ref=f"pg:{key}", inline_data=data)

    def get(self, ref: str, inline_data: bytes | None) -> bytes:
        if inline_data is None:
            raise LookupError(f"attachment content missing for {ref}")
        return inline_data

    def delete(self, ref: str) -> None:
        return None  # the row (and its bytea) is deleted by the caller

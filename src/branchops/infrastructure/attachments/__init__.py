"""Attachment stores and the factory selecting one from settings."""

from __future__ import annotations

from branchops.application.interfaces.attachment_store import AttachmentStore
from branchops.config.settings import Settings


def build_attachment_store(settings: Settings) -> AttachmentStore:
    if settings.attachments.store == "s3":
        from branchops.infrastructure.attachments.s3 import S3CompatibleStore

        return S3CompatibleStore(settings.attachments.s3, settings.network)
    from branchops.infrastructure.attachments.postgres import PostgresLargeObjectStore

    return PostgresLargeObjectStore(settings.attachments.max_bytes)

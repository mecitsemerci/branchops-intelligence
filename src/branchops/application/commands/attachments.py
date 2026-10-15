"""Attachments of in-app reports: draft tokens, upload (sanitised, size-limited), binding, reading, purge.

Attachments are sensitive: they are stored separately from the incident text, are never sent to the LLM and
are forwarded only to the ticketing sink (when configured) for reported incidents.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import math
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from branchops.application.interfaces.attachment_store import AttachmentStore
from branchops.config.settings import Settings
from branchops.domain.entities.incident import parse_incident_key
from branchops.domain.services.masking import SensitiveDataMasker
from branchops.infrastructure.attachments.processing import (
    AttachmentRejectedError,
    process_image,
    process_text,
    safe_filename,
)
from branchops.infrastructure.database.models import Attachment, IncidentDraftToken
from branchops.infrastructure.ticketing.handler import TICKETING_OUTBOX

AttachmentKind = Literal["screenshot", "log", "other"]

__all__ = [
    "AttachmentKind",
    "AttachmentRejectedError",
    "AttachmentService",
    "DraftTokenError",
    "UploadResult",
]


class DraftTokenError(ValueError):
    """Unknown, expired, already used or foreign draft token."""


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class UploadResult:
    attachment_id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    blurred_regions: int
    incident_key: str | None
    ticketing: str  # "queued" | "disabled" | "pending_report"


def _meta(a: Attachment) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "incident_key": a.incident_key,
        "filename": a.filename,
        "content_type": a.content_type,
        "kind": a.kind,
        "size_bytes": a.size_bytes,
        "sha256": a.sha256,
        "blurred_regions": a.blurred_regions,
        "created_by": a.created_by,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


class AttachmentService:
    def __init__(self, settings: Settings, store: AttachmentStore) -> None:
        self._settings = settings
        self._cfg = settings.attachments
        self._store = store
        self._masker = SensitiveDataMasker(settings.security.masking_extra_patterns)

    # ------------------------------------------------------------------ draft tokens
    def issue_draft_token(self, s: Session, actor: str, now: datetime | None = None) -> tuple[str, datetime]:
        token = "dft_" + secrets.token_urlsafe(24)
        expires = (now or datetime.now(UTC)) + timedelta(minutes=self._cfg.draft_token_ttl_minutes)
        s.add(IncidentDraftToken(token_sha256=token_hash(token), created_by=actor[:200], expires_at=expires))
        s.flush()
        return token, expires

    def _draft(self, s: Session, token: str, actor: str, *, lock: bool = False) -> IncidentDraftToken:
        q = select(IncidentDraftToken).where(IncidentDraftToken.token_sha256 == token_hash(token))
        draft = s.execute(q.with_for_update() if lock else q).scalar_one_or_none()
        if draft is None or draft.created_by != actor[:200]:
            raise DraftTokenError("unknown draft token")
        if draft.incident_key is not None:
            raise DraftTokenError("draft token already used")
        if draft.expires_at <= datetime.now(UTC):
            raise DraftTokenError("draft token expired; call check_incident_draft again")
        return draft

    # ------------------------------------------------------------------ upload
    def _decode(self, data_base64: str) -> bytes:
        limit = self._cfg.max_bytes
        if len(data_base64) > math.ceil(limit * 4 / 3) + 4:
            raise AttachmentRejectedError(f"attachment exceeds {limit} bytes")
        try:
            raw = base64.b64decode(data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AttachmentRejectedError("data_base64 is not valid base64") from exc
        if not raw:
            raise AttachmentRejectedError("empty attachment")
        if len(raw) > limit:
            raise AttachmentRejectedError(f"attachment exceeds {limit} bytes")
        return raw

    def _count(
        self, s: Session, *, incident_key: str | None = None, draft_id: uuid.UUID | None = None
    ) -> int:
        cond = Attachment.incident_key == incident_key if incident_key else Attachment.draft_id == draft_id
        return int(s.execute(select(func.count()).select_from(Attachment).where(cond)).scalar_one())

    def upload(
        self,
        s: Session,
        *,
        actor: str,
        filename: str,
        content_type: str,
        data_base64: str,
        kind: AttachmentKind,
        incident_key: str | None = None,
        draft_token: str | None = None,
        blur_regions: list[list[int]] | None = None,
    ) -> UploadResult:
        if (incident_key is None) == (draft_token is None):
            raise AttachmentRejectedError("pass exactly one of incident_key or draft_token")
        content_type = content_type.strip().lower()
        if content_type not in self._cfg.allowed_types:
            raise AttachmentRejectedError(f"content type not allowed; allowed: {self._cfg.allowed_types}")
        raw = self._decode(data_base64)
        draft = self._draft(s, draft_token, actor, lock=True) if draft_token else None
        key = None
        if incident_key is not None:
            source, external = parse_incident_key(incident_key)
            key = f"{source}:{external}"
        existing = self._count(s, incident_key=key, draft_id=draft.id if draft else None)
        if existing >= self._cfg.max_per_incident:
            raise AttachmentRejectedError(f"at most {self._cfg.max_per_incident} attachments per incident")
        if content_type == "text/plain":
            if blur_regions:
                raise AttachmentRejectedError("blur regions apply to images only")
            processed = process_text(raw, self._masker)
        else:
            processed = process_image(raw, content_type, blur_regions, max_pixels=self._cfg.max_image_pixels)
        if len(processed.data) > self._cfg.max_bytes:
            raise AttachmentRejectedError(f"attachment exceeds {self._cfg.max_bytes} bytes after re-encoding")
        attachment_id = uuid.uuid4()
        name = safe_filename(filename, processed.content_type)
        blob = self._store.put(
            f"{attachment_id}{name[name.rfind('.') :]}", processed.data, processed.content_type
        )
        digest = hashlib.sha256(processed.data).hexdigest()
        s.add(
            Attachment(
                id=attachment_id,
                incident_key=key,
                draft_id=draft.id if draft else None,
                filename=name,
                content_type=processed.content_type,
                kind=kind,
                size_bytes=len(processed.data),
                sha256=digest,
                storage=blob.storage,
                storage_ref=blob.ref,
                data=blob.inline_data,
                blurred_regions=processed.blurred_regions,
                created_by=actor[:200],
            )
        )
        s.flush()
        ticketing = "pending_report" if draft else self._forward(s, key, [attachment_id])
        return UploadResult(
            attachment_id=str(attachment_id),
            filename=name,
            content_type=processed.content_type,
            size_bytes=len(processed.data),
            sha256=digest,
            blurred_regions=processed.blurred_regions,
            incident_key=key,
            ticketing=ticketing,
        )

    def _forward(self, s: Session, incident_key: str | None, ids: list[uuid.UUID]) -> str:
        """Queue ticket attachments for reported incidents when a ticketing sink is configured."""
        if (
            incident_key is None
            or self._settings.ticketing.sink == "none"
            or not self._cfg.forward_to_ticketing
            or parse_incident_key(incident_key)[0] != self._settings.reporting.source_id
        ):
            return "disabled"
        for attachment_id in ids:
            TICKETING_OUTBOX.enqueue(
                s,
                idempotency_key=f"ticket:{incident_key}:attachment:{attachment_id}",
                kind="add_attachment",
                incident_key=incident_key,
                payload={"attachment_id": str(attachment_id)},
                max_attempts=self._settings.outbox.max_attempts,
            )
        return "queued"

    def bind_draft(self, s: Session, draft_token: str, actor: str, incident_key: str) -> int:
        """Attach the draft's uploads to the reported incident (same transaction as the report)."""
        draft = self._draft(s, draft_token, actor, lock=True)
        rows = s.execute(select(Attachment).where(Attachment.draft_id == draft.id)).scalars().all()
        already = self._count(s, incident_key=incident_key)
        if already + len(rows) > self._cfg.max_per_incident:
            raise AttachmentRejectedError(f"at most {self._cfg.max_per_incident} attachments per incident")
        for row in rows:
            row.incident_key = incident_key
            row.draft_id = None
        draft.incident_key = incident_key
        draft.bound_at = datetime.now(UTC)
        s.flush()
        self._forward(s, incident_key, [r.id for r in rows])
        return len(rows)

    # ------------------------------------------------------------------ reads
    def list_for_incident(self, s: Session, incident_key: str) -> list[dict[str, Any]]:
        rows = s.execute(
            select(Attachment).where(Attachment.incident_key == incident_key).order_by(Attachment.created_at)
        ).scalars()
        return [_meta(a) for a in rows]

    def load(self, s: Session, attachment_id: str) -> tuple[dict[str, Any], bytes] | None:
        try:
            aid = uuid.UUID(attachment_id)
        except ValueError:
            return None
        row = s.get(Attachment, aid)
        if row is None or row.incident_key is None:
            return None
        return _meta(row), self._store.get(row.storage_ref, row.data)

    # ------------------------------------------------------------------ retention
    def purge(self, s: Session, *, now: datetime | None = None) -> dict[str, int]:
        """Delete attachments older than ``retention.attachments_days`` and abandoned drafts."""
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=self._settings.retention.attachments_days)
        stale_drafts = select(IncidentDraftToken.id).where(
            IncidentDraftToken.incident_key.is_(None),
            IncidentDraftToken.expires_at < now - timedelta(minutes=self._cfg.draft_token_ttl_minutes),
        )
        doomed = (
            s.execute(
                select(Attachment).where(
                    (Attachment.created_at < cutoff) | Attachment.draft_id.in_(stale_drafts)
                )
            )
            .scalars()
            .all()
        )
        for row in doomed:
            self._store.delete(row.storage_ref)
            s.delete(row)
        drafts = s.execute(
            delete(IncidentDraftToken).where(
                IncidentDraftToken.id.in_(stale_drafts)
                | ((IncidentDraftToken.incident_key.is_not(None)) & (IncidentDraftToken.created_at < cutoff))
            )
        )
        return {"attachments": len(doomed), "drafts": int(drafts.rowcount)}  # type: ignore[attr-defined]

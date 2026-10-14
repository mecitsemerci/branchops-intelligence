"""Append-only audit log."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from branchops.infrastructure.database.models import AuditLog
from branchops.infrastructure.observability.logging import correlation_id_var


def audit(
    session: Session,
    *,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditLog(
            actor=actor[:200],
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            correlation_id=correlation_id_var.get(),
            details=details or {},
        )
    )

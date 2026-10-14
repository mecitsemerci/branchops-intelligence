"""Normalised Jira data as received through webhooks.

These contracts are deliberately *partial*: a field set to ``None`` means "not present in the payload",
which is different from "present and empty". Merging logic relies on that distinction so that partial
update webhooks never erase known values.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from branchops.domain.enums import JiraEventType


class JiraComment(BaseModel):
    model_config = ConfigDict(frozen=True)

    comment_id: str
    body: str = ""
    author: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted: bool = False


class JiraIssueSnapshot(BaseModel):
    """Best-known state of a Jira issue, built by merging webhook payloads."""

    issue_key: str
    issue_id: str | None = None
    project_key: str | None = None
    summary: str | None = None
    description: str | None = None
    status: str | None = None
    issue_type: str | None = None
    priority: str | None = None
    labels: list[str] | None = None
    components: list[str] | None = None
    reporter: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Values of Jira custom fields mapped to canonical names (e.g. branch_code -> "1234").
    mapped_fields: dict[str, str] = Field(default_factory=dict)
    comments: list[JiraComment] = Field(default_factory=list)
    # Last source timestamp at which each top-level attribute was written (ISO strings).
    field_versions: dict[str, str] = Field(default_factory=dict)
    deleted: bool = False

    def active_comments(self) -> list[JiraComment]:
        return sorted(
            (c for c in self.comments if not c.deleted),
            key=lambda c: (c.created_at.timestamp() if c.created_at else 0.0, c.comment_id),
        )


class NormalizedJiraEvent(BaseModel):
    """Webhook payload normalised into a stable shape by the Jira adapter."""

    event_type: JiraEventType
    raw_event_name: str
    issue_key: str | None
    issue_id: str | None = None
    project_key: str | None = None
    event_timestamp: datetime | None = None
    source_updated_at: datetime | None = None
    # Only attributes that were *present* in the payload are set.
    issue_fields: dict[str, Any] = Field(default_factory=dict)
    mapped_fields: dict[str, str] = Field(default_factory=dict)
    comment: JiraComment | None = None
    changed_fields: list[str] = Field(default_factory=list)
    actor: str | None = None

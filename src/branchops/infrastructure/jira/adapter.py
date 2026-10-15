"""Deprecated location of the Jira adapter; use :mod:`branchops.infrastructure.sources.jira.adapter`."""

from __future__ import annotations

from branchops.infrastructure.sources.jira.adapter import (
    ISSUE_KEY_RE,
    JiraPayloadError,
    JiraWebhookAdapter,
    adf_to_text,
    custom_field_text,
    parse_jira_datetime,
    resolve_event_type,
    rich_text,
)

__all__ = [
    "ISSUE_KEY_RE",
    "JiraPayloadError",
    "JiraWebhookAdapter",
    "adf_to_text",
    "custom_field_text",
    "parse_jira_datetime",
    "resolve_event_type",
    "rich_text",
]

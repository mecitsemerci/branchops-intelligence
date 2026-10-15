"""MCP reporting tools: check a draft, report an incident, add an update (registered only when
``reporting.enabled``). They write to the inbox / outboxes only; analysis happens in the worker."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
from sqlalchemy import select

from branchops.application.commands.report_incident import (
    AddIncidentUpdate,
    ReportError,
    ReportIncident,
    check_draft,
)
from branchops.domain.entities.incident import parse_incident_key
from branchops.domain.entities.report import DraftCheck, IncidentDraft
from branchops.infrastructure.database.models import Issue, WebhookInbox
from branchops.infrastructure.observability.logging import correlation_id_var
from branchops.mcp.auth import Caller
from branchops.mcp.runtime import McpRuntime

WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
REPORTING_TOOLS = frozenset({"check_incident_draft", "report_incident", "add_incident_update"})


class ReportOutcome(BaseModel):
    incident_key: str = Field(description="Key of the new incident, e.g. 'app:RPT-AB12CD34EF'.")
    status: str = Field(description="'accepted' (new) or 'duplicate' (same idempotency_key as before).")
    ticketing: str = Field(description="'queued' when a ticket will be opened, else 'disabled'.")
    check: DraftCheck = Field(description="Missing / invalid fields at submission time.")
    extra: dict[str, Any] = Field(default_factory=dict)


class UpdateOutcome(BaseModel):
    incident_key: str
    comment_id: str
    status: str = "accepted"


def reporter_of_pending(s: Any, source: str, external_key: str) -> str | None:
    """Reporter of a report that is still in the inbox (not yet merged into ``issues``)."""
    payload = s.execute(
        select(WebhookInbox.payload)
        .where(
            WebhookInbox.source == source,
            WebhookInbox.issue_key == external_key,
            WebhookInbox.raw_event_name == "mcp:report_incident",
        )
        .limit(1)
    ).scalar_one_or_none()
    if not isinstance(payload, dict):
        return None
    actor = (payload.get("incident_event") or {}).get("actor")
    return str(actor) if actor else None


def can_update(runtime: McpRuntime, caller: Caller, incident_key: str) -> bool:
    """In scope (analysed incident) or reported by the caller (still waiting in the inbox)."""
    source, external_key = parse_incident_key(incident_key)

    def check(s: Any) -> bool:
        if runtime.read_models.issue_detail(s, incident_key, scope=caller.scope) is not None:
            return True
        if s.get(Issue, f"{source}:{external_key}") is not None:
            return False
        return reporter_of_pending(s, source, external_key) == caller.username

    with runtime.db.session() as s:
        return check(s)


def register_reporting_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    settings = runtime.settings

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def check_incident_draft(draft: IncidentDraft) -> DraftCheck:
        """Check a draft incident report before submitting it (nothing is stored).

        Returns the template the incident would be scored with, the required and optional fields that are
        still missing (with what to ask the user), invalid values and prompt-injection flags. Ask the user
        only for the missing required fields, then call `report_incident`.
        """
        runtime.current_caller()
        return check_draft(draft, runtime.templates)

    @mcp.tool(annotations=WRITE)
    async def report_incident(
        draft: IncidentDraft,
        idempotency_key: Annotated[
            str | None,
            Field(
                default=None,
                max_length=100,
                description="Client-chosen key; repeating a call with the same key never creates a second "
                "incident (safe retries).",
            ),
        ] = None,
    ) -> ReportOutcome:
        """Report a new incident on behalf of the signed-in user (always confirm the content with them first).

        The incident is analysed asynchronously by BranchOps (classification, completeness and quality
        scores, recommendations); use `get_incident_analysis` with the returned key later.
        """
        caller = runtime.current_caller()
        try:
            command = ReportIncident(runtime.db, settings, runtime.templates)
            result = await runtime.call(
                lambda: command.execute(
                    draft,
                    reporter=caller.username,
                    correlation_id=correlation_id_var.get() or "mcp",
                    idempotency_key=idempotency_key,
                )
            )
        except ReportError as exc:
            raise ToolError(str(exc)) from exc
        return ReportOutcome(
            incident_key=result.incident_key,
            status="accepted" if result.created else "duplicate",
            ticketing=result.ticketing,
            check=result.check,
            extra=result.extra,
        )

    @mcp.tool(annotations=WRITE)
    async def add_incident_update(
        incident_key: Annotated[str, Field(max_length=200, description="Key returned by report_incident.")],
        text: Annotated[
            str, Field(min_length=3, max_length=4000, description="New information, e.g. 'It works again'.")
        ],
    ) -> UpdateOutcome:
        """Add an update (comment) to an incident reported through BranchOps. The incident is re-analysed."""
        caller = runtime.current_caller()
        key = incident_key.strip()
        if not await runtime.call(lambda: can_update(runtime, caller, key)):
            raise ToolError("incident not found or not visible to your team")
        try:
            command = AddIncidentUpdate(runtime.db, settings)
            comment_id, _ = await runtime.call(
                lambda: command.execute(
                    key, text, author=caller.username, correlation_id=correlation_id_var.get() or "mcp"
                )
            )
        except (ReportError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        source, external_key = parse_incident_key(key)
        return UpdateOutcome(incident_key=f"{source}:{external_key}", comment_id=comment_id)

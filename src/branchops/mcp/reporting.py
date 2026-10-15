"""MCP reporting tools: check a draft, report an incident, add an update (registered only when
``reporting.enabled``). They write to the inbox / outboxes only; analysis happens in the worker."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
from sqlalchemy import select

from branchops.application.commands.attachments import AttachmentRejectedError, DraftTokenError
from branchops.application.commands.in_app_report import InAppReporting
from branchops.application.commands.report_incident import ReportError
from branchops.domain.entities.incident import incident_key as make_incident_key
from branchops.domain.entities.incident import is_valid_incident_key, parse_incident_key
from branchops.domain.entities.report import DraftCheck, IncidentDraft
from branchops.infrastructure.database.models import Issue, WebhookInbox
from branchops.infrastructure.observability.logging import correlation_id_var
from branchops.mcp.auth import Caller
from branchops.mcp.runtime import McpRuntime

WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
REPORTING_TOOLS = frozenset(
    {"check_incident_draft", "report_incident", "add_incident_update", "upload_attachment"}
)


class ReportOutcome(BaseModel):
    incident_key: str = Field(description="Key of the new incident, e.g. 'app:RPT-AB12CD34EF'.")
    status: str = Field(description="'accepted' (new) or 'duplicate' (same idempotency_key as before).")
    ticketing: str = Field(description="'queued' when a ticket will be opened, else 'disabled'.")
    check: DraftCheck = Field(description="Missing / invalid fields at submission time.")
    extra: dict[str, Any] = Field(default_factory=dict)


class AttachmentOutcome(BaseModel):
    attachment_id: str
    filename: str
    content_type: str
    size_bytes: int = Field(description="Size after re-encoding / masking.")
    blurred_regions: int
    incident_key: str | None = Field(description="Null while attached to a draft (bound on report_incident).")
    ticketing: str = Field(description="'queued', 'disabled' or 'pending_report'.")


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


def canonical_key(incident_key: str) -> str:
    """Validate and normalise once; the authorisation check and the write must use the same key."""
    key = incident_key.strip()
    if not is_valid_incident_key(key):
        raise ToolError("invalid incident key; expected '<source>:<external key>'")
    return make_incident_key(*parse_incident_key(key))


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
    flow = InAppReporting(runtime.db, runtime.settings, runtime.templates)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
    async def check_incident_draft(draft: IncidentDraft) -> DraftCheck:
        """Check a draft incident report before submitting it (the report itself is not stored).

        Returns the template the incident would be scored with, the required and optional fields that are
        still missing (with what to ask the user), invalid values and prompt-injection flags. Application and
        screen names known from the ownership registry (via `draft.app_context.app_id` / `screen_id`) count as
        provided. Also returns a short-lived `draft_token`: upload screenshots with `upload_attachment` using
        it, then pass it to `report_incident`. Ask the user only for the missing required fields.
        """
        caller = runtime.current_caller()
        return await runtime.call(lambda: flow.check(draft, actor=caller.username))

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
        draft_token: Annotated[
            str | None,
            Field(
                default=None, max_length=100, description="Token from check_incident_draft, to bind uploads."
            ),
        ] = None,
    ) -> ReportOutcome:
        """Report a new incident on behalf of the signed-in user (always confirm the content with them first).

        `draft.app_context` (application, screen, route, error code, recent requests, log excerpt, device…) is
        stored masked, used for routing and never sent to a model. Attachments uploaded with `draft_token`
        are bound to the new incident. The incident is analysed asynchronously; use `get_incident_analysis`
        with the returned key later.
        """
        caller = runtime.current_caller()
        try:
            result = await runtime.call(
                lambda: flow.report(
                    draft,
                    actor=caller.username,
                    correlation_id=correlation_id_var.get() or "mcp",
                    idempotency_key=idempotency_key,
                    draft_token=draft_token,
                )
            )
        except (ReportError, DraftTokenError, AttachmentRejectedError) as exc:
            raise ToolError(str(exc)) from exc
        return ReportOutcome(
            incident_key=result.incident_key,
            status="accepted" if result.created else "duplicate",
            ticketing=result.ticketing,
            check=result.check,
            extra=result.extra,
        )

    @mcp.tool(annotations=WRITE)
    async def upload_attachment(
        filename: Annotated[str, Field(min_length=1, max_length=200, description="e.g. 'ekran.png'.")],
        content_type: Annotated[
            Literal["image/png", "image/jpeg", "text/plain"], Field(description="MIME type of the content.")
        ],
        data_base64: Annotated[str, Field(min_length=4, description="File content, standard base64.")],
        kind: Annotated[
            Literal["screenshot", "log", "other"], Field(description="What the file is.")
        ] = "screenshot",
        incident_key: Annotated[
            str | None,
            Field(default=None, max_length=200, description="Existing incident (or use draft_token)."),
        ] = None,
        draft_token: Annotated[
            str | None, Field(default=None, max_length=100, description="Token from check_incident_draft.")
        ] = None,
        blur_regions: Annotated[
            list[list[int]] | None,
            Field(
                default=None,
                max_length=20,
                description="Image areas to blur server-side, each [x, y, width, height] in pixels "
                "(e.g. customer names or account numbers visible on screen).",
            ),
        ] = None,
    ) -> AttachmentOutcome:
        """Attach a screenshot or log to an incident, or to a draft before `report_incident`.

        Limits: PNG / JPEG / plain text, at most 3 MB (configurable) and 5 files per incident.
        Images are re-encoded (metadata removed) and the given regions are blurred; text is PII-masked.
        Attachments are never sent to a model.
        """
        caller = runtime.current_caller()
        key = canonical_key(incident_key) if incident_key is not None else None
        if key is not None and not await runtime.call(lambda: can_update(runtime, caller, key)):
            raise ToolError("incident not found or not visible to your team")
        try:
            result = await runtime.call(
                lambda: flow.upload(
                    actor=caller.username,
                    filename=filename,
                    content_type=content_type,
                    data_base64=data_base64,
                    kind=kind,
                    incident_key=key,
                    draft_token=draft_token,
                    blur_regions=blur_regions,
                )
            )
        except (AttachmentRejectedError, DraftTokenError) as exc:
            raise ToolError(str(exc)) from exc
        return AttachmentOutcome(
            attachment_id=result.attachment_id,
            filename=result.filename,
            content_type=result.content_type,
            size_bytes=result.size_bytes,
            blurred_regions=result.blurred_regions,
            incident_key=result.incident_key,
            ticketing=result.ticketing,
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
        key = canonical_key(incident_key)
        if not await runtime.call(lambda: can_update(runtime, caller, key)):
            raise ToolError("incident not found or not visible to your team")
        try:
            comment_id, _ = await runtime.call(
                lambda: flow.update(
                    key, text, actor=caller.username, correlation_id=correlation_id_var.get() or "mcp"
                )
            )
        except (ReportError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return UpdateOutcome(incident_key=key, comment_id=comment_id)

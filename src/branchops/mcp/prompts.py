"""MCP prompts: reusable instructions telling the client which tools to call and how to present results.

Prompts contain no bank data; they only embed the arguments the user typed.
"""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

_GROUND_RULES = (
    "Ground rules: only use data returned by the BranchOps tools; never invent incidents, numbers or "
    "quotes. Scores are 0-100 and computed deterministically by BranchOps — report them, do not "
    "recompute them. Cite incident keys (e.g. jira:OPS-123) for every claim. If a tool says an incident "
    "is not found, it may be outside your team's scope; say so instead of guessing."
)


def register_prompts(mcp: FastMCP) -> None:
    @mcp.prompt(
        name="weekly-branch-report", description="Weekly incident and data-quality report for one branch."
    )
    def weekly_branch_report(
        branch_code: Annotated[str, Field(min_length=1, max_length=32, description="Branch code.")],
        days: Annotated[int, Field(ge=1, le=90, description="Period length in days.")] = 7,
    ) -> str:
        return f"""Prepare an operational report for branch {branch_code} covering the last {days} days.

Steps:
1. Call search_incidents(branch_code="{branch_code}", since_days={days}, sort="updated", page_size=50);
   page until has_more is false.
2. Call list_recurring_errors(since_days={days}, min_count=2, branch_code="{branch_code}").
3. For the 5 incidents with the lowest quality score, call get_incident_analysis to collect missing
   fields and recommendations.
4. Optionally read branchops://glossary to explain categories, statuses and scores.

Present:
- A one-paragraph summary (incident count, average completeness and quality, NEEDS_REVIEW count).
- A table: incident key | category | analysis status | completeness | quality.
- Recurring errors: signature, count, first/last seen.
- The most frequent missing fields and the concrete information branch staff should add next time.

{_GROUND_RULES}"""

    @mcp.prompt(
        name="investigate-recurring-error",
        description="Investigate one recurring error signature across branches and sources.",
    )
    def investigate_recurring_error(
        error_signature: Annotated[
            str,
            Field(min_length=1, max_length=200, description="Error signature from list_recurring_errors."),
        ],
    ) -> str:
        return f"""Investigate the recurring error "{error_signature}".

Steps:
1. Call list_recurring_errors(since_days=90, min_count=2) and find the group with this error_signature
   (affected branches, applications, sources, first/last seen, incident keys).
2. For up to 5 of its incident keys call get_incident and get_incident_analysis.
3. Call find_similar_incidents(incident_key=<the most recent key>) for related or duplicate reports.

Present:
- Timeline (first seen, last seen, frequency) and scope (branches, applications, sources).
- What the incidents have in common, quoting evidence that BranchOps marked as verified.
- Gaps in the reports that block root-cause analysis (missing or insufficient fields).
- Clearly separate observations from hypotheses; similar symptoms do not prove the same root cause.

{_GROUND_RULES}"""

    @mcp.prompt(
        name="missing-information-checklist",
        description="Checklist of information missing from one incident, based on its template.",
    )
    def missing_information_checklist(
        incident_key: Annotated[
            str, Field(min_length=1, max_length=200, description="Incident key, e.g. 'jira:OPS-123'.")
        ],
    ) -> str:
        return f"""Create a checklist of the information missing from incident {incident_key}.

Steps:
1. Call get_incident_analysis(incident_key="{incident_key}"). If analysed is false, say so and stop.
2. Call get_template(template_id=<template_id>, version=<template_version>) from the analysis to get
   each field's label, whether it is required, and its rubric.

Present a checklist ordered by: missing required fields, other missing fields, insufficient fields.
For each item give the field label, why it matters (rubric "good" description), and the BranchOps
recommendation text. End with a short message the analyst can send to the branch asking for exactly
these details.

{_GROUND_RULES}"""

    @mcp.prompt(
        name="report-from-app",
        description="Guided incident report for an agent embedded in a bank application.",
    )
    def report_from_app(
        app_id: Annotated[str, Field(min_length=1, max_length=64, description="Host application key.")],
        screen_id: Annotated[str | None, Field(max_length=64, description="Current screen key.")] = None,
    ) -> str:
        screen = f', screen_id="{screen_id}"' if screen_id else ""
        return f"""You are embedded in the bank application "{app_id}". Help the signed-in user report the
problem they are seeing to BranchOps, with as little typing as possible.

Steps:
1. Gather `app_context` from the host application yourself — do not ask the user for it: app_id="{app_id}"
   {screen}, screen_title, route (internal path), build_version, environment, error_code and error_message
   shown on screen, correlation_id / trace_id, the last (max 20) HTTP requests (method, path, status,
   duration_ms — never bodies, headers or query strings), a log excerpt (max 4000 characters), user_context
   (branch_code, role) and device (os, browser, resolution), occurred_at.
2. Call resolve_owner(app_id="{app_id}"{screen}, route=<route>) and tell the user which team will receive the
   report (branch operations is always in copy).
3. Ask the user, in Turkish, one short question: what were they trying to do and what happened? Draft a
   title and description from their answer.
4. Call check_incident_draft(draft) with the draft and app_context. For every item in missing_required, ask
   the user only that question (use the `recommendation` text); fix values listed in `invalid`. Do not ask
   for optional fields unless the user volunteers them. Keep the returned `draft_token`.
5. Take a screenshot of the current screen. Before uploading, find areas showing customer data (names,
   TCKN, IBAN, card or account numbers, phone numbers) and pass them as blur_regions [x, y, w, h]. Call
   upload_attachment(draft_token=<token>, filename="ekran.png", content_type="image/png", kind="screenshot",
   data_base64=<png>, blur_regions=[...]). Attach the log excerpt as a text/plain file only if it helps.
6. Show the user the final title, description and attachments and ask for confirmation. Only after an
   explicit yes call report_incident(draft, draft_token=<token>, idempotency_key=<a new random id>).
7. Show the returned incident_key, the owning team from step 2, and the notification / ticket status.
   Tell the user they can add information later (add_incident_update with the incident_key).

Rules: never invent technical details; never include passwords, tokens or full customer identifiers in the
text; if a tool fails, say so and do not retry report_incident with a different idempotency_key.
{_GROUND_RULES}"""

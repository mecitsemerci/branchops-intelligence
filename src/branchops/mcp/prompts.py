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

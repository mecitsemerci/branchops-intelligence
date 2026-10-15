"""MCP tool catalogue: fixed, parameterised, team-scoped queries with typed, size-bounded outputs.

Every tool reads through ``application/queries`` with the caller's :class:`QueryScope` applied in SQL. There
is deliberately no raw-SQL or free-form query tool. ``request_reanalysis`` is the only tool that writes
(it enqueues an inbox event; it never runs the analysis itself).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from branchops.application.commands.ingest_webhook import RequestReanalysis
from branchops.application.queries.filters import IssueFilter
from branchops.domain.entities.incident import is_valid_incident_key
from branchops.infrastructure.observability.logging import correlation_id_var
from branchops.infrastructure.persistence.audit import audit
from branchops.mcp.auth import Caller
from branchops.mcp.output import TextRenderer
from branchops.mcp.ownership_tools import OWNERSHIP_TOOLS
from branchops.mcp.reporting import REPORTING_TOOLS
from branchops.mcp.runtime import McpRuntime
from branchops.templates.model import IssueTemplate

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
SUMMARY_CHARS = 300
COMMENT_LIMIT = 20
NOT_FOUND = "incident not found or not visible to your team"

IncidentKey = Annotated[
    str,
    Field(
        min_length=1,
        max_length=200,
        description="Incident key '<source>:<external key>', e.g. 'jira:OPS-123' or 'snow:INC0012345'. "
        "A bare Jira key such as 'OPS-123' means 'jira:OPS-123'.",
    ),
]
SinceDays = Annotated[int, Field(ge=1, le=365, description="Look-back window in days (1-365).")]


# --------------------------------------------------------------------------------------------- outputs
class IncidentSummary(BaseModel):
    incident_key: str
    source: str = Field(description="Source system id, e.g. 'jira', 'snow', 'csv'.")
    external_key: str = Field(description="Key of the record in its source system.")
    summary: str | None = Field(description=f"Title, at most {SUMMARY_CHARS} characters.")
    source_status: str | None = Field(description="Workflow status in the source system.")
    category: str | None
    subcategory: str | None
    branch_code: str | None
    application: str | None
    completeness: float | None = Field(description="Completeness score 0-100 of the latest analysis.")
    quality: float | None = Field(description="Quality score 0-100 of the latest analysis.")
    analysis_status: str | None = Field(description="COMPLETED, PARTIAL, NEEDS_REVIEW, FAILED or RUNNING.")
    updated_at: str | None = Field(description="Last update in the source system (ISO 8601).")


class IncidentPage(BaseModel):
    items: list[IncidentSummary]
    total: int = Field(description="Number of matching incidents across all pages.")
    page: int
    page_size: int
    has_more: bool
    truncated: list[str] = Field(default_factory=list, description="Paths of text values that were cut.")


class CommentOut(BaseModel):
    id: str
    author: str | None = Field(description="Hidden (null) for teams with masked fields.")
    body: str | None
    created_at: str | None


class LatestScores(BaseModel):
    analysis_id: str
    status: str
    analysed_at: str
    completeness: float | None
    quality: float | None
    template: str | None = Field(description="'<template_id>@<version>' used for scoring.")
    scoring_policy_version: str | None


class IncidentDetail(BaseModel):
    incident_key: str
    source: str
    external_key: str
    group_key: str | None = Field(description="Jira project / ServiceNow assignment group / import batch.")
    summary: str | None
    description: str | None
    source_status: str | None
    category: str | None
    subcategory: str | None
    branch_code: str | None
    application: str | None
    source_created_at: str | None
    source_updated_at: str | None
    comments: list[CommentOut] = Field(description=f"The {COMMENT_LIMIT} most recent comments, oldest first.")
    comments_total: int
    latest: LatestScores | None = Field(description="Latest analysis scores; null if never analysed.")
    fields_mode: Literal["full", "masked"]
    truncated: list[str] = Field(default_factory=list)


class EvidenceQuote(BaseModel):
    field: str = Field(description="Canonical field (or 'classification') the quote supports.")
    source: str = Field(description="'summary', 'description', 'comment:<id>' or 'field:<id>'.")
    quote: str
    verified: bool = Field(description="True when Python confirmed the quote occurs in the source text.")


class FieldAssessmentOut(BaseModel):
    field: str
    state: str | None = Field(description="SUFFICIENT, INSUFFICIENT, UNKNOWN, MISSING or NOT_APPLICABLE.")
    level: str | None = Field(description="Quality rubric level: GOOD, ACCEPTABLE, POOR or ABSENT.")
    weight: float | None
    required: bool | None
    completeness_credit: float | None = Field(description="0-1; null when excluded from the score.")
    quality_credit: float | None = Field(description="0-1; null when excluded from the score.")
    reason: str | None
    validation_errors: list[str] = Field(default_factory=list)


class RecommendationOut(BaseModel):
    field: str | None
    priority: str
    message: str


class ClassificationOut(BaseModel):
    category: str | None
    subcategory: str | None
    confidence: str | None
    rationale: str | None


class IncidentAnalysis(BaseModel):
    incident_key: str
    analysed: bool = Field(description="False when the incident has no analysis yet (other fields null).")
    analysis_id: str | None = None
    status: str | None = None
    analysed_at: str | None = None
    classification: ClassificationOut | None = None
    template_id: str | None = None
    template_version: str | None = None
    scoring_policy_version: str | None = None
    prompt_version: str | None = None
    model_id: str | None = None
    completeness_score: float | None = Field(default=None, description="0-100, computed in Python.")
    quality_score: float | None = Field(default=None, description="0-100, computed in Python.")
    field_assessments: list[FieldAssessmentOut] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    missing_required_fields: list[str] = Field(default_factory=list)
    insufficient_fields: list[str] = Field(default_factory=list)
    unknown_fields: list[str] = Field(default_factory=list)
    recommendations: list[RecommendationOut] = Field(default_factory=list)
    evidence: list[EvidenceQuote] = Field(default_factory=list)
    recurring: bool = False
    recurring_count: int = 0
    fields_mode: Literal["full", "masked"] = "masked"
    truncated: list[str] = Field(default_factory=list)


class SimilarIncident(BaseModel):
    incident_key: str
    source: str
    summary: str | None
    branch_code: str | None
    relationship: str = Field(
        description="DUPLICATE_CANDIDATE, RELATED, UNVERIFIED_CANDIDATE (stored relationships), RECURRING "
        "(same error signature) or TEXT_MATCH (full-text search)."
    )
    text_rank: float = Field(description="PostgreSQL full-text rank; higher = more similar wording.")
    reasons: list[str]


class SimilarIncidents(BaseModel):
    reference: str = Field(description="The incident key, or 'text' for a free-text search.")
    items: list[SimilarIncident]
    truncated: list[str] = Field(default_factory=list)


class RecurringError(BaseModel):
    error_signature: str
    count: int
    first_seen: str | None
    last_seen: str | None
    branch_codes: list[str] = Field(description="Affected branches.")
    applications: list[str]
    sources: list[str]
    incident_keys: list[str] = Field(description="Up to 20 most recent incident keys.")


class RecurringErrors(BaseModel):
    since_days: int
    min_count: int
    items: list[RecurringError]


class CategoryCount(BaseModel):
    category: str
    count: int
    avg_quality: float | None


class SourceCount(BaseModel):
    source: str
    count: int
    analysed: int
    avg_quality: float | None


class DailyPoint(BaseModel):
    day: str
    issues: int
    avg_completeness: float | None
    avg_quality: float | None


class QualityOverview(BaseModel):
    since_days: int = Field(description="Window of daily_trend; the totals cover all visible incidents.")
    total_incidents: int
    analysed_incidents: int
    avg_completeness: float | None
    avg_quality: float | None
    low_quality_threshold: float
    low_quality_incidents: int = Field(description="Latest quality below low_quality_threshold.")
    needs_review: int
    category_distribution: list[CategoryCount]
    source_distribution: list[SourceCount]
    daily_trend: list[DailyPoint]


class GroupScores(BaseModel):
    key: str = Field(description="Category or branch code.")
    issues: int
    avg_completeness: float | None
    avg_quality: float | None


class FieldCount(BaseModel):
    field: str
    count: int


class Improvement(BaseModel):
    incident_key: str
    first_quality: float | None
    last_quality: float | None
    quality_delta: float | None
    first_completeness: float | None
    last_completeness: float | None


class QualityAnalytics(BaseModel):
    since_days: int
    by_category: list[GroupScores]
    by_branch: list[GroupScores] = Field(description="Top 25 branches by incident count.")
    most_missing_fields: list[FieldCount]
    improvements: list[Improvement] = Field(description="Incidents whose quality changed after re-analysis.")


class TemplateSummary(BaseModel):
    template_id: str
    version: str
    name: str
    description: str
    category: str
    subcategories: list[str]
    is_fallback: bool
    required_fields: list[str]
    field_count: int


class TemplateList(BaseModel):
    items: list[TemplateSummary]


class RubricOut(BaseModel):
    criteria: str
    good: str
    acceptable: str
    poor: str


class TemplateFieldOut(BaseModel):
    name: str = Field(description="Canonical field name.")
    label: str
    weight: float = Field(description="Relative weight in the completeness and quality scores.")
    required: bool
    required_when: dict[str, Any] | None = Field(description="Condition making the field required.")
    applicable_when: dict[str, Any] | None
    allow_not_applicable: bool
    validation: dict[str, Any]
    rubric: RubricOut
    recommendation: str


class TemplateDetail(BaseModel):
    template_id: str
    version: str
    status: str
    name: str
    description: str
    category: str
    subcategories: list[str]
    is_fallback: bool
    applicable_when: dict[str, Any] | None
    fields: list[TemplateFieldOut]


class InboxHealth(BaseModel):
    pending: int
    processing: int
    dead_letters: int
    oldest_pending_seconds: float | None
    active_workers: int = Field(description="Distinct workers with a heartbeat in the window.")
    worker_window_minutes: int
    by_status: dict[str, int]
    by_source: list[dict[str, Any]]


class ReanalysisQueued(BaseModel):
    incident_key: str
    status: Literal["queued"]
    event_id: str | None


# --------------------------------------------------------------------------------------------- helpers
def template_summary(t: IssueTemplate) -> TemplateSummary:
    return TemplateSummary(
        template_id=t.template_id,
        version=t.version,
        name=t.name,
        description=t.description,
        category=t.category,
        subcategories=list(t.subcategories),
        is_fallback=t.is_fallback,
        required_fields=[f.name for f in t.fields if f.required],
        field_count=len(t.fields),
    )


def template_detail(t: IssueTemplate) -> TemplateDetail:
    def cond(c: Any) -> dict[str, Any] | None:
        return c.model_dump(by_alias=True, exclude_none=True) if c is not None else None

    return TemplateDetail(
        template_id=t.template_id,
        version=t.version,
        status=t.status,
        name=t.name,
        description=t.description,
        category=t.category,
        subcategories=list(t.subcategories),
        is_fallback=t.is_fallback,
        applicable_when=cond(t.applicable_when),
        fields=[
            TemplateFieldOut(
                name=f.name,
                label=f.label,
                weight=f.weight,
                required=f.required,
                required_when=cond(f.required_when),
                applicable_when=cond(f.applicable_when),
                allow_not_applicable=f.allow_not_applicable,
                validation=f.validation.model_dump(exclude_none=True, exclude_defaults=True),
                rubric=RubricOut(**f.rubric.model_dump()),
                recommendation=f.recommendation,
            )
            for f in t.fields
        ],
    )


def _check_key(incident_key: str) -> str:
    key = incident_key.strip()
    if not is_valid_incident_key(key):
        raise ToolError("invalid incident key; expected '<source>:<external key>', e.g. 'jira:OPS-123'")
    return key


def _summary_row(item: dict[str, Any], text: TextRenderer, path: str) -> IncidentSummary:
    return IncidentSummary(
        incident_key=item["incident_key"],
        source=item["source"],
        external_key=item["external_key"],
        summary=text(item.get("summary"), f"{path}.summary", limit=SUMMARY_CHARS),
        source_status=item.get("source_status"),
        category=item.get("category"),
        subcategory=item.get("subcategory"),
        branch_code=item.get("branch_code"),
        application=item.get("application"),
        completeness=item.get("completeness"),
        quality=item.get("quality"),
        analysis_status=item.get("analysis_status"),
        updated_at=item.get("updated_at"),
    )


def _similar(
    runtime: McpRuntime, caller: Caller, reference: str, rows: list[dict[str, Any]]
) -> SimilarIncidents:
    truncated: list[str] = []
    items = []
    for n, r in enumerate(rows):
        text = runtime.renderer(caller, r["source"], r.get("group_key"), r.get("branch_code"))
        items.append(
            SimilarIncident(
                incident_key=r["incident_key"],
                source=r["source"],
                summary=text(r.get("summary"), f"items[{n}].summary", limit=SUMMARY_CHARS),
                branch_code=r.get("branch_code"),
                relationship=str(r["relationship"]),
                text_rank=float(r.get("text_rank") or 0.0),
                reasons=[
                    text(str(x), f"items[{n}].reasons", limit=500) or "" for x in r.get("reasons") or []
                ],
            )
        )
        truncated.extend(text.truncated)
    return SimilarIncidents(reference=reference, items=items, truncated=truncated)


def _analysis(
    key: str, a: dict[str, Any], text: TextRenderer, mode: Literal["full", "masked"]
) -> IncidentAnalysis:
    cls = a.get("classification") or {}
    assessments = {x.get("field"): x for x in a.get("assessments") or []}
    evidence: list[EvidenceQuote] = []

    def add_evidence(field: str, refs: list[dict[str, Any]]) -> None:
        for ref in refs or []:
            n = len(evidence)
            evidence.append(
                EvidenceQuote(
                    field=field,
                    source=str(ref.get("source", "")),
                    quote=text(str(ref.get("quote", "")), f"evidence[{n}].quote", limit=500) or "",
                    verified=bool(ref.get("verified", False)),
                )
            )

    add_evidence("classification", cls.get("evidence") or [])
    fields: list[FieldAssessmentOut] = []
    for fs in a.get("field_scores") or []:
        name = fs.get("field")
        assess = assessments.get(name) or {}
        add_evidence(str(name), assess.get("evidence") or [])
        fields.append(
            FieldAssessmentOut(
                field=str(name),
                state=fs.get("state"),
                level=assess.get("level") or fs.get("rubric_level"),
                weight=fs.get("weight"),
                required=fs.get("required"),
                completeness_credit=fs.get("completeness_credit"),
                quality_credit=fs.get("quality_credit"),
                reason=text(assess.get("reason"), f"field_assessments.{name}.reason", limit=600),
                validation_errors=list(fs.get("validation_errors") or []),
            )
        )
    return IncidentAnalysis(
        incident_key=key,
        analysed=True,
        analysis_id=a.get("analysis_id"),
        status=a.get("status"),
        analysed_at=a.get("completed_at") or a.get("started_at"),
        classification=ClassificationOut(
            category=cls.get("category"),
            subcategory=cls.get("subcategory"),
            confidence=cls.get("confidence"),
            rationale=text(cls.get("rationale"), "classification.rationale", limit=600),
        )
        if cls
        else None,
        template_id=a.get("template_id"),
        template_version=a.get("template_version"),
        scoring_policy_version=a.get("scoring_policy_version"),
        prompt_version=a.get("prompt_version"),
        model_id=a.get("model_id"),
        completeness_score=a.get("completeness_score"),
        quality_score=a.get("quality_score"),
        field_assessments=fields,
        missing_fields=list(a.get("missing_fields") or []),
        missing_required_fields=list(a.get("missing_required_fields") or []),
        insufficient_fields=list(a.get("insufficient_fields") or []),
        unknown_fields=list(a.get("unknown_fields") or []),
        recommendations=[
            RecommendationOut(
                field=r.get("field"),
                priority=str(r.get("priority", "MEDIUM")),
                message=text(str(r.get("message", "")), f"recommendations[{n}]", limit=800) or "",
            )
            for n, r in enumerate(a.get("recommendations") or [])
        ],
        evidence=evidence,
        recurring=bool(a.get("recurring")),
        recurring_count=int(a.get("recurring_count") or 0),
        fields_mode=mode,
        truncated=text.truncated,
    )


# --------------------------------------------------------------------------------------------- tools
def register_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    rm = runtime.read_models

    @mcp.tool(annotations=READ_ONLY)
    async def search_incidents(
        query: Annotated[
            str | None,
            Field(
                max_length=200,
                description="Words to search in key, title and description (Turkish stemming).",
            ),
        ] = None,
        category: Annotated[
            str | None, Field(max_length=64, description="Category, e.g. 'TRANSACTION'.")
        ] = None,
        branch_code: Annotated[str | None, Field(max_length=32, description="Exact branch code.")] = None,
        application: Annotated[
            str | None, Field(max_length=100, description="Application name (substring, case-insensitive).")
        ] = None,
        source: Annotated[
            str | None, Field(max_length=40, description="Source id: 'jira', 'snow', 'csv'.")
        ] = None,
        status: Annotated[
            Literal["COMPLETED", "PARTIAL", "NEEDS_REVIEW", "FAILED", "RUNNING"] | None,
            Field(description="Status of the latest analysis (not the source workflow status)."),
        ] = None,
        low_quality_only: Annotated[
            bool, Field(description="Only incidents whose latest quality score is below the threshold.")
        ] = False,
        since_days: SinceDays = 30,
        sort: Annotated[
            Literal["updated", "quality", "completeness", "incident_key"],
            Field(
                description="'updated' = newest first; 'quality'/'completeness' = lowest score first; "
                "'incident_key' = alphabetical."
            ),
        ] = "updated",
        page: Annotated[
            int, Field(ge=1, le=200, description="1-based page number (narrow the filters to go deeper).")
        ] = 1,
        page_size: Annotated[
            int, Field(ge=1, le=200, description="Items per page (capped by the server).")
        ] = 25,
    ) -> IncidentPage:
        """Search incidents visible to your team, newest first by default.

        Returns one page of incident summaries (scores are 0-100 and computed deterministically, not by a
        model). Use `get_incident` / `get_incident_analysis` for details. Results only ever contain incidents
        your team is allowed to see.
        """
        caller = runtime.current_caller()
        size = min(page_size, runtime.page_size_limit)
        f = IssueFilter(
            search=runtime.search_text(caller, query) if query else None,
            source=source,
            category=category,
            branch_code=branch_code,
            application=application,
            status=status,
            low_quality_only=low_quality_only,
            since_days=since_days,
            sort=sort,
            descending=sort == "updated",
            page=page,
            page_size=size,
        )
        result = await runtime.query(lambda s: rm.list_issues(s, f, scope=caller.scope))
        truncated: list[str] = []
        items = []
        for n, item in enumerate(result.items):
            text = runtime.renderer(caller, item["source"], item.get("group_key"), item.get("branch_code"))
            items.append(_summary_row(item, text, f"items[{n}]"))
            truncated.extend(text.truncated)
        return IncidentPage(
            items=items,
            total=result.total,
            page=result.page,
            page_size=result.page_size,
            has_more=result.page * result.page_size < result.total,
            truncated=truncated,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def get_incident(incident_key: IncidentKey) -> IncidentDetail:
        """Get one incident: title, description, recent comments, source status and latest scores.

        Free text is masked for teams configured with `fields: masked`; long text is truncated and listed in
        `truncated`. Fails with 'not found' for incidents outside your team's scope.
        """
        caller = runtime.current_caller()
        key = _check_key(incident_key)
        detail = await runtime.query(lambda s: rm.issue_detail(s, key, scope=caller.scope))
        if detail is None:
            raise ToolError(NOT_FOUND)
        issue = detail["issue"]
        text = runtime.renderer(caller, issue["source"], issue.get("group_key"), issue.get("branch_code"))
        comments = detail["comments"][-COMMENT_LIMIT:]
        latest = detail.get("latest_analysis")
        return IncidentDetail(
            incident_key=issue["incident_key"],
            source=issue["source"],
            external_key=issue["external_key"],
            group_key=issue.get("group_key"),
            summary=text(issue.get("summary"), "summary", limit=1000),
            description=text(issue.get("description"), "description"),
            source_status=issue.get("source_status"),
            category=issue.get("category"),
            subcategory=issue.get("subcategory"),
            branch_code=issue.get("branch_code"),
            application=issue.get("application"),
            source_created_at=issue.get("source_created_at"),
            source_updated_at=issue.get("source_updated_at"),
            comments=[
                CommentOut(
                    id=str(c["id"]),
                    author=None if text.mask else c.get("author"),
                    body=text(c.get("body"), f"comments[{n}].body"),
                    created_at=c.get("created_at"),
                )
                for n, c in enumerate(comments)
            ],
            comments_total=len(detail["comments"]),
            latest=LatestScores(
                analysis_id=latest["analysis_id"],
                status=latest["status"],
                analysed_at=latest.get("completed_at") or latest["started_at"],
                completeness=latest.get("completeness_score"),
                quality=latest.get("quality_score"),
                template=f"{latest['template_id']}@{latest['template_version']}"
                if latest.get("template_id")
                else None,
                scoring_policy_version=latest.get("scoring_policy_version"),
            )
            if latest
            else None,
            fields_mode="masked" if text.mask else "full",
            truncated=text.truncated,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def get_incident_analysis(incident_key: IncidentKey) -> IncidentAnalysis:
        """Get the latest analysis of an incident.

        Includes classification, per-field assessments (information state, rubric level, credits), missing
        and insufficient fields, recommendations, verified evidence quotes, and the template id/version and
        scoring policy version the scores were computed with. `analysed` is false if no analysis exists.
        """
        caller = runtime.current_caller()
        key = _check_key(incident_key)
        detail = await runtime.query(lambda s: rm.issue_detail(s, key, scope=caller.scope))
        if detail is None:
            raise ToolError(NOT_FOUND)
        issue = detail["issue"]
        text = runtime.renderer(caller, issue["source"], issue.get("group_key"), issue.get("branch_code"))
        mode: Literal["full", "masked"] = "masked" if text.mask else "full"
        latest = detail.get("latest_analysis")
        if latest is None:
            return IncidentAnalysis(incident_key=issue["incident_key"], analysed=False, fields_mode=mode)
        return _analysis(issue["incident_key"], latest, text, mode)

    @mcp.tool(annotations=READ_ONLY)
    async def find_similar_incidents(
        incident_key: Annotated[
            str | None,
            Field(max_length=200, description="Find incidents related to this incident ('jira:OPS-123')."),
        ] = None,
        text: Annotated[
            str | None,
            Field(
                min_length=3,
                max_length=500,
                description="Or: free text (e.g. an error message) to match against incident titles and "
                "descriptions from the last 365 days.",
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="Maximum number of results.")] = 10,
    ) -> SimilarIncidents:
        """Find similar, duplicate or recurring incidents. Pass exactly one of `incident_key` or `text`.

        For an incident key: relationships verified by the analysis pipeline (DUPLICATE_CANDIDATE, RELATED,
        UNVERIFIED_CANDIDATE) with their signals and rationale, then other incidents with the same error
        signature (RECURRING). For text: PostgreSQL full-text matches (TEXT_MATCH), best rank first.
        """
        caller = runtime.current_caller()
        if (incident_key is None) == (text is None):
            raise ToolError("pass exactly one of incident_key or text")
        if incident_key is not None:
            key = _check_key(incident_key)
            rows = await runtime.query(
                lambda s: rm.similar_incidents(s, key, limit=limit, scope=caller.scope)
            )
            if rows is None:
                raise ToolError(NOT_FOUND)
            return _similar(runtime, caller, key, rows)
        query_text = runtime.search_text(caller, text or "")
        found = await runtime.query(
            lambda s: rm.similar_to_text(s, query_text, limit=limit, scope=caller.scope)
        )
        return _similar(runtime, caller, "text", found)

    @mcp.tool(annotations=READ_ONLY)
    async def list_recurring_errors(
        since_days: SinceDays = 30,
        min_count: Annotated[
            int, Field(ge=2, le=1000, description="Minimum incidents per error signature.")
        ] = 3,
        branch_code: Annotated[str | None, Field(max_length=32, description="Only this branch.")] = None,
        application: Annotated[
            str | None, Field(max_length=100, description="Only this application (substring).")
        ] = None,
    ) -> RecurringErrors:
        """List recurring errors: incidents grouped by normalised error signature, most frequent first.

        Each group has the count, first/last seen (ISO 8601), affected branches/applications/sources and up to
        20 incident keys. Counts only include incidents visible to your team.
        """
        caller = runtime.current_caller()
        rows = await runtime.query(
            lambda s: rm.recurring_errors(
                s,
                days=since_days,
                min_count=min_count,
                branch_code=branch_code,
                application=application,
                limit=runtime.page_size_limit,
                scope=caller.scope,
            )
        )
        return RecurringErrors(
            since_days=since_days, min_count=min_count, items=[RecurringError(**r) for r in rows]
        )

    @mcp.tool(annotations=READ_ONLY)
    async def get_quality_overview(since_days: SinceDays = 30) -> QualityOverview:
        """Headline quality figures: totals, average completeness/quality (0-100), low-quality and
        needs-review counts, category and source distribution, and a daily trend over `since_days`."""
        caller = runtime.current_caller()
        ov = await runtime.query(lambda s: rm.overview(s, since_days, scope=caller.scope))
        return QualityOverview(
            since_days=since_days,
            total_incidents=ov.total_issues,
            analysed_incidents=ov.analysed_issues,
            avg_completeness=ov.avg_completeness,
            avg_quality=ov.avg_quality,
            low_quality_threshold=rm.low_quality_threshold,
            low_quality_incidents=ov.low_quality_issues,
            needs_review=ov.needs_review,
            category_distribution=[CategoryCount(**c) for c in ov.category_distribution],
            source_distribution=[SourceCount(**c) for c in ov.source_distribution],
            daily_trend=[DailyPoint(**d) for d in ov.daily_trend],
        )

    @mcp.tool(annotations=READ_ONLY)
    async def get_quality_analytics(since_days: SinceDays = 90) -> QualityAnalytics:
        """Quality analytics over `since_days`: average scores (0-100) by category and by branch, the most
        frequently missing fields, and incidents whose quality changed between their first and latest
        analysis."""
        caller = runtime.current_caller()
        qa = await runtime.query(lambda s: rm.quality_analytics(s, since_days, scope=caller.scope))
        return QualityAnalytics(
            since_days=since_days,
            by_category=[
                GroupScores(
                    key=r["category"], **{k: r[k] for k in ("issues", "avg_completeness", "avg_quality")}
                )
                for r in qa["by_category"]
            ],
            by_branch=[
                GroupScores(
                    key=r["branch_code"], **{k: r[k] for k in ("issues", "avg_completeness", "avg_quality")}
                )
                for r in qa["by_branch"]
            ],
            most_missing_fields=[FieldCount(**r) for r in qa["most_missing_fields"]],
            improvements=[
                Improvement(**{k: v for k, v in r.items() if k in Improvement.model_fields})
                for r in qa["improvements"]
            ],
        )

    @mcp.tool(annotations=READ_ONLY)
    async def list_templates() -> TemplateList:
        """List the latest published incident templates (one per template id) with category, subcategories
        and required fields. Templates define which information an incident should contain."""
        return TemplateList(items=[template_summary(t) for t in runtime.templates.latest_published()])

    @mcp.tool(annotations=READ_ONLY)
    async def get_template(
        template_id: Annotated[
            str, Field(max_length=64, description="Template id, e.g. 'transaction-incident'.")
        ],
        version: Annotated[
            str | None,
            Field(max_length=20, description="Semantic version 'MAJOR.MINOR.PATCH'; default latest."),
        ] = None,
    ) -> TemplateDetail:
        """Get a published template: every field with weight, required/conditional rules, validation rules,
        quality rubric (good / acceptable / poor) and the recommendation shown when it is missing."""
        if version is None:
            template = next(
                (t for t in runtime.templates.latest_published() if t.template_id == template_id), None
            )
        else:
            template = runtime.templates.get(template_id, version)
        if template is None or template.status != "published":
            raise ToolError("template not found")
        return template_detail(template)

    @mcp.tool(annotations=READ_ONLY)
    async def get_inbox_health() -> InboxHealth:
        """Operational health of the ingestion inbox: pending/processing/dead-letter counts, age of the oldest
        pending event in seconds, and the number of workers active in the last 5 minutes."""
        return InboxHealth(**await runtime.query(rm.inbox_health))

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        }
    )
    async def request_reanalysis(
        incident_key: IncidentKey,
        reason: Annotated[
            str, Field(min_length=5, max_length=500, description="Why a re-analysis is needed (audited).")
        ],
    ) -> ReanalysisQueued:
        """Queue a fresh analysis of an incident (requires the ANALYST role). The analysis runs asynchronously
        in the BranchOps workers; poll `get_incident_analysis` later. The request is audited."""
        caller = runtime.current_caller()
        key = _check_key(incident_key)
        correlation_id = correlation_id_var.get() or "mcp"

        def enqueue(s: Any) -> str | None:
            detail = rm.issue_detail(s, key, scope=caller.scope)
            if detail is None:
                return None
            canonical = detail["issue"]["incident_key"]
            audit(
                s,
                actor=caller.username,
                action="reanalysis_requested",
                resource_type="issue",
                resource_id=canonical,
                details={
                    "via": "mcp",
                    "team": caller.team_label,
                    "reason": runtime.masker.mask_text(reason),
                },
            )
            return str(canonical)

        canonical = await runtime.query(enqueue)
        if canonical is None:
            raise ToolError(NOT_FOUND)
        event_id = await runtime.call(
            lambda: RequestReanalysis(runtime.db, runtime.settings).execute(
                canonical, requested_by=caller.username, correlation_id=correlation_id
            )
        )
        return ReanalysisQueued(incident_key=canonical, status="queued", event_id=event_id)


TOOL_NAMES: frozenset[str] = (
    frozenset(
        {
            "search_incidents",
            "get_incident",
            "get_incident_analysis",
            "find_similar_incidents",
            "list_recurring_errors",
            "get_quality_overview",
            "get_quality_analytics",
            "list_templates",
            "get_template",
            "get_inbox_health",
            "request_reanalysis",
        }
    )
    | REPORTING_TOOLS
    | OWNERSHIP_TOOLS
)

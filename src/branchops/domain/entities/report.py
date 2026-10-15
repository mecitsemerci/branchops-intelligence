"""Incident drafts submitted by people or in-app agents through the MCP reporting tools."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# Draft attribute -> canonical field name (see ``domain/entities/issue.py``).
DRAFT_FIELD_MAP: dict[str, str] = {
    "title": "summary",
    "description": "description",
    "branch_code": "branch_code",
    "application": "application",
    "application_version": "application_version",
    "category": "category",
    "subcategory": "subcategory",
    "business_process": "business_process",
    "business_impact": "business_impact",
    "error_details": "error_details",
    "occurred_at": "occurrence_time",
    "reproduction_steps": "reproduction_steps",
    "frequency": "frequency",
    "environment": "environment",
    "troubleshooting_steps": "troubleshooting_steps",
    "evidence": "evidence",
}


class IncidentDraft(BaseModel):
    """What the reporter knows. Everything except title and description is optional; the server tells the
    client which template fields are still missing (``check_incident_draft``)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=5, max_length=200, description="One-line summary of the problem.")
    description: str = Field(
        min_length=10, max_length=8000, description="What happened, in the reporter's words."
    )
    branch_code: str | None = Field(default=None, max_length=32, description="Branch code, e.g. '1001'.")
    application: str | None = Field(default=None, max_length=200, description="Affected application.")
    application_version: str | None = Field(default=None, max_length=60)
    category: str | None = Field(
        default=None, max_length=64, description="Category from the glossary, if known (e.g. 'TRANSACTION')."
    )
    subcategory: str | None = Field(default=None, max_length=64)
    business_process: str | None = Field(default=None, max_length=500)
    business_impact: str | None = Field(
        default=None, max_length=2000, description="Effect on customers / branch operations."
    )
    error_details: str | None = Field(
        default=None, max_length=4000, description="Exact error message or code shown on screen."
    )
    occurred_at: datetime | None = Field(default=None, description="When it happened (ISO 8601).")
    reproduction_steps: str | None = Field(default=None, max_length=4000)
    frequency: str | None = Field(default=None, max_length=500, description="Once, every time, sometimes…")
    environment: str | None = Field(default=None, max_length=500, description="Device, browser, network…")
    troubleshooting_steps: str | None = Field(default=None, max_length=4000, description="What was tried.")
    evidence: str | None = Field(
        default=None, max_length=2000, description="References to screenshots, logs, transaction numbers."
    )

    def canonical_values(self) -> dict[str, str]:
        """Present values keyed by canonical field name (dates as ISO 8601)."""
        out: dict[str, str] = {}
        for attr, canonical in DRAFT_FIELD_MAP.items():
            value = getattr(self, attr)
            if value is None or value == "":
                continue
            out[canonical] = value.isoformat() if isinstance(value, datetime) else str(value)
        return out

    def texts(self) -> list[str]:
        return [v for v in self.canonical_values().values() if v]


class FieldGap(BaseModel):
    field: str = Field(description="Canonical field name.")
    label: str
    required: bool
    problems: list[str] = Field(default_factory=list, description="Validation problems of a present value.")
    recommendation: str = Field(description="What to add, from the template.")


class DraftCheck(BaseModel):
    """Deterministic pre-submission check of a draft against the template it would be scored with."""

    ready: bool = Field(description="True when no required field is missing and no value is invalid.")
    template_id: str
    template_version: str
    template_name: str
    missing_required: list[FieldGap] = Field(default_factory=list)
    missing_optional: list[FieldGap] = Field(default_factory=list)
    invalid: list[FieldGap] = Field(default_factory=list)
    security_flags: list[str] = Field(
        default_factory=list, description="Prompt-injection patterns found in the text (review triggers)."
    )

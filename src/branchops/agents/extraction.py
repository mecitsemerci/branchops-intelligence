"""ExtractionAgent: structured operational fields with verified evidence.

Precedence when building the canonical issue (highest first):
1. Jira custom fields mapped in configuration (``JIRA_FIELD`` provenance).
2. Values extracted by the model **and** supported by verifiable evidence (``LLM_EXTRACTION``).
3. Classification output for category/subcategory/application/business process (``LLM_CLASSIFICATION``).

Model values that cannot be verified against the source text are downgraded to ``UNKNOWN``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, model_validator

from branchops.agents.base import StructuredCaller, StructuredResult
from branchops.agents.prompts import GUARDRAILS, EvidenceQuote
from branchops.domain.entities.analysis import Classification
from branchops.domain.entities.issue import FIELD_LABELS_TR, LLM_EXTRACTABLE_FIELDS, CanonicalIssue
from branchops.domain.entities.jira import JiraIssueSnapshot
from branchops.domain.enums import FieldState, ProvenanceSource
from branchops.domain.services.evidence import value_occurs_in_sources, verify_evidence
from branchops.domain.value_objects import EvidenceRef, ExtractedField, Provenance

AGENT_NAME = "extraction"

FIELD_GUIDE: dict[str, str] = {
    "branch_code": "numeric branch code (şube kodu), only if explicitly written",
    "application": "name of the affected application / screen / module",
    "application_version": "application version or build number",
    "business_process": "banking process being performed (e.g. EFT, hesap açılışı, kasa işlemi)",
    "business_impact": "effect on customers / transactions / branch operations",
    "error_details": "error code and/or error message exactly as shown",
    "occurrence_time": "date/time the problem occurred, as written",
    "reproduction_steps": "steps that lead to the problem",
    "frequency": "how often it happens (every time, intermittently, first time …)",
    "environment": "terminal, device model, OS, network or environment details",
    "troubleshooting_steps": "what branch staff already tried and the results",
    "evidence": "screenshots, logs, transaction reference numbers mentioned as attached/available",
}


class ExtractedValueOutput(BaseModel):
    field: Literal[
        "branch_code",
        "application",
        "application_version",
        "business_process",
        "business_impact",
        "error_details",
        "occurrence_time",
        "reproduction_steps",
        "frequency",
        "environment",
        "troubleshooting_steps",
        "evidence",
    ]
    status: Literal["PRESENT", "VAGUE", "UNKNOWN", "NOT_APPLICABLE", "MISSING"] = Field(
        description="PRESENT: clearly stated; VAGUE: mentioned but too unspecific; UNKNOWN: mentioned but "
        "value cannot be determined; NOT_APPLICABLE: the data says it does not apply; MISSING: not mentioned"
    )
    value: str | None = Field(default=None, max_length=1500, description="value in the original language")
    evidence: list[EvidenceQuote] = Field(default_factory=list, max_length=3)


class ExtractionOutput(BaseModel):
    fields: list[ExtractedValueOutput]

    @model_validator(mode="after")
    def _consistency(self, info: ValidationInfo) -> ExtractionOutput:
        """Normalise instead of rejecting: small models often omit quotes, and a repair round-trip for
        one field would discard the whole extraction. A claimed value without any value becomes
        MISSING; a value without quotes is kept here and judged by evidence verification later
        (it survives only if it occurs verbatim in the issue text)."""
        for f in self.fields:
            if f.status in ("PRESENT", "VAGUE") and not (f.value and f.value.strip()):
                f.status = "MISSING"
                f.value = None
        return self


SYSTEM_PROMPT = (
    GUARDRAILS
    + """
Task: extract the operational fields listed in <fields> from the issue data.
- Report every listed field exactly once.
- Use the original wording of the data for values; do not translate, summarise loosely or normalise codes.
- A value must be supported by at least one verbatim evidence quote. If you cannot quote it, the field is
  MISSING (not mentioned) or UNKNOWN (mentioned without a usable value).
- Use VAGUE when the information exists but is too unspecific to act on (e.g. "hata veriyor").
"""
)


class ExtractionAgent:
    def __init__(self, caller: StructuredCaller) -> None:
        self._caller = caller

    def run(
        self, *, issue_block: str, correlation_id: str, deadline: float | None = None
    ) -> StructuredResult[ExtractionOutput]:
        fields_spec = "\n".join(f"- {name}: {FIELD_GUIDE[name]}" for name in LLM_EXTRACTABLE_FIELDS)
        user_prompt = f"<fields>\n{fields_spec}\n</fields>\n\n{issue_block}"
        return self._caller.call(
            agent=AGENT_NAME,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema=ExtractionOutput,
            correlation_id=correlation_id,
            deadline=deadline,
        )


_STATUS_TO_STATE: dict[str, FieldState] = {
    "PRESENT": FieldState.SUFFICIENT,
    "VAGUE": FieldState.INSUFFICIENT,
    "UNKNOWN": FieldState.UNKNOWN,
    "NOT_APPLICABLE": FieldState.NOT_APPLICABLE,
    "MISSING": FieldState.MISSING,
}


def build_canonical_issue(
    snapshot: JiraIssueSnapshot,
    sources: dict[str, str],
    classification: Classification | None,
    extraction: ExtractionOutput | None,
    model_id: str | None,
    *,
    extraction_failed: bool = False,
) -> CanonicalIssue:
    now = datetime.now(UTC)
    fields: dict[str, ExtractedField] = {}

    def jira(path: str, value: str | None) -> ExtractedField:
        if value is None or not value.strip():
            return ExtractedField.missing()
        return ExtractedField(
            value=value,
            state=FieldState.SUFFICIENT,
            evidence=(EvidenceRef(source=path, quote=value[:200], verified=True),),
            provenance=Provenance(source=ProvenanceSource.JIRA_FIELD, source_path=path, recorded_at=now),
        )

    fields["summary"] = jira("summary", snapshot.summary)
    fields["description"] = jira("description", snapshot.description)

    llm_values: dict[str, ExtractedValueOutput] = {}
    if extraction is not None:
        for item in extraction.fields:
            llm_values.setdefault(item.field, item)

    for name in LLM_EXTRACTABLE_FIELDS:
        mapped = snapshot.mapped_fields.get(name)
        if mapped:
            fields[name] = jira(f"field:{name}", mapped)
            continue
        model_item = llm_values.get(name)
        if model_item is None:
            fields[name] = (
                ExtractedField.unknown("extraction step unavailable")
                if extraction_failed
                else ExtractedField.missing()
            )
            continue
        fields[name] = _from_model(name, model_item, sources, model_id, now)

    if classification is not None:
        cls_prov = Provenance(source=ProvenanceSource.LLM_CLASSIFICATION, model_id=model_id, recorded_at=now)
        fields["category"] = ExtractedField(
            value=classification.category,
            state=FieldState.SUFFICIENT,
            evidence=tuple(classification.evidence),
            provenance=cls_prov,
        )
        fields["subcategory"] = (
            ExtractedField(value=classification.subcategory, state=FieldState.SUFFICIENT, provenance=cls_prov)
            if classification.subcategory
            else ExtractedField.missing()
        )
        for name, value in (
            ("application", classification.application),
            ("business_process", classification.business_process),
        ):
            current = fields.get(name)
            if (
                (current is None or not current.has_value)
                and value
                and value_occurs_in_sources(sources, value)
            ):
                fields[name] = ExtractedField(
                    value=value,
                    state=FieldState.SUFFICIENT,
                    provenance=cls_prov,
                    note="taken from classification",
                )
    else:
        fields["category"] = ExtractedField.unknown("classification unavailable")
        fields["subcategory"] = ExtractedField.unknown("classification unavailable")

    return CanonicalIssue(
        issue_key=snapshot.issue_key,
        project=snapshot.project_key,
        status=snapshot.status,
        source_created_at=snapshot.created_at,
        source_updated_at=snapshot.updated_at,
        fields=fields,
    )


def _from_model(
    name: str,
    item: ExtractedValueOutput,
    sources: dict[str, str],
    model_id: str | None,
    now: datetime,
) -> ExtractedField:
    state = _STATUS_TO_STATE[item.status]
    prov = Provenance(
        source=ProvenanceSource.LLM_EXTRACTION, source_path=name, model_id=model_id, recorded_at=now
    )
    if state in (FieldState.MISSING, FieldState.UNKNOWN, FieldState.NOT_APPLICABLE):
        return ExtractedField(state=state, provenance=prov)
    evidence = verify_evidence(sources, [EvidenceRef(source=e.source, quote=e.quote) for e in item.evidence])
    supported = any(e.verified for e in evidence) or (
        item.value is not None and value_occurs_in_sources(sources, item.value)
    )
    if not supported:
        return ExtractedField(
            state=FieldState.UNKNOWN,
            evidence=tuple(evidence),
            provenance=prov,
            note=f"{FIELD_LABELS_TR[name]}: model value could not be verified against the issue text",
        )
    return ExtractedField(value=item.value, state=state, evidence=tuple(evidence), provenance=prov)

"""Domain enumerations shared by every layer."""

from __future__ import annotations

from enum import StrEnum


class FieldState(StrEnum):
    """Information state of a canonical issue field.

    MISSING         The source contains nothing for the field.
    UNKNOWN         The source mentions the topic but the value cannot be determined reliably
                    (or the extracted value could not be verified against the source text).
    NOT_APPLICABLE  The field does not apply to this issue (by template rule or explicit statement).
    INSUFFICIENT    A value is present but too vague / invalid to be operationally useful.
    SUFFICIENT      A usable value is present.
    """

    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INSUFFICIENT = "INSUFFICIENT"
    SUFFICIENT = "SUFFICIENT"


class RubricLevel(StrEnum):
    """Qualitative level assigned by the quality assessment agent for one field.

    ABSENT      No usable information for the field.
    POOR        Information is present but vague, generic or hard to act on.
    ACCEPTABLE  Usable information with gaps (meets the template rubric's "acceptable" description).
    GOOD        Specific, actionable information (meets the rubric's "good" description).
    """

    ABSENT = "ABSENT"
    POOR = "POOR"
    ACCEPTABLE = "ACCEPTABLE"
    GOOD = "GOOD"


class Confidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class IncidentEventType(StrEnum):
    RECORD_CREATED = "RECORD_CREATED"
    RECORD_UPDATED = "RECORD_UPDATED"
    RECORD_DELETED = "RECORD_DELETED"
    COMMENT_CREATED = "COMMENT_CREATED"
    COMMENT_UPDATED = "COMMENT_UPDATED"
    COMMENT_DELETED = "COMMENT_DELETED"
    MANUAL_REANALYSIS = "MANUAL_REANALYSIS"
    UNSUPPORTED = "UNSUPPORTED"

    @classmethod
    def _missing_(cls, value: object) -> IncidentEventType | None:
        # Inbox rows written before the multi-source migration carry the Jira-era values.
        legacy = _LEGACY_EVENT_TYPES.get(str(value))
        return cls(legacy) if legacy else None


_LEGACY_EVENT_TYPES: dict[str, str] = {
    "ISSUE_CREATED": "RECORD_CREATED",
    "ISSUE_UPDATED": "RECORD_UPDATED",
    "ISSUE_DELETED": "RECORD_DELETED",
}


class JiraEventType:
    """Deprecated alias of :class:`IncidentEventType` keeping the Jira-era member names.

    ``JiraEventType.ISSUE_CREATED is IncidentEventType.RECORD_CREATED`` and ``JiraEventType("ISSUE_CREATED")``
    returns ``IncidentEventType.RECORD_CREATED``.
    """

    ISSUE_CREATED = IncidentEventType.RECORD_CREATED
    ISSUE_UPDATED = IncidentEventType.RECORD_UPDATED
    ISSUE_DELETED = IncidentEventType.RECORD_DELETED
    COMMENT_CREATED = IncidentEventType.COMMENT_CREATED
    COMMENT_UPDATED = IncidentEventType.COMMENT_UPDATED
    COMMENT_DELETED = IncidentEventType.COMMENT_DELETED
    MANUAL_REANALYSIS = IncidentEventType.MANUAL_REANALYSIS
    UNSUPPORTED = IncidentEventType.UNSUPPORTED

    def __new__(cls, value: str) -> IncidentEventType:  # type: ignore[misc]
        return IncidentEventType(value)


class InboxStatus(StrEnum):
    """Processing state of an event in the ingestion inbox.

    PENDING      Waiting for a worker (possibly after a retry back-off).
    PROCESSING   Claimed by a worker under a lease.
    COMPLETED    Processed successfully.
    SKIPPED      Not processed (e.g. the record is deleted or unknown).
    DEAD_LETTER  Failed permanently; needs an operator replay.
    """

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    DEAD_LETTER = "DEAD_LETTER"


class AnalysisStatus(StrEnum):
    """Outcome of one analysis run of an incident.

    RUNNING       The analysis workflow is in progress.
    COMPLETED     All steps succeeded; scores are final for this input.
    PARTIAL       Some steps degraded (e.g. a model call failed); scores use the available information.
    NEEDS_REVIEW  Flagged for a human: classification failed or security flags were raised (e.g. suspected
                  prompt injection).
    FAILED        No usable result.
    """

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


class StepStatus(StrEnum):
    """Outcome of one workflow step.

    SUCCEEDED  The step produced its result.
    DEGRADED   The model was unavailable (outage, timeout, access refused); a fallback was used and the
               analysis is eligible for automatic recovery.
    FAILED     The step failed for a reason a retry will not fix (invalid output, rejected request).
    SKIPPED    Nothing to do (e.g. no field to assess).
    """

    SUCCEEDED = "SUCCEEDED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class RelationshipType(StrEnum):
    """Relationship between two incidents found by the similarity step.

    DUPLICATE_CANDIDATE   Very likely the same incident reported again (same branch, symptom and time;
                          deterministic duplicate gates passed).
    RELATED               Similar symptom or component, but a different occurrence or branch.
    UNRELATED             Superficial word overlap only.
    UNVERIFIED_CANDIDATE  Full-text candidate that the model did not verify.
    """

    DUPLICATE_CANDIDATE = "DUPLICATE_CANDIDATE"
    RELATED = "RELATED"
    UNRELATED = "UNRELATED"
    UNVERIFIED_CANDIDATE = "UNVERIFIED_CANDIDATE"


class ProvenanceSource(StrEnum):
    JIRA_FIELD = "JIRA_FIELD"
    LLM_EXTRACTION = "LLM_EXTRACTION"
    LLM_CLASSIFICATION = "LLM_CLASSIFICATION"
    TEMPLATE_RULE = "TEMPLATE_RULE"
    NONE = "NONE"


class Role(StrEnum):
    VIEWER = "VIEWER"
    ANALYST = "ANALYST"
    ADMIN = "ADMIN"


class RecommendationPriority(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

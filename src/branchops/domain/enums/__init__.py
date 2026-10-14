"""Domain enumerations shared by every layer."""

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
    """Qualitative level assigned by the quality assessment agent for one field."""

    ABSENT = "ABSENT"
    POOR = "POOR"
    ACCEPTABLE = "ACCEPTABLE"
    GOOD = "GOOD"


class Confidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class JiraEventType(StrEnum):
    ISSUE_CREATED = "ISSUE_CREATED"
    ISSUE_UPDATED = "ISSUE_UPDATED"
    ISSUE_DELETED = "ISSUE_DELETED"
    COMMENT_CREATED = "COMMENT_CREATED"
    COMMENT_UPDATED = "COMMENT_UPDATED"
    COMMENT_DELETED = "COMMENT_DELETED"
    MANUAL_REANALYSIS = "MANUAL_REANALYSIS"
    UNSUPPORTED = "UNSUPPORTED"


class InboxStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    DEAD_LETTER = "DEAD_LETTER"


class AnalysisStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


class StepStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class RelationshipType(StrEnum):
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

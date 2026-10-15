"""SQLAlchemy ORM schema."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import BYTEA, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


class WebhookInbox(Base):
    __tablename__ = "webhook_inbox"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    raw_event_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False, server_default="jira")
    # The source's own record key (``external_key``); per-incident ordering uses (source, issue_key).
    issue_key: Mapped[str | None] = mapped_column(String(160))
    group_key: Mapped[str | None] = mapped_column(String(120))
    event_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_redacted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(200))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_inbox_claim", "status", "available_at"),
        Index("ix_inbox_source_claim", "source", "status", "available_at"),
        Index("ix_inbox_incident", "source", "issue_key", "received_at"),
    )


class Issue(Base):
    __tablename__ = "issues"

    # "<source>:<external_key>", e.g. "jira:OPS-123", "snow:INC0012345".
    incident_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False, server_default="jira", index=True)
    external_key: Mapped[str] = mapped_column(String(160), nullable=False)
    # Deprecated (one release): the pre-multi-source primary key, equal to ``external_key``.
    issue_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    external_id: Mapped[str | None] = mapped_column(String(64))
    group_key: Mapped[str | None] = mapped_column(String(120), index=True)
    summary: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(80))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Denormalised from the latest analysis, for filtering/analytics.
    category: Mapped[str | None] = mapped_column(String(64), index=True)
    subcategory: Mapped[str | None] = mapped_column(String(64))
    branch_code: Mapped[str | None] = mapped_column(String(32), index=True)
    application: Mapped[str | None] = mapped_column(String(200), index=True)
    error_signature: Mapped[str | None] = mapped_column(String(200), index=True)
    latest_analysis_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    latest_completeness: Mapped[float | None] = mapped_column(Float)
    latest_quality: Mapped[float | None] = mapped_column(Float)
    latest_status: Mapped[str | None] = mapped_column(String(20))
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('turkish', coalesce(summary, '')), 'A') || "
            "setweight(to_tsvector('turkish', coalesce(description, '')), 'B')",
            persisted=True,
        ),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_issues_search", "search_vector", postgresql_using="gin"),
        UniqueConstraint("source", "external_key", name="uq_issues_source_external"),
    )


class Analysis(Base):
    """One execution of the analysis workflow for an issue (doubles as the execution record)."""

    __tablename__ = "analyses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_key: Mapped[str] = mapped_column(
        String(200), ForeignKey("issues.incident_key", ondelete="CASCADE")
    )
    inbox_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    template_id: Mapped[str | None] = mapped_column(String(64))
    template_version: Mapped[str | None] = mapped_column(String(20))
    scoring_policy_version: Mapped[str | None] = mapped_column(String(40))
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    model_id: Mapped[str | None] = mapped_column(String(120))
    category: Mapped[str | None] = mapped_column(String(64))
    subcategory: Mapped[str | None] = mapped_column(String(64))
    completeness_score: Mapped[float | None] = mapped_column(Float)
    quality_score: Mapped[float | None] = mapped_column(Float)
    classification: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    canonical_issue: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    field_scores: Mapped[list[Any] | None] = mapped_column(JSONB)
    assessments: Mapped[list[Any] | None] = mapped_column(JSONB)
    missing_fields: Mapped[list[Any] | None] = mapped_column(JSONB)
    insufficient_fields: Mapped[list[Any] | None] = mapped_column(JSONB)
    unknown_fields: Mapped[list[Any] | None] = mapped_column(JSONB)
    missing_required_fields: Mapped[list[Any] | None] = mapped_column(JSONB)
    recommendations: Mapped[list[Any] | None] = mapped_column(JSONB)
    similar_issues: Mapped[list[Any] | None] = mapped_column(JSONB)
    recurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recurring_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    security_flags: Mapped[list[Any] | None] = mapped_column(JSONB)
    errors: Mapped[list[Any] | None] = mapped_column(JSONB)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Live progress (set while the workflow runs; cleared when the analysis is persisted).
    current_step: Mapped[str | None] = mapped_column(String(60))
    worker_id: Mapped[str | None] = mapped_column(String(200))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_analyses_incident_started", "incident_key", "started_at"),
        Index("ix_analyses_status", "status"),
        Index("ix_analyses_started", "started_at"),
    )


class AgentStep(Base):
    __tablename__ = "agent_steps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analyses.id", ondelete="CASCADE"), index=True
    )
    step: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    model_id: Mapped[str | None] = mapped_column(String(120))
    output_mode: Mapped[str | None] = mapped_column(String(20))
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class IssueComment(Base):
    __tablename__ = "issue_comments"

    incident_key: Mapped[str] = mapped_column(
        String(200), ForeignKey("issues.incident_key", ondelete="CASCADE"), primary_key=True
    )
    comment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    author: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class IssueRelationship(Base):
    __tablename__ = "issue_relationships"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_incident_key: Mapped[str] = mapped_column(
        String(200), ForeignKey("issues.incident_key", ondelete="CASCADE"), nullable=False
    )
    target_incident_key: Mapped[str] = mapped_column(
        String(200), ForeignKey("issues.incident_key", ondelete="CASCADE"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(String(40), nullable=False)
    text_rank: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    signals: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    rationale: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    model_id: Mapped[str | None] = mapped_column(String(120))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("source_incident_key", "target_incident_key", name="uq_relationship_pair"),
        Index("ix_rel_target", "target_incident_key"),
    )


class SourceCheckpoint(Base):
    """Progress cursor of a pull source (``branchops poll``)."""

    __tablename__ = "source_checkpoints"

    source_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    cursor: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TemplateVersion(Base):
    __tablename__ = "template_versions"

    template_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(20), primary_key=True)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False, index=True
    )
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(60), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(200))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class OutboxMixin:
    """Columns shared by the transactional outboxes (claimed with SKIP LOCKED + lease, like the inbox)."""

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    incident_key: Mapped[str | None] = mapped_column(String(200), index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TicketingOutbox(OutboxMixin, Base):
    """Ticket operations for reported incidents (create_ticket, add_comment, add_attachment)."""

    __tablename__ = "ticketing_outbox"
    __table_args__ = (Index("ix_ticketing_outbox_claim", "status", "available_at"),)


class OwnershipTeam(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="app_owner")
    email: Mapped[str | None] = mapped_column(String(320))
    zoom_channel_id: Mapped[str | None] = mapped_column(String(200))
    zoom_webhook_secret_ref: Mapped[str | None] = mapped_column(String(120))
    jira_project_key: Mapped[str | None] = mapped_column(String(30))
    oncall_contact: Mapped[str | None] = mapped_column(String(300))
    working_hours: Mapped[str | None] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_by: Mapped[str | None] = mapped_column(String(200))
    updated_by: Mapped[str | None] = mapped_column(String(200))


class OwnedApplication(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )
    default_screen_owner_team_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="RESTRICT")
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_by: Mapped[str | None] = mapped_column(String(200))
    updated_by: Mapped[str | None] = mapped_column(String(200))


class OwnedScreen(Base):
    __tablename__ = "screens"
    __table_args__ = (UniqueConstraint("application_id", "key", name="uq_screens_application_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    route_pattern: Mapped[str | None] = mapped_column(String(300))
    owner_team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )
    tags: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_by: Mapped[str | None] = mapped_column(String(200))
    updated_by: Mapped[str | None] = mapped_column(String(200))


class RoutingRuleRow(Base):
    __tablename__ = "routing_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    match: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    notify_to_team_ids: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    cc_team_ids: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    channels: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_by: Mapped[str | None] = mapped_column(String(200))
    updated_by: Mapped[str | None] = mapped_column(String(200))


class OwnershipChange(Base):
    """Append-only history of every registry write (who, when, before/after)."""

    __tablename__ = "ownership_changes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False, index=True
    )
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    entity: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    correlation_id: Mapped[str | None] = mapped_column(String(64))


class IncidentDraftToken(Base):
    """Short-lived token from check_incident_draft: attachments can be uploaded before the report."""

    __tablename__ = "incident_drafts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    incident_key: Mapped[str | None] = mapped_column(String(200))
    bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Attachment(Base):
    """Screenshot / log attached to an incident (or to a draft until the report is submitted)."""

    __tablename__ = "attachments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_key: Mapped[str | None] = mapped_column(String(200), index=True)
    draft_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("incident_drafts.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    content_type: Mapped[str] = mapped_column(String(60), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage: Mapped[str] = mapped_column(String(20), nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    # Content for the PostgreSQL store (size-capped by settings.attachments.max_bytes); NULL for S3.
    data: Mapped[bytes | None] = mapped_column(BYTEA)
    blurred_regions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class IncidentAppContext(Base):
    """Masked technical context reported by an in-app agent (routing input; never sent to the LLM)."""

    __tablename__ = "incident_app_context"

    incident_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    security_flags: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

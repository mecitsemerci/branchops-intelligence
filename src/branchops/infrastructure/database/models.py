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
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
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
    issue_key: Mapped[str | None] = mapped_column(String(64))
    project_key: Mapped[str | None] = mapped_column(String(64))
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
        Index("ix_inbox_issue", "issue_key", "received_at"),
    )


class Issue(Base):
    __tablename__ = "issues"

    issue_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    issue_id: Mapped[str | None] = mapped_column(String(64))
    project_key: Mapped[str | None] = mapped_column(String(64), index=True)
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

    __table_args__ = (Index("ix_issues_search", "search_vector", postgresql_using="gin"),)


class Analysis(Base):
    """One execution of the analysis workflow for an issue (doubles as the execution record)."""

    __tablename__ = "analyses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_key: Mapped[str] = mapped_column(String(64), ForeignKey("issues.issue_key", ondelete="CASCADE"))
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

    __table_args__ = (
        Index("ix_analyses_issue_started", "issue_key", "started_at"),
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

    issue_key: Mapped[str] = mapped_column(
        String(64), ForeignKey("issues.issue_key", ondelete="CASCADE"), primary_key=True
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
    source_issue_key: Mapped[str] = mapped_column(
        String(64), ForeignKey("issues.issue_key", ondelete="CASCADE"), nullable=False
    )
    target_issue_key: Mapped[str] = mapped_column(
        String(64), ForeignKey("issues.issue_key", ondelete="CASCADE"), nullable=False
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
        UniqueConstraint("source_issue_key", "target_issue_key", name="uq_relationship_pair"),
        Index("ix_rel_target", "target_issue_key"),
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

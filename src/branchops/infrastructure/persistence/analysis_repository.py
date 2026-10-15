"""Persistence of analysis results, agent steps and issue relationships."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from branchops.domain.entities.analysis import AnalysisResult
from branchops.domain.enums import RelationshipType
from branchops.infrastructure.database.models import AgentStep, Analysis, Issue, IssueRelationship
from branchops.infrastructure.database.session import Database


def _dump(items: list[Any]) -> list[Any]:
    return [i.model_dump(mode="json") if hasattr(i, "model_dump") else i for i in items]


class AnalysisRepository:
    def save(self, session: Session, result: AnalysisResult) -> None:
        analysis_id = uuid.UUID(result.analysis_id)
        scores = result.scores
        cls = result.classification
        prompt_tokens = sum(s.prompt_tokens for s in result.steps)
        completion_tokens = sum(s.completion_tokens for s in result.steps)
        duration_ms = (
            int((result.completed_at - result.started_at).total_seconds() * 1000)
            if result.completed_at
            else None
        )
        values: dict[str, Any] = {
            "id": analysis_id,
            "incident_key": result.incident_key,
            "inbox_event_id": uuid.UUID(result.inbox_event_id) if result.inbox_event_id else None,
            "correlation_id": result.correlation_id,
            "status": result.status.value,
            "input_hash": result.input_hash,
            "template_id": result.template_id,
            "template_version": result.template_version,
            "scoring_policy_version": scores.scoring_policy_version if scores else None,
            "prompt_version": result.prompt_version,
            "model_id": result.model_id,
            "category": cls.category if cls else None,
            "subcategory": cls.subcategory if cls else None,
            "completeness_score": scores.completeness_score if scores else None,
            "quality_score": scores.quality_score if scores else None,
            "classification": cls.model_dump(mode="json") if cls else None,
            "canonical_issue": result.canonical_issue.model_dump(mode="json")
            if result.canonical_issue
            else None,
            "field_scores": _dump(scores.field_scores) if scores else None,
            "assessments": _dump(result.assessments),
            "missing_fields": scores.missing_fields if scores else None,
            "insufficient_fields": scores.insufficient_fields if scores else None,
            "unknown_fields": scores.unknown_fields if scores else None,
            "missing_required_fields": scores.missing_required_fields if scores else None,
            "recommendations": _dump(result.recommendations),
            "similar_issues": _dump(result.similar_issues),
            "recurring": result.recurring,
            "recurring_count": result.recurring_count,
            "security_flags": result.security_flags,
            "errors": result.errors,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "started_at": result.started_at,
            "completed_at": result.completed_at,
            "duration_ms": duration_ms,
            "source_updated_at": result.source_updated_at,
            "current_step": None,
            "heartbeat_at": result.completed_at,
        }
        stmt = insert(Analysis).values(**values)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["id"], set_={k: stmt.excluded[k] for k in values if k != "id"}
            )
        )
        session.execute(delete(AgentStep).where(AgentStep.analysis_id == analysis_id))
        for step in result.steps:
            session.add(
                AgentStep(
                    analysis_id=analysis_id,
                    step=step.step,
                    status=step.status.value,
                    started_at=step.started_at,
                    duration_ms=step.duration_ms,
                    attempts=step.attempts,
                    model_id=step.model_id,
                    output_mode=step.output_mode,
                    prompt_tokens=step.prompt_tokens,
                    completion_tokens=step.completion_tokens,
                    error=step.error,
                )
            )
        self._save_relationships(session, result)
        self._update_issue(session, result)

    def _save_relationships(self, session: Session, result: AnalysisResult) -> None:
        analysis_id = uuid.UUID(result.analysis_id)
        existing_keys = set(
            session.execute(
                select(Issue.incident_key).where(
                    Issue.incident_key.in_([s.incident_key for s in result.similar_issues])
                )
            ).scalars()
        )
        for s in result.similar_issues:
            if s.incident_key not in existing_keys:
                continue
            if s.relationship is RelationshipType.UNRELATED:
                session.execute(
                    delete(IssueRelationship).where(
                        IssueRelationship.source_incident_key == result.incident_key,
                        IssueRelationship.target_incident_key == s.incident_key,
                    )
                )
                continue
            values = {
                "source_incident_key": result.incident_key,
                "target_incident_key": s.incident_key,
                "relationship_type": s.relationship.value,
                "text_rank": s.text_rank,
                "signals": s.deterministic_signals,
                "rationale": s.rationale,
                "evidence": [e.model_dump(mode="json") for e in s.evidence],
                "analysis_id": analysis_id,
                "model_id": result.model_id,
            }
            stmt = insert(IssueRelationship).values(**values)
            session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_relationship_pair",
                    set_={
                        k: stmt.excluded[k]
                        for k in values
                        if k not in ("source_incident_key", "target_incident_key")
                    },
                )
            )

    def _update_issue(self, session: Session, result: AnalysisResult) -> None:
        canonical = result.canonical_issue
        latest_started = session.execute(
            select(Analysis.started_at)
            .join(Issue, Issue.latest_analysis_id == Analysis.id)
            .where(Issue.incident_key == result.incident_key)
        ).scalar_one_or_none()
        if latest_started is not None and latest_started > result.started_at:
            return  # a newer analysis already owns the denormalised columns
        scores = result.scores
        session.execute(
            update(Issue)
            .where(Issue.incident_key == result.incident_key)
            .values(
                latest_analysis_id=uuid.UUID(result.analysis_id),
                latest_completeness=scores.completeness_score if scores else None,
                latest_quality=scores.quality_score if scores else None,
                latest_status=result.status.value,
                category=result.classification.category if result.classification else None,
                subcategory=result.classification.subcategory if result.classification else None,
                branch_code=canonical.value("branch_code") if canonical else None,
                application=(canonical.value("application") or "")[:200] or None if canonical else None,
                error_signature=result.error_signature,
            )
        )


class DatabaseAnalysisSink:
    """``AnalysisSink`` that writes each result in its own transaction."""

    def __init__(self, db: Database, repo: AnalysisRepository | None = None) -> None:
        self._db = db
        self._repo = repo or AnalysisRepository()

    def save(self, result: AnalysisResult) -> None:
        with self._db.session() as session:
            self._repo.save(session, result)

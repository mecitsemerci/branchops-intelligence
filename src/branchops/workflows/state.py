"""LangGraph state for the issue analysis workflow.

All values are JSON-compatible (Pydantic models are stored via ``model_dump(mode="json")``) so that the
state can be checkpointed in PostgreSQL and resumed by another worker after a crash or retry.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class AnalysisState(TypedDict, total=False):
    # Inputs
    analysis_id: str
    incident_key: str
    correlation_id: str
    inbox_event_id: str | None
    input_hash: str
    allow_degraded: bool
    worker_id: str | None
    started_at: str
    snapshot: dict[str, Any]

    # Produced by nodes
    sources: dict[str, str]
    issue_block: str
    security_flags: list[str]
    classification: dict[str, Any] | None
    template_id: str
    template_version: str
    extraction: dict[str, Any] | None
    extraction_failed: bool
    canonical: dict[str, Any]
    assessments: dict[str, dict[str, Any]] | None
    scores: dict[str, Any]
    validated_assessments: list[dict[str, Any]]
    similar: list[dict[str, Any]]
    recurring: bool
    recurring_count: int
    investigation: dict[str, Any] | None
    recommendations: list[dict[str, Any]]
    model_id: str | None
    status: str

    # Accumulated across nodes
    steps: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]
    degraded: Annotated[list[str], operator.add]

"""MCP resources: published templates, the scoring policy and a generated glossary (no incident data).

The glossary is derived from the domain enums' docstrings, the scoring modules and the published templates,
so it cannot drift from the code.
"""

from __future__ import annotations

import inspect
import json
import re
from enum import StrEnum
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError

from branchops.domain.entities.issue import CANONICAL_FIELDS
from branchops.domain.enums import (
    AnalysisStatus,
    FieldState,
    RecommendationPriority,
    RelationshipType,
    RubricLevel,
)
from branchops.mcp.runtime import McpRuntime
from branchops.mcp.tools import template_detail
from branchops.scoring import completeness, quality

_MEMBER_RE = re.compile(r"^([A-Z_]+)\s{2,}(\S.*)$")


def enum_meanings(enum: type[StrEnum]) -> dict[str, str]:
    """``NAME  description`` lines of an enum docstring (indented continuation lines are joined).

    The docstring is normalised with ``inspect.cleandoc`` first: Python 3.13 strips the common indentation at
    compile time, 3.12 does not.
    """
    meanings: dict[str, str] = {}
    current: str | None = None
    for line in inspect.cleandoc(enum.__doc__ or "").splitlines():
        if m := _MEMBER_RE.match(line):
            current = m.group(1)
            meanings[current] = m.group(2).strip()
        elif current and line.strip() and line[0].isspace():
            meanings[current] += " " + line.strip()
        else:
            current = None
    return {member.value: meanings.get(member.value, "") for member in enum}


def _first_paragraph(obj: Any) -> str:
    return (inspect.getdoc(obj) or "").split("\n\n")[0].replace("\n", " ")


def glossary(runtime: McpRuntime) -> dict[str, Any]:
    templates = runtime.templates.latest_published()
    labels: dict[str, set[str]] = {}
    for t in templates:
        for f in t.fields:
            labels.setdefault(f.name, set()).add(f.label)
    policy = runtime.settings.scoring
    return {
        "categories": [
            {
                "category": category,
                "subcategories": subcategories,
                "description": runtime.templates.category_descriptions().get(category, ""),
            }
            for category, subcategories in runtime.templates.taxonomy().items()
        ],
        "canonical_fields": [
            {"name": name, "labels": sorted(labels.get(name, set()))} for name in CANONICAL_FIELDS
        ],
        "field_states": enum_meanings(FieldState),
        "rubric_levels": {
            level: {"meaning": meaning, "quality_credit": policy.level_credit(RubricLevel(level))}
            for level, meaning in enum_meanings(RubricLevel).items()
        },
        "scores": {
            "completeness": {
                "range": "0-100",
                "definition": _first_paragraph(completeness),
                "credits": {
                    "SUFFICIENT": policy.sufficient_completeness_credit,
                    "INSUFFICIENT": policy.insufficient_completeness_credit,
                    "UNKNOWN": f"{policy.unknown_completeness_handling.value}"
                    f" ({policy.unknown_completeness_credit})",
                    "NOT_APPLICABLE": "excluded",
                },
            },
            "quality": {
                "range": "0-100",
                "definition": _first_paragraph(quality),
                "credits": {k.value: v for k, v in policy.level_credits.items()},
            },
            "low_quality_threshold": policy.low_quality_threshold,
            "scoring_policy_version": policy.version,
            "computed_by": "Python (deterministic), from the template weights and validated rubric levels",
        },
        "analysis_statuses": enum_meanings(AnalysisStatus),
        "relationship_types": {
            **enum_meanings(RelationshipType),
            "RECURRING": "find_similar_incidents only: same normalised error signature.",
            "TEXT_MATCH": "find_similar_incidents only: PostgreSQL full-text match of free text.",
        },
        "recommendation_priorities": [p.value for p in RecommendationPriority],
        "sources": [{"id": s.id, "type": s.type} for s in runtime.settings.enabled_sources()],
        "incident_key": "'<source>:<external key>', e.g. 'jira:OPS-123'; a bare Jira key means 'jira:<key>'.",
    }


def register_resources(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.resource(
        "branchops://templates/{template_id}/{version}",
        name="template",
        description="A published incident template (fields, weights, rules, rubrics) as JSON.",
        mime_type="application/json",
    )
    def template_resource(template_id: str, version: str) -> str:
        template = runtime.templates.get(template_id, version)
        if template is None or template.status != "published":
            raise ResourceError("template not found")
        return template_detail(template).model_dump_json(indent=2)

    @mcp.resource(
        "branchops://scoring/policy",
        name="scoring-policy",
        description="The current versioned scoring policy (credits per state/level, thresholds) as JSON.",
        mime_type="application/json",
    )
    def scoring_policy() -> str:
        return runtime.settings.scoring.model_dump_json(indent=2)

    @mcp.resource(
        "branchops://glossary",
        name="glossary",
        description="Categories, canonical field names, score definitions and status meanings, generated "
        "from the code and the published templates.",
        mime_type="application/json",
    )
    def glossary_resource() -> str:
        return json.dumps(glossary(runtime), ensure_ascii=False, indent=2)

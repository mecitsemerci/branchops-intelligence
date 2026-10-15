"""MCP resources (templates, scoring policy, generated glossary) and prompts."""

from __future__ import annotations

import asyncio
from typing import Any

from fastmcp import Client

from branchops.mcp.server import build_server
from tests.helpers import make_settings
from tests.unit.test_mcp import fake_container


class TestResourcesAndPrompts:
    def test_glossary_is_generated_from_code_and_templates(self) -> None:
        from branchops.domain.entities.issue import CANONICAL_FIELDS
        from branchops.domain.enums import AnalysisStatus, FieldState, RelationshipType
        from branchops.mcp.resources import enum_meanings

        # Every member of the documented enums has a non-empty meaning (docstrings stay in sync).
        for enum in (FieldState, AnalysisStatus, RelationshipType):
            meanings = enum_meanings(enum)
            assert set(meanings) == {m.value for m in enum}
            assert all(meanings.values()), enum
        assert "verified" in enum_meanings(FieldState)["UNKNOWN"]  # continuation lines are joined

        settings = make_settings()
        container, _ = fake_container(settings)
        server, _ = build_server(settings, container, stdio=True)

        async def scenario() -> tuple[dict[str, Any], set[str], set[str], str]:
            import json

            async with Client(server) as client:
                glossary = json.loads((await client.read_resource("branchops://glossary"))[0].text)  # type: ignore[union-attr]
                uris = {str(r.uri) for r in await client.list_resources()}
                prompts = {p.name for p in await client.list_prompts()}
                prompt = await client.get_prompt(
                    "missing-information-checklist", {"incident_key": "jira:OPS-1"}
                )
                return glossary, uris, prompts, prompt.messages[0].content.text  # type: ignore[union-attr]

        glossary, uris, prompts, text = asyncio.run(scenario())
        assert [f["name"] for f in glossary["canonical_fields"]] == list(CANONICAL_FIELDS)
        assert {c["category"] for c in glossary["categories"]} >= {"TRANSACTION", "APPLICATION"}
        assert glossary["scores"]["scoring_policy_version"] == settings.scoring.version
        assert uris == {"branchops://glossary", "branchops://scoring/policy"}
        assert prompts == {
            "weekly-branch-report",
            "investigate-recurring-error",
            "missing-information-checklist",
            "report-from-app",
        }
        assert "get_incident_analysis" in text and "jira:OPS-1" in text

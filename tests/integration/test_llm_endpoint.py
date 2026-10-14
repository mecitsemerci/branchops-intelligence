"""Optional checks against the real internal inference gateway (never run in CI).

    BRANCHOPS_LLM_INTEGRATION=1 BRANCHOPS_CONFIG=config/branchops.yaml LLM_BASE_URL=... LLM_API_KEY=... \
        pytest -m llm_integration tests/integration/test_llm_endpoint.py
"""

from __future__ import annotations

import os

import pytest

from branchops.agents.base import StructuredCaller
from branchops.agents.classification import ClassificationAgent
from branchops.agents.extraction import ExtractionAgent, build_canonical_issue
from branchops.agents.prompts import issue_data_block, issue_sources
from branchops.config.settings import load_settings
from branchops.domain.enums import FieldState
from branchops.domain.services.snapshot_merge import merge_event
from branchops.infrastructure.jira.adapter import JiraWebhookAdapter
from branchops.infrastructure.llm.openai_compatible import OpenAICompatibleModelProvider
from branchops.templates.registry import TemplateRegistry
from tests.helpers import TEMPLATES, load_sample

pytestmark = [
    pytest.mark.llm_integration,
    pytest.mark.skipif(
        os.environ.get("BRANCHOPS_LLM_INTEGRATION") != "1", reason="real LLM endpoint not enabled"
    ),
]


@pytest.fixture(scope="module")
def provider() -> OpenAICompatibleModelProvider:
    settings = load_settings()
    return OpenAICompatibleModelProvider(settings.llm, settings.network)


def test_health(provider: OpenAICompatibleModelProvider) -> None:
    health = provider.health()
    assert health.healthy, health.detail


def test_classification_and_extraction_on_turkish_sample(provider: OpenAICompatibleModelProvider) -> None:
    snap = merge_event(
        None,
        JiraWebhookAdapter({"branch_code": "customfield_10101"}).normalize(load_sample("complete_incident")),
    ).snapshot
    caller = StructuredCaller(provider, max_repair_attempts=1)
    taxonomy = TemplateRegistry.from_directory(TEMPLATES).taxonomy()
    block, sources = issue_data_block(snap, 24_000), issue_sources(snap)
    cls, _ = ClassificationAgent(caller).run(
        issue_block=block, sources=sources, taxonomy=taxonomy, correlation_id="llm-it"
    )
    assert cls.category in ("TRANSACTION", "APPLICATION")
    extraction = ExtractionAgent(caller).run(issue_block=block, correlation_id="llm-it")
    canonical = build_canonical_issue(snap, sources, cls, extraction.value, provider.model_id)
    assert canonical.field("error_details").state is FieldState.SUFFICIENT
    assert "EFT-4012" in (canonical.value("error_details") or "")


def test_prompt_injection_does_not_change_classification(provider: OpenAICompatibleModelProvider) -> None:
    snap = merge_event(None, JiraWebhookAdapter().normalize(load_sample("prompt_injection"))).snapshot
    taxonomy = TemplateRegistry.from_directory(TEMPLATES).taxonomy()
    cls, _ = ClassificationAgent(StructuredCaller(provider)).run(
        issue_block=issue_data_block(snap, 24_000),
        sources=issue_sources(snap),
        taxonomy=taxonomy,
        correlation_id="llm-it",
    )
    assert cls.category != "NETWORK"

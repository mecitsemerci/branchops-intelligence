"""Source adapters (Jira, ServiceNow, CSV/XLSX/JSONL import) and the registry factory."""

from __future__ import annotations

from branchops.application.interfaces.source_adapter import SourceAdapter, SourceRegistry
from branchops.config.settings import Settings, SourceSettings


def build_adapter(src: SourceSettings) -> SourceAdapter:
    if src.type == "jira_webhook":
        from branchops.infrastructure.sources.jira.adapter import JiraWebhookAdapter

        return JiraWebhookAdapter(src.field_mapping, source_id=src.id, auth=src.auth)
    if src.type == "servicenow_webhook":
        from branchops.infrastructure.sources.servicenow.adapter import ServiceNowWebhookAdapter

        return ServiceNowWebhookAdapter(src)
    if src.type == "csv_import":
        from branchops.infrastructure.sources.csv_import.adapter import CsvImportAdapter

        return CsvImportAdapter(src)
    raise ValueError(f"unsupported source type {src.type!r}")  # pragma: no cover - Literal-validated


def build_source_registry(settings: Settings) -> SourceRegistry:
    """Registry of the *enabled* sources; disabled or unknown ids resolve to ``UnknownSourceError``."""
    return SourceRegistry(build_adapter(src) for src in settings.enabled_sources())

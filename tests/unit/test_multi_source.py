"""Source-neutral contracts: incident keys, legacy aliases, configuration translation, registry, poller."""

from __future__ import annotations

import warnings
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from branchops.application.commands.ingest_webhook import IngestIncidentEvent
from branchops.application.interfaces.source_adapter import (
    SourcePayloadError,
    SourceRegistry,
    UnknownSourceError,
    decode_event_envelope,
    event_envelope,
)
from branchops.config.settings import Settings
from branchops.domain.entities.incident import (
    IncidentComment,
    IncidentEvent,
    IncidentSnapshot,
    is_valid_incident_key,
    parse_incident_key,
)
from branchops.domain.enums import IncidentEventType
from branchops.domain.services.snapshot_merge import merge_event
from branchops.infrastructure.sources import build_source_registry
from branchops.infrastructure.sources.jira.adapter import JiraWebhookAdapter
from branchops.workers.poller import next_cursor
from tests.helpers import load_sample


def ts(hour: int) -> datetime:
    return datetime(2026, 10, 14, hour, tzinfo=UTC)


class TestIncidentKey:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("jira:OPS-123", ("jira", "OPS-123")),
            ("snow:INC0012345", ("snow", "INC0012345")),
            ("csv:2026-10-import/row-17", ("csv", "2026-10-import/row-17")),
            ("OPS-123", ("jira", "OPS-123")),  # legacy bare Jira key
        ],
    )
    def test_parse(self, value: str, expected: tuple[str, str]) -> None:
        assert parse_incident_key(value) == expected
        assert is_valid_incident_key(value)

    @pytest.mark.parametrize("value", ["not-a-key", "jira:", "Jira:OPS-1", "snow:INC 1", "csv:<x>", ""])
    def test_invalid(self, value: str) -> None:
        assert not is_valid_incident_key(value)

    def test_snapshot_and_event_identity(self) -> None:
        snap = IncidentSnapshot(source="snow", external_key="INC0012345")
        assert snap.incident_key == "snow:INC0012345"
        event = IncidentEvent(
            source="snow",
            event_type=IncidentEventType.RECORD_CREATED,
            raw_event_name="x",
            external_key="INC0012345",
        )
        assert event.incident_key == snap.incident_key


class TestLegacyAliases:
    def test_jira_names_still_import_without_warnings(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            from branchops.domain.entities.jira import JiraComment, JiraIssueSnapshot, NormalizedJiraEvent
            from branchops.domain.enums import JiraEventType
            from branchops.infrastructure.jira.adapter import JiraWebhookAdapter as LegacyAdapter

        assert NormalizedJiraEvent is IncidentEvent
        assert JiraIssueSnapshot is IncidentSnapshot
        assert JiraComment is IncidentComment
        assert LegacyAdapter is JiraWebhookAdapter
        assert JiraEventType.ISSUE_CREATED is IncidentEventType.RECORD_CREATED
        assert JiraEventType.ISSUE_UPDATED is IncidentEventType.RECORD_UPDATED
        assert JiraEventType.ISSUE_DELETED is IncidentEventType.RECORD_DELETED
        assert JiraEventType.COMMENT_DELETED is IncidentEventType.COMMENT_DELETED
        assert JiraEventType("ISSUE_CREATED") is IncidentEventType.RECORD_CREATED

    def test_inbox_rows_with_old_event_type_values(self) -> None:
        assert IncidentEventType("ISSUE_UPDATED") is IncidentEventType.RECORD_UPDATED
        with pytest.raises(ValueError):
            IncidentEventType("SPRINT_STARTED")

    def test_old_snapshot_json_loads(self) -> None:
        stored: dict[str, Any] = {
            "issue_key": "BOPS-1",
            "issue_id": "10001",
            "project_key": "BOPS",
            "summary": "Özet",
            "field_versions": {
                "project_key": "2026-10-14T09:00:00+00:00",
                "summary": "2026-10-14T09:00:00+00:00",
            },
        }
        snap = IncidentSnapshot.model_validate(stored)
        assert (snap.source, snap.external_key, snap.external_id, snap.group_key) == (
            "jira",
            "BOPS-1",
            "10001",
            "BOPS",
        )
        assert set(snap.field_versions) == {"group_key", "summary"}
        assert snap.issue_key == "BOPS-1" and snap.project_key == "BOPS"  # deprecated accessors

    def test_old_constructor_keywords(self) -> None:
        event = IncidentEvent(
            event_type=IncidentEventType.RECORD_CREATED,
            raw_event_name="x",
            issue_key="BOPS-1",
            project_key="BOPS",
            issue_fields={"summary": "Özet", "issue_id": "1"},
        )
        assert event.incident_key == "jira:BOPS-1"
        assert event.fields == {"summary": "Özet", "external_id": "1"}
        assert event.issue_fields is event.fields


class TestJiraAdapterIsSourceNeutral:
    def test_event_carries_source_and_group(self) -> None:
        event = JiraWebhookAdapter(source_id="jira-cloud").normalize(load_sample("complete_incident"))
        assert event.source == "jira-cloud"
        assert event.incident_key == "jira-cloud:BOPS-101"
        assert event.group_key == "BOPS"
        assert event.event_type is IncidentEventType.RECORD_CREATED

    def test_idempotency_key_prefers_delivery_id(self) -> None:
        adapter = JiraWebhookAdapter()
        payload = load_sample("complete_incident")
        assert adapter.idempotency_key(payload, {"X-Atlassian-Webhook-Identifier": "d1"}) == "delivery:d1"
        digest = adapter.idempotency_key(payload, {})
        assert digest.startswith("sha256:") and digest == adapter.idempotency_key(dict(payload), {})


class TestMerge:
    def test_events_of_another_incident_are_rejected(self) -> None:
        snap = IncidentSnapshot(source="snow", external_key="INC1")
        event = IncidentEvent(
            source="jira",
            event_type=IncidentEventType.RECORD_UPDATED,
            raw_event_name="x",
            external_key="INC1",
        )
        with pytest.raises(ValueError, match="cannot be merged"):
            merge_event(snap, event)

    def test_envelope_round_trip_merges_like_the_original(self) -> None:
        event = IncidentEvent(
            source="csv",
            event_type=IncidentEventType.RECORD_CREATED,
            raw_event_name="csv_import",
            external_key="IMP-1",
            event_timestamp=ts(9),
            fields={
                "summary": "Özet 10000000146",
                "created_at": ts(8),
                "comments": [IncidentComment(comment_id="c1", body="not")],
            },
            mapped_fields={"branch_code": "1234"},
        )
        decoded = decode_event_envelope("csv", event_envelope(event))
        assert decoded == event
        assert merge_event(None, decoded).snapshot == merge_event(None, event).snapshot
        masked = decode_event_envelope("csv", event_envelope(event), transform=lambda t: t.replace("1", "*"))
        assert masked.fields["summary"] == "Özet *0000000*46"
        assert masked.mapped_fields["branch_code"] == "*234"
        with pytest.raises(SourcePayloadError):
            decode_event_envelope("snow", event_envelope(event))


def _settings(**data: Any) -> Settings:
    return Settings.model_validate({"app": {"environment": "test"}, **data})


class TestSourceConfiguration:
    def test_no_configuration_means_a_single_jira_source(self) -> None:
        (src,) = _settings().sources
        assert (src.id, src.type, src.enabled) == ("jira", "jira_webhook", True)
        assert src.auth is not None and src.auth.secrets_env == "JIRA_WEBHOOK_SECRETS"

    def test_legacy_jira_and_webhook_sections_are_translated(self) -> None:
        settings = _settings(
            jira={"field_mapping": {"branch_code": "customfield_1"}, "allowed_projects": ["OPS"]},
            webhook={
                "auth_mode": "token",
                "secrets_env": "MY_SECRETS",
                "token_header": "X-Token",
                "allow_query_token": True,
                "allowed_source_cidrs": ["10.0.0.0/8"],
                "max_body_bytes": 1000,
            },
        )
        (src,) = settings.sources
        assert src.field_mapping == {"branch_code": "customfield_1"}
        assert src.allowed_groups == ["OPS"]
        assert src.auth is not None
        assert (src.auth.mode, src.auth.secrets_env, src.auth.token_header) == (
            "token",
            "MY_SECRETS",
            "X-Token",
        )
        assert src.auth.allow_query_token and src.auth.allowed_source_cidrs == ["10.0.0.0/8"]
        assert settings.webhook.max_body_bytes == 1000

    def test_explicit_sources_win_over_legacy_sections(self) -> None:
        settings = _settings(
            jira={"field_mapping": {"branch_code": "ignored"}},
            sources=[
                {"id": "jira", "type": "jira_webhook", "field_mapping": {"branch_code": "customfield_9"}},
                {
                    "id": "snow",
                    "type": "servicenow_webhook",
                    "auth": {"mode": "token", "secrets_env": "SNOW"},
                },
                {"id": "csv", "type": "csv_import", "poll_interval_seconds": 60},
                {"id": "old", "type": "csv_import", "enabled": False},
            ],
        )
        assert [s.id for s in settings.enabled_sources()] == ["jira", "snow", "csv"]
        jira = settings.source("jira")
        assert jira is not None and jira.field_mapping == {"branch_code": "customfield_9"}
        assert jira.auth is not None  # push sources always get auth settings
        csv = settings.source("csv")
        assert csv is not None and csv.auth is None and not csv.is_push

    def test_invalid_source_definitions(self) -> None:
        with pytest.raises(ValidationError, match="unique"):
            _settings(sources=[{"id": "a", "type": "csv_import"}, {"id": "a", "type": "csv_import"}])
        with pytest.raises(ValidationError):
            _settings(sources=[{"id": "Bad Id", "type": "csv_import"}])
        with pytest.raises(ValidationError):
            _settings(sources=[{"id": "x", "type": "kafka"}])

    def test_production_requires_secrets_for_every_enabled_push_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prod: dict[str, Any] = {
            "app": {"environment": "production"},
            "security": {"auth_mode": "oauth_proxy"},
            "llm": {"provider": "openai-compatible", "base_url": "https://gw.internal/v1"},
            "sources": [
                {"id": "jira", "type": "jira_webhook"},
                {
                    "id": "snow",
                    "type": "servicenow_webhook",
                    "auth": {"mode": "token", "secrets_env": "SNOW_S"},
                },
                {
                    "id": "off",
                    "type": "servicenow_webhook",
                    "enabled": False,
                    "auth": {"secrets_env": "UNSET"},
                },
                {"id": "csv", "type": "csv_import"},
            ],
        }
        monkeypatch.setenv("JIRA_WEBHOOK_SECRETS", "s1")
        monkeypatch.delenv("SNOW_S", raising=False)
        with pytest.raises(ValidationError, match=r"'snow'.*SNOW_S"):
            Settings.model_validate(prod)
        monkeypatch.setenv("SNOW_S", "s2")
        assert len(Settings.model_validate(prod).enabled_sources()) == 3


class TestRegistry:
    def test_resolution(self) -> None:
        registry = build_source_registry(
            _settings(
                sources=[
                    {"id": "jira", "type": "jira_webhook"},
                    {"id": "csv", "type": "csv_import"},
                    {"id": "off", "type": "jira_webhook", "enabled": False},
                ]
            )
        )
        assert registry.ids() == ["jira", "csv"]
        assert registry.get_push("jira").source_id == "jira"
        assert registry.get_pull("csv").source_id == "csv"
        for bad in ("off", "nope"):
            with pytest.raises(UnknownSourceError):
                registry.get(bad)
        with pytest.raises(UnknownSourceError):
            registry.get_push("csv")
        with pytest.raises(UnknownSourceError):
            registry.get_pull("jira")
        with pytest.raises(ValueError, match="twice"):
            registry.register(JiraWebhookAdapter())

    def test_ingest_rejects_unknown_and_pull_sources_before_touching_the_database(self) -> None:
        settings = _settings(sources=[{"id": "csv", "type": "csv_import"}])
        command = IngestIncidentEvent(None, settings, build_source_registry(settings))  # type: ignore[arg-type]
        with pytest.raises(UnknownSourceError):
            command.execute("jira", {}, correlation_id="c")
        with pytest.raises(UnknownSourceError):
            command.execute("csv", {"a": 1}, correlation_id="c")
        with pytest.raises(ValueError, match="exactly one"):
            command.execute("csv", correlation_id="c")

    def test_empty_registry(self) -> None:
        registry = SourceRegistry()
        assert registry.push_sources() == [] and registry.pull_sources() == []


def _event(at: datetime | None, updated: datetime | None = None) -> IncidentEvent:
    return IncidentEvent(
        source="csv",
        event_type=IncidentEventType.RECORD_CREATED,
        raw_event_name="csv_import",
        external_key="k",
        event_timestamp=at,
        source_updated_at=updated,
    )


def test_poller_cursor_is_the_newest_event_timestamp() -> None:
    assert next_cursor([], None) is None
    assert next_cursor([], ts(5)) == ts(5)
    # event_timestamp (e.g. file mtime) wins over the row's own update time
    assert next_cursor([_event(ts(9), updated=ts(12)), _event(ts(10))], ts(5)) == ts(10)
    assert next_cursor([_event(None, updated=ts(7))], None) == ts(7)
    assert next_cursor([_event(ts(3))], ts(5)) == ts(5)  # never moves backwards

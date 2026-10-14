from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from branchops.config.settings import NetworkSettings, SecuritySettings, Settings, WebhookSettings
from branchops.domain.enums import JiraEventType, Role
from branchops.infrastructure.jira.adapter import (
    JiraPayloadError,
    JiraWebhookAdapter,
    adf_to_text,
    custom_field_text,
    parse_jira_datetime,
)
from branchops.infrastructure.security.auth import AuthenticationError, resolve_principal
from branchops.infrastructure.security.network_guard import ExternalEndpointError, assert_internal_url
from branchops.infrastructure.security.webhook_auth import (
    WebhookAuthError,
    authenticate_webhook,
    sign_body,
)
from tests.helpers import load_sample

MAPPING = {"branch_code": "customfield_10101", "application": "customfield_10102"}


class TestAdapter:
    def test_complete_issue(self) -> None:
        ev = JiraWebhookAdapter(MAPPING).normalize(load_sample("complete_incident"))
        assert ev.event_type is JiraEventType.ISSUE_CREATED
        assert ev.issue_key == "BOPS-101"
        assert ev.project_key == "BOPS"
        assert ev.mapped_fields == {"branch_code": "1234", "application": "Gişe Ekranı"}
        assert ev.issue_fields["status"] == "Açık"
        assert ev.issue_fields["components"] == ["Şube Uygulamaları"]
        assert ev.source_updated_at is not None
        assert ev.source_updated_at.utcoffset() is not None

    def test_adf_description(self) -> None:
        ev = JiraWebhookAdapter(MAPPING).normalize(load_sample("cloud_adf_issue"))
        desc = ev.issue_fields["description"]
        assert "AUTH-403" in desc
        assert "- Şifre sıfırlama denendi" in desc
        assert ev.mapped_fields == {"branch_code": ""}  # present but empty

    def test_comment_event(self) -> None:
        ev = JiraWebhookAdapter().normalize(load_sample("comment_created"))
        assert ev.event_type is JiraEventType.COMMENT_CREATED
        assert ev.comment is not None
        assert ev.comment.comment_id == "50001"

    def test_legacy_comment_inside_issue_updated(self) -> None:
        payload: dict[str, Any] = load_sample("comment_created")
        payload["webhookEvent"] = "jira:issue_updated"
        payload["issue_event_type_name"] = "issue_comment_edited"
        assert JiraWebhookAdapter().normalize(payload).event_type is JiraEventType.COMMENT_UPDATED

    def test_partial_payload_only_present_fields(self) -> None:
        ev = JiraWebhookAdapter(MAPPING).normalize(load_sample("partial_update"))
        assert set(ev.issue_fields) == {"issue_id", "project_key", "description"}
        assert "description" in ev.changed_fields
        assert ev.mapped_fields == {}

    def test_changelog_fallback_when_field_absent(self) -> None:
        payload = {
            "webhookEvent": "jira:issue_updated",
            "issue": {"key": "BOPS-9", "fields": {"updated": "2026-10-14T10:00:00.000+0300"}},
            "changelog": {
                "items": [
                    {"field": "summary", "toString": "Yeni özet"},
                    {"fieldId": "customfield_10101", "toString": "4321"},
                ]
            },
        }
        ev = JiraWebhookAdapter(MAPPING).normalize(payload)
        assert ev.issue_fields["summary"] == "Yeni özet"
        assert ev.mapped_fields == {"branch_code": "4321"}

    def test_masking_transform_applied(self) -> None:
        ev = JiraWebhookAdapter().normalize(
            load_sample("pii_incident"), transform=lambda s: s.replace("TCKN", "X")
        )
        assert "TCKN" not in ev.issue_fields["description"]

    def test_unsupported_and_invalid(self) -> None:
        assert JiraWebhookAdapter().normalize({"webhookEvent": "project_created"}).event_type is (
            JiraEventType.UNSUPPORTED
        )
        with pytest.raises(JiraPayloadError):
            JiraWebhookAdapter().normalize(
                {"webhookEvent": "jira:issue_created", "issue": {"key": "bad key"}}
            )
        with pytest.raises(JiraPayloadError):
            JiraWebhookAdapter().normalize({"webhookEvent": "jira:issue_created", "issue": {}})
        with pytest.raises(JiraPayloadError):
            JiraWebhookAdapter().normalize({"webhookEvent": "comment_created", "issue": {"key": "A-1"}})

    def test_helpers(self) -> None:
        assert parse_jira_datetime("2026-10-14T09:12:33.000+0300") is not None
        assert parse_jira_datetime(1791972753000) is not None
        assert parse_jira_datetime("garbage") is None
        assert custom_field_text({"value": "A", "child": {"value": "B"}}) == "A / B"
        assert custom_field_text([{"name": "x"}, {"name": "y"}]) == "x, y"
        assert (
            adf_to_text(
                {
                    "type": "doc",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "a"}]}],
                }
            )
            == "a\n"
        )


class TestWebhookAuth:
    body = b'{"webhookEvent":"jira:issue_created"}'

    def _auth(self, headers: dict[str, str], query: dict[str, str] | None = None, **settings: Any) -> str:
        return authenticate_webhook(
            body=self.body,
            headers=headers,
            query=query or {},
            client_host="10.1.2.3",
            settings=WebhookSettings(**settings),
            secrets=["old-secret", "new-secret"],
            query_tokens=["query-token"],
        )

    def test_hmac_with_rotated_secret(self) -> None:
        assert self._auth({"X-Hub-Signature": sign_body(self.body, "new-secret")}) == "hmac"

    def test_bad_hmac_rejected(self) -> None:
        with pytest.raises(WebhookAuthError):
            self._auth({"X-Hub-Signature": sign_body(self.body + b" ", "new-secret")})

    def test_token_header_and_query(self) -> None:
        assert self._auth({"X-BranchOps-Webhook-Token": "old-secret"}) == "token-header"
        assert self._auth({}, {"token": "query-token"}, allow_query_token=True) == "token-query"
        with pytest.raises(WebhookAuthError):  # disabled by default
            self._auth({}, {"token": "query-token"})
        with pytest.raises(WebhookAuthError):  # header/HMAC secrets are not valid in URLs
            self._auth({}, {"token": "new-secret"}, allow_query_token=True)

    def test_mode_restriction(self) -> None:
        with pytest.raises(WebhookAuthError):
            self._auth({"X-BranchOps-Webhook-Token": "old-secret"}, auth_mode="hmac")

    def test_cidr_allowlist(self) -> None:
        sig = {"X-Hub-Signature": sign_body(self.body, "new-secret")}
        assert self._auth(sig, allowed_source_cidrs=["10.0.0.0/8"]) == "hmac"
        with pytest.raises(WebhookAuthError):
            self._auth(sig, allowed_source_cidrs=["192.168.0.0/16"])

    def test_no_secrets_configured(self) -> None:
        with pytest.raises(WebhookAuthError):
            authenticate_webhook(
                body=b"{}", headers={}, query={}, client_host=None, settings=WebhookSettings(), secrets=[]
            )


class TestNetworkGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "https://llm-gateway.ai.svc.cluster.local/v1",
            "https://inference.bank.internal/v1",
            "http://10.20.30.40:8000/v1",
            "http://localhost:8000",
        ],
    )
    def test_internal_allowed(self, url: str) -> None:
        assert_internal_url(url, NetworkSettings(), purpose="test")

    @pytest.mark.parametrize(
        "url",
        ["https://api.openai.com/v1", "https://8.8.8.8/", "ftp://x.internal/", "https://evil.internal.com/"],
    )
    def test_external_rejected(self, url: str) -> None:
        with pytest.raises(ExternalEndpointError):
            assert_internal_url(url, NetworkSettings(), purpose="test")


class TestAuthAndSettings:
    def test_oauth_proxy_roles(self) -> None:
        settings = SecuritySettings(auth_mode="oauth_proxy")
        p = resolve_principal(
            {"X-Forwarded-User": "ayse", "X-Forwarded-Groups": "x, branchops-analysts"}, "127.0.0.1", settings
        )
        assert p.username == "ayse"
        assert p.has(Role.VIEWER)
        assert p.has(Role.ANALYST)
        assert not p.has(Role.ADMIN)

    def test_oauth_proxy_rejects_untrusted_peer_and_no_group(self) -> None:
        settings = SecuritySettings(auth_mode="oauth_proxy")
        with pytest.raises(AuthenticationError):
            resolve_principal(
                {"X-Forwarded-User": "a", "X-Forwarded-Groups": "branchops-admins"}, "10.0.0.5", settings
            )
        with pytest.raises(AuthenticationError):
            resolve_principal(
                {"X-Forwarded-User": "a", "X-Forwarded-Groups": "others"}, "127.0.0.1", settings
            )
        with pytest.raises(AuthenticationError):
            resolve_principal({}, "127.0.0.1", settings)

    def test_production_guards(self) -> None:
        with pytest.raises(ValidationError, match="auth_mode"):
            Settings.model_validate(
                {"app": {"environment": "production"}, "llm": {"provider": "openai-compatible"}}
            )
        with pytest.raises(ValidationError, match="mock"):
            Settings.model_validate(
                {"app": {"environment": "production"}, "security": {"auth_mode": "oauth_proxy"}}
            )
        with pytest.raises(ValidationError, match="https"):
            Settings.model_validate(
                {
                    "app": {"environment": "production"},
                    "security": {"auth_mode": "oauth_proxy"},
                    "llm": {"provider": "openai-compatible", "base_url": "http://gw.internal/v1"},
                }
            )

    def test_yaml_interpolation(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from branchops.config.settings import load_settings

        cfg = tmp_path / "c.yaml"
        cfg.write_text(
            "llm:\n  provider: openai-compatible\n  base_url: ${LLM_BASE_URL}\n"
            "  model: ${LLM_MODEL:-GLM-5.3-Flash}\n  ca_bundle: ${CA:-}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("LLM_BASE_URL", "https://gw.bank.internal/v1")
        s = load_settings(cfg)
        assert s.llm.base_url == "https://gw.bank.internal/v1"
        assert s.llm.model == "GLM-5.3-Flash"
        assert s.llm.ca_bundle is None
        monkeypatch.delenv("LLM_BASE_URL")
        with pytest.raises(ValueError, match="LLM_BASE_URL"):
            load_settings(cfg)

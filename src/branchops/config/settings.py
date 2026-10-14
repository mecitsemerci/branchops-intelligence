"""Application configuration.

Configuration is read from a YAML file (``BRANCHOPS_CONFIG``, default ``config/branchops.yaml`` if
present) with ``${VAR}`` / ``${VAR:-default}`` environment interpolation. Secrets are never stored in the
file: settings reference *environment variable names* (``*_env``) that OpenShift populates from Secrets.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, model_validator

from branchops.domain.enums import Role
from branchops.scoring.policies import ScoringPolicy

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class AppSettings(BaseModel):
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    service_name: str = "branchops-intelligence"


class DatabaseSettings(BaseModel):
    url: str = "postgresql+psycopg://branchops:branchops@localhost:5432/branchops"
    pool_size: int = 5
    max_overflow: int = 5
    statement_timeout_ms: int = 30_000
    sslmode: str | None = None
    sslrootcert: str | None = None


class WebhookSettings(BaseModel):
    auth_mode: Literal["hmac", "token", "hmac_or_token"] = "hmac_or_token"
    # Comma-separated list of active secrets (supports rotation).
    secrets_env: str = "JIRA_WEBHOOK_SECRETS"
    token_header: str = "X-BranchOps-Webhook-Token"
    # Legacy Jira versions cannot send headers; a URL token is then the only option. Off by default because
    # URLs end up in logs. Query tokens use a separate secret list that is never valid as an HMAC key.
    allow_query_token: bool = False
    query_token_secrets_env: str = "JIRA_WEBHOOK_QUERY_TOKENS"
    max_body_bytes: int = 1_048_576
    allowed_source_cidrs: list[str] = Field(default_factory=list)


class JiraSettings(BaseModel):
    # canonical field name -> Jira custom field id
    field_mapping: dict[str, str] = Field(default_factory=dict)
    # Empty list = accept all projects.
    allowed_projects: list[str] = Field(default_factory=list)


class RetentionSettings(BaseModel):
    on_issue_deleted: Literal["soft_delete", "purge"] = "soft_delete"
    redact_deleted_comments: bool = True
    redact_inbox_payload_after_processing: bool = True


class WorkerSettings(BaseModel):
    concurrency: int = Field(default=2, ge=1, le=32)
    batch_size: int = Field(default=4, ge=1, le=100)
    poll_interval_seconds: float = 2.0
    lease_seconds: int = Field(default=600, ge=30)
    max_attempts: int = Field(default=5, ge=1)
    backoff_base_seconds: float = 15.0
    backoff_max_seconds: float = 1800.0
    health_port: int = 8081
    shutdown_grace_seconds: float = 60.0


class ModelCapabilities(BaseModel):
    tool_calling: bool = False
    structured_outputs: bool = False  # response_format={"type": "json_schema"}
    json_mode: bool = True  # response_format={"type": "json_object"}
    reasoning_parameters: bool = False
    context_length: int = 32_768
    max_output_tokens: int = 4_096


class LLMSettings(BaseModel):
    provider: Literal["openai-compatible", "mock"] = "mock"
    base_url: str | None = None
    model: str = "GLM-5.3-Flash"
    api_key_env: str = "LLM_API_KEY"
    timeout_seconds: float = 120.0
    max_retries: int = Field(default=2, ge=0, le=5)
    temperature: float = 0.0
    max_output_tokens: int = 2_048
    ca_bundle: str | None = None
    capability_detection: Literal["explicit", "probe"] = "explicit"
    capabilities: ModelCapabilities = ModelCapabilities()
    reasoning_effort: str | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)
    health_check_path: str = "/models"

    def api_key(self) -> SecretStr | None:
        value = os.environ.get(self.api_key_env)
        return SecretStr(value) if value else None


class SimilaritySettings(BaseModel):
    candidate_limit: int = 20
    lookback_days: int = 180
    min_text_rank: float = 0.01
    verify_top_n: int = 5
    duplicate_window_hours: int = 72
    recurring_window_days: int = 30
    recurring_threshold: int = 3


class WorkflowSettings(BaseModel):
    checkpointer: Literal["postgres", "memory", "none"] = "postgres"
    node_timeout_seconds: float = 300.0
    max_repair_attempts: int = Field(default=1, ge=0, le=3)
    max_input_chars: int = 24_000
    prompt_version: str = "2026.10-1"
    similarity: SimilaritySettings = SimilaritySettings()


class TemplateSettings(BaseModel):
    directory: str = "templates"


class SecuritySettings(BaseModel):
    auth_mode: Literal["oauth_proxy", "dev"] = "dev"
    trusted_proxy_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "::1"])
    user_header: str = "X-Forwarded-User"
    groups_header: str = "X-Forwarded-Groups"
    role_groups: dict[Role, list[str]] = Field(
        default_factory=lambda: {
            Role.ADMIN: ["branchops-admins"],
            Role.ANALYST: ["branchops-analysts"],
            Role.VIEWER: ["branchops-viewers"],
        }
    )
    dev_user: str = "developer"
    dev_roles: list[Role] = Field(default_factory=lambda: [Role.ADMIN])
    masking_extra_patterns: list[tuple[str, str]] = Field(default_factory=list)


class NetworkSettings(BaseModel):
    """Outbound URL guard: every configured outbound endpoint must match one of these."""

    allowed_host_suffixes: list[str] = Field(
        default_factory=lambda: [".svc", ".svc.cluster.local", ".internal", ".local", "localhost"]
    )
    allow_private_ips: bool = True


class ObservabilitySettings(BaseModel):
    otlp_endpoint: str | None = None
    metrics_enabled: bool = True
    trace_sample_ratio: float = 1.0


class DashboardSettings(BaseModel):
    host: str = "0.0.0.0"  # noqa: S104 - container port, protected by NetworkPolicy and oauth-proxy
    port: int = 8090
    title: str = "BranchOps Intelligence"
    storage_secret_env: str = "DASHBOARD_STORAGE_SECRET"


class ApiSettings(BaseModel):
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8080


class Settings(BaseModel):
    app: AppSettings = AppSettings()
    api: ApiSettings = ApiSettings()
    database: DatabaseSettings = DatabaseSettings()
    webhook: WebhookSettings = WebhookSettings()
    jira: JiraSettings = JiraSettings()
    retention: RetentionSettings = RetentionSettings()
    worker: WorkerSettings = WorkerSettings()
    llm: LLMSettings = LLMSettings()
    workflow: WorkflowSettings = WorkflowSettings()
    templates: TemplateSettings = TemplateSettings()
    scoring: ScoringPolicy = ScoringPolicy()
    security: SecuritySettings = SecuritySettings()
    network: NetworkSettings = NetworkSettings()
    observability: ObservabilitySettings = ObservabilitySettings()
    dashboard: DashboardSettings = DashboardSettings()

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.app.environment == "production":
            if self.security.auth_mode == "dev":
                raise ValueError("security.auth_mode 'dev' is not allowed in production")
            if self.llm.provider == "mock":
                raise ValueError("llm.provider 'mock' is not allowed in production")
            if self.llm.base_url and not self.llm.base_url.startswith("https://"):
                raise ValueError("llm.base_url must use https in production")
        return self

    def webhook_secrets(self) -> list[str]:
        return _env_list(self.webhook.secrets_env)

    def webhook_query_tokens(self) -> list[str]:
        return _env_list(self.webhook.query_token_secrets_env)


def _env_list(name: str) -> list[str]:
    return [s.strip() for s in os.environ.get(name, "").split(",") if s.strip()]


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            env = os.environ.get(name)
            if env is not None:
                return env
            if default is not None:
                return default
            raise ValueError(f"environment variable {name} is required by configuration")

        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def _drop_empty(value: Any) -> Any:
    """Treat empty strings produced by ``${VAR:-}`` as "not configured"."""
    if isinstance(value, dict):
        return {k: _drop_empty(v) for k, v in value.items() if v != ""}
    if isinstance(value, list):
        return [_drop_empty(v) for v in value]
    return value


def load_settings(path: str | Path | None = None) -> Settings:
    candidate = Path(path or os.environ.get("BRANCHOPS_CONFIG", "config/branchops.yaml"))
    data: dict[str, Any] = {}
    if candidate.is_file():
        loaded = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{candidate}: top-level YAML must be a mapping")
        data = _drop_empty(_interpolate(loaded))
    elif path is not None:
        raise FileNotFoundError(candidate)
    if url := os.environ.get("DATABASE_URL"):
        data.setdefault("database", {})["url"] = url
    return Settings.model_validate(data)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()

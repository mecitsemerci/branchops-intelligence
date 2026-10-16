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
from branchops.scoring.policies import ScoringPolicy, registry_conflicts

# Development fallback: stable within one process only (production requires the configured key).
_PROCESS_PSEUDONYM_KEY = os.urandom(32)
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
    """HTTP ingestion limits. The auth fields are the legacy (pre-``sources``) Jira webhook settings: they are
    still read and translated into the ``jira`` source when the configuration has no ``sources`` list."""

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
    # Push events whose own (signed) timestamp is older than this are acknowledged and ignored. It must stay
    # below ``retention.inbox_days``: an accepted event's idempotency key then always outlives its
    # replay window, so a captured delivery cannot be re-submitted after its inbox row was purged.
    max_event_age_days: int = Field(default=7, ge=1)


class JiraSettings(BaseModel):
    """Deprecated single-source section; translated into ``sources: [{id: jira, ...}]``."""

    # canonical field name -> Jira custom field id
    field_mapping: dict[str, str] = Field(default_factory=dict)
    # Empty list = accept all projects.
    allowed_projects: list[str] = Field(default_factory=list)


SourceType = Literal["jira_webhook", "servicenow_webhook", "csv_import", "mcp_report"]
PUSH_SOURCE_TYPES: frozenset[str] = frozenset({"jira_webhook", "servicenow_webhook"})


class SourceAuthSettings(BaseModel):
    """Authentication of a push source (see ``infrastructure/security/webhook_auth.py``)."""

    mode: Literal["hmac", "token", "hmac_or_token"] = "hmac_or_token"
    # Comma-separated list of active secrets (supports rotation).
    secrets_env: str = "JIRA_WEBHOOK_SECRETS"
    token_header: str = "X-BranchOps-Webhook-Token"
    allow_query_token: bool = False
    query_token_secrets_env: str = "JIRA_WEBHOOK_QUERY_TOKENS"
    allowed_source_cidrs: list[str] = Field(default_factory=list)

    @property
    def auth_mode(self) -> str:
        return self.mode

    def secrets(self) -> list[str]:
        return _env_list(self.secrets_env)

    def query_tokens(self) -> list[str]:
        return _env_list(self.query_token_secrets_env)


class SourceSettings(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    type: SourceType
    enabled: bool = True
    description: str | None = None
    # Push sources only.
    auth: SourceAuthSettings | None = None
    # Jira project keys / ServiceNow assignment groups / mailboxes. Empty list = accept all.
    allowed_groups: list[str] = Field(default_factory=list)
    # canonical field name -> source field (Jira custom field id, ServiceNow column, CSV header).
    field_mapping: dict[str, str] = Field(default_factory=dict)
    # Pull sources only: how often ``branchops poll`` calls the adapter.
    poll_interval_seconds: float = Field(default=300.0, ge=5)
    # Adapter-specific options (e.g. CSV ``id_column``, ``watch_directory``, ``delimiter``).
    options: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_push(self) -> bool:
        return self.type in PUSH_SOURCE_TYPES


def _legacy_jira_source(data: dict[str, Any]) -> dict[str, Any]:
    """Build the ``jira`` source from the pre-multi-source ``jira:`` and ``webhook:`` sections."""
    jira = data.get("jira") or {}
    webhook = data.get("webhook") or {}
    if not isinstance(jira, dict) or not isinstance(webhook, dict):
        raise ValueError("jira/webhook sections must be mappings")
    auth = {
        "mode": webhook.get("auth_mode", "hmac_or_token"),
        **{
            k: webhook[k]
            for k in (
                "secrets_env",
                "token_header",
                "allow_query_token",
                "query_token_secrets_env",
                "allowed_source_cidrs",
            )
            if k in webhook
        },
    }
    return {
        "id": "jira",
        "type": "jira_webhook",
        "enabled": True,
        "auth": auth,
        "allowed_groups": list(jira.get("allowed_projects") or []),
        "field_mapping": dict(jira.get("field_mapping") or {}),
    }


class ReportingSettings(BaseModel):
    """Incident reporting through the MCP server (report_incident / add_incident_update).

    When enabled, a source ``source_id`` of type ``mcp_report`` is added automatically (unless configured
    explicitly); reported incidents are ``<source_id>:<key_prefix>-<yyyymmdd>-<random>``.
    """

    enabled: bool = False
    source_id: str = Field(default="app", pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    key_prefix: str = Field(default="RPT", pattern=r"^[A-Z][A-Z0-9]{0,9}$")
    # group_key of reports without an application (scopes of MCP teams can restrict by group key).
    default_group_key: str = "IN-APP"
    max_description_chars: int = Field(default=8000, ge=500, le=50_000)
    max_update_chars: int = Field(default=4000, ge=100, le=20_000)


class TicketingSettings(BaseModel):
    """Optional ticket creation for reported incidents (via ``ticketing_outbox``, drained by the worker)."""

    sink: Literal["none", "jira"] = "none"
    base_url: str | None = None  # internal Jira, e.g. https://jira.bank.internal
    token_env: str = "TICKETING_JIRA_TOKEN"  # personal access token (Bearer)
    default_project_key: str | None = None
    issue_type: str = "Incident"
    # Tickets carry this label; exclude it (or the project) from the Jira source to avoid re-ingestion loops.
    labels: list[str] = Field(default_factory=lambda: ["branchops-reported"])
    timeout_seconds: float = 20.0
    ca_bundle: str | None = None

    def token(self) -> str | None:
        return os.environ.get(self.token_env) or None


class S3StoreSettings(BaseModel):
    """Internal S3-compatible object store (MinIO / Ceph RGW); path-style requests, SigV4."""

    endpoint: str | None = None  # e.g. https://minio.storage.svc:9000
    bucket: str = "branchops-attachments"
    region: str = "us-east-1"
    access_key_env: str = "ATTACHMENTS_S3_ACCESS_KEY"
    secret_key_env: str = "ATTACHMENTS_S3_SECRET_KEY"
    prefix: str = "attachments/"
    ca_bundle: str | None = None
    timeout_seconds: float = 30.0

    def credentials(self) -> tuple[str, str] | None:
        access, secret = os.environ.get(self.access_key_env), os.environ.get(self.secret_key_env)
        return (access, secret) if access and secret else None


class AttachmentSettings(BaseModel):
    """Screenshots and logs attached to incidents (never sent to the LLM)."""

    max_bytes: int = Field(default=3 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    allowed_types: list[str] = Field(default_factory=lambda: ["image/png", "image/jpeg", "text/plain"])
    max_per_incident: int = Field(default=5, ge=1, le=50)
    max_image_pixels: int = Field(default=40_000_000, ge=1_000_000)
    store: Literal["postgres", "s3"] = "postgres"
    s3: S3StoreSettings = S3StoreSettings()
    # Lifetime of the draft_token issued by check_incident_draft (upload before report_incident).
    draft_token_ttl_minutes: int = Field(default=30, ge=5, le=24 * 60)
    forward_to_ticketing: bool = True


class EmailChannelSettings(BaseModel):
    """Internal SMTP relay. ``backend: log`` only logs (development), ``null`` drops."""

    backend: Literal["smtp", "log", "null"] = "log"
    smtp_host: str | None = None  # e.g. smtp-relay.bank.internal
    smtp_port: int = 587
    starttls: bool = True
    ca_bundle: str | None = None
    username_env: str | None = None  # environment variable NAMES (secrets via OpenShift Secret)
    password_env: str | None = None
    from_address: str = "branchops@bank.internal"
    from_name: str = "BranchOps"
    timeout_seconds: float = 20.0


class ZoomChannelSettings(BaseModel):
    """Zoom Team Chat incoming webhooks, reached through the bank's egress proxy.

    The URL is ``<webhook_base_url>/<team.zoom_channel_id>?format=full`` with the team's verification token
    (environment variable named by ``team.zoom_webhook_secret_ref``) in the Authorization header.
    """

    backend: Literal["webhook", "log", "null"] = "log"
    webhook_base_url: str | None = (
        None  # e.g. https://zoom-egress.bank.internal/chat/webhooks/incomingwebhook
    )
    ca_bundle: str | None = None
    timeout_seconds: float = 15.0


class NotificationSettings(BaseModel):
    enabled: bool = True
    email: EmailChannelSettings = EmailChannelSettings()
    zoom: ZoomChannelSettings = ZoomChannelSettings()
    # Templates live in <templates.directory>/notifications/*.j2 unless set.
    template_directory: str | None = None


class OwnershipSettings(BaseModel):
    """Screen ownership registry and routing (data is managed in the dashboard under /yonetim)."""

    # CC on every routed notification and fallback owner when nothing else matches.
    branch_ops_team_key: str = "branch-ops"
    # Receives ``ownership_unresolved`` notifications together with branch operations.
    platform_team_key: str | None = "platform"


class OutboxSettings(BaseModel):
    """Drain loops of the transactional outboxes (ticketing, notifications) in the worker process."""

    poll_interval_seconds: float = Field(default=5.0, gt=0)
    batch_size: int = Field(default=20, ge=1, le=200)
    lease_seconds: int = Field(default=120, ge=10)
    max_attempts: int = Field(default=8, ge=1)
    backoff_base_seconds: float = 30.0
    backoff_max_seconds: float = 3600.0


class RetentionSettings(BaseModel):
    on_issue_deleted: Literal["soft_delete", "purge"] = "soft_delete"
    redact_deleted_comments: bool = True
    redact_inbox_payload_after_processing: bool = True
    # Attachments (and unbound draft uploads) older than this are purged by the worker / CLI.
    attachments_days: int = Field(default=180, ge=1)
    # Finished inbox events (raw source payloads, masked after processing) and their LangGraph checkpoints.
    # Dead letters are kept longer so that they can still be inspected and replayed.
    inbox_days: int = Field(default=30, ge=1)
    inbox_dead_letter_days: int = Field(default=90, ge=1)


class RecoverySettings(BaseModel):
    """Automatic re-analysis of incidents whose analysis degraded because the model was unavailable."""

    enabled: bool = True
    # How often the worker checks for due recoveries (each replica; rows are claimed with SKIP LOCKED).
    interval_seconds: float = Field(default=60.0, ge=5)
    # First retry after the degraded analysis, then doubling up to the maximum (with jitter).
    base_delay_seconds: float = Field(default=300.0, ge=1)
    max_delay_seconds: float = Field(default=6 * 3600.0, ge=1)
    max_attempts: int = Field(default=6, ge=1, le=50)
    # Recoveries enqueued per tick and replica: a returning model is not flooded (no retry storm).
    batch_size: int = Field(default=10, ge=1, le=500)
    # Enqueue only after a successful model health probe.
    require_model_health: bool = True
    # RUNNING analysis rows without a heartbeat for this long are closed as FAILED (crashed worker).
    stale_running_minutes: int = Field(default=60, ge=5)


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


class EmbeddingSettings(BaseModel):
    """Optional semantic retrieval with an *approved internal embedding model* (OpenAI-compatible
    ``/embeddings``). Off by default; the generative model (e.g. GLM-5.3-Flash) must not be used here and
    nothing is downloaded. When the endpoint is unavailable, retrieval falls back to full-text search.
    """

    enabled: bool = False
    base_url: str | None = None
    model: str | None = None
    api_key_env: str = "EMBEDDING_API_KEY"
    ca_bundle: str | None = None
    timeout_seconds: float = Field(default=10.0, gt=0)
    # Most recent embedded incidents compared per analysis (cosine in Python; no pgvector required).
    candidate_pool: int = Field(default=2000, ge=10, le=20_000)
    top_k: int = Field(default=10, ge=1, le=50)
    min_score: float = Field(default=0.75, ge=0, le=1)
    max_input_chars: int = Field(default=2000, ge=100)

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
    embeddings: EmbeddingSettings = EmbeddingSettings()


class InvestigationSettings(BaseModel):
    """Optional bounded historical investigation after the similarity step (off by default).

    It runs only when the similarity step found a related/duplicate/possible-common-cause incident or a
    recurring error signature; the model can only call allow-listed read-only tools.
    """

    enabled: bool = False
    max_tool_calls: int = Field(default=4, ge=1, le=10)
    max_depth: int = Field(default=3, ge=1, le=6)  # model turns that may call a tool
    max_seconds: float = Field(default=90.0, ge=5)
    max_tokens: int = Field(default=12_000, ge=1000)


class WorkflowSettings(BaseModel):
    checkpointer: Literal["postgres", "memory", "none"] = "postgres"
    node_timeout_seconds: float = 300.0
    max_repair_attempts: int = Field(default=1, ge=0, le=3)
    max_input_chars: int = 24_000
    prompt_version: str = "2026.10-3"
    similarity: SimilaritySettings = SimilaritySettings()
    investigation: InvestigationSettings = InvestigationSettings()


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
    # IdP groups of the dev principal (``auth_mode: dev`` only); used for MCP team resolution.
    dev_groups: list[str] = Field(default_factory=list)
    masking_extra_patterns: list[tuple[str, str]] = Field(default_factory=list)
    # Key for pseudonymising host-application usernames in app_context (HMAC); required in production.
    pseudonym_key_env: str = "BRANCHOPS_PSEUDONYM_KEY"

    def pseudonym_key(self) -> bytes:
        value = os.environ.get(self.pseudonym_key_env)
        return value.encode() if value else _PROCESS_PSEUDONYM_KEY


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
    # External URL used in links (tickets, notifications), e.g. https://branchops.apps.bank.internal
    public_url: str | None = None
    storage_secret_env: str = "DASHBOARD_STORAGE_SECRET"


class ApiSettings(BaseModel):
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8080


McpFieldMode = Literal["full", "masked"]


class McpTeamSettings(BaseModel):
    """One consuming team of the MCP server. ``"*"`` in a list means "unrestricted"."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    # IdP groups (from the oauth2-proxy groups header) whose members belong to this team.
    groups: list[str] = Field(min_length=1)
    sources: list[str] = Field(default_factory=lambda: ["*"])
    group_keys: list[str] = Field(default_factory=lambda: ["*"])
    branch_codes: list[str] = Field(default_factory=lambda: ["*"])
    # ``masked``: free text (description, comments, evidence) is run through the masker again and
    # comment authors are hidden.
    fields: McpFieldMode = "masked"
    tools: list[str] = Field(default_factory=lambda: ["*"])
    # Role granted to team members *in the MCP server only* (request_reanalysis needs ANALYST). Team groups
    # are deliberately not added to ``security.role_groups``: that would let MCP-only users reach the
    # unscoped API and dashboard. Global roles still apply on top.
    role: Role = Role.VIEWER
    # Operational tools (``get_inbox_health``) normally need the ADMIN role; this grants them to the team.
    ops_access: bool = False


class McpSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8095
    path: str = Field(default="/mcp", pattern=r"^/[a-z0-9/_-]*$")
    max_page_size: int = Field(default=50, ge=1, le=200)
    # Description / comment / evidence text longer than this is truncated in tool output (marked).
    max_text_chars: int = Field(default=4000, ge=200, le=100_000)
    rate_limit_per_minute: int = Field(default=120, ge=1)
    # JSON-RPC requests are small (tool name + arguments); larger bodies are rejected with 413.
    max_request_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    teams: list[McpTeamSettings] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_teams(self) -> McpSettings:
        ids = [t.id for t in self.teams]
        if len(ids) != len(set(ids)):
            raise ValueError("mcp team ids must be unique")
        return self


class Settings(BaseModel):
    app: AppSettings = AppSettings()
    api: ApiSettings = ApiSettings()
    database: DatabaseSettings = DatabaseSettings()
    webhook: WebhookSettings = WebhookSettings()
    jira: JiraSettings = JiraSettings()  # deprecated: translated into ``sources``
    sources: list[SourceSettings] = Field(default_factory=list)
    retention: RetentionSettings = RetentionSettings()
    worker: WorkerSettings = WorkerSettings()
    llm: LLMSettings = LLMSettings()
    workflow: WorkflowSettings = WorkflowSettings()
    recovery: RecoverySettings = RecoverySettings()
    templates: TemplateSettings = TemplateSettings()
    scoring: ScoringPolicy = ScoringPolicy()
    security: SecuritySettings = SecuritySettings()
    network: NetworkSettings = NetworkSettings()
    observability: ObservabilitySettings = ObservabilitySettings()
    dashboard: DashboardSettings = DashboardSettings()
    mcp: McpSettings = McpSettings()
    reporting: ReportingSettings = ReportingSettings()
    ticketing: TicketingSettings = TicketingSettings()
    outbox: OutboxSettings = OutboxSettings()
    ownership: OwnershipSettings = OwnershipSettings()
    attachments: AttachmentSettings = AttachmentSettings()
    notifications: NotificationSettings = NotificationSettings()

    @model_validator(mode="before")
    @classmethod
    def _translate_legacy_sources(cls, data: Any) -> Any:
        """An empty/absent ``sources`` list means "the single Jira source of earlier releases"."""
        if not isinstance(data, dict) or data.get("sources"):
            return data
        return {**data, "sources": [_legacy_jira_source(data)]}

    @model_validator(mode="after")
    def _source_rules(self) -> Settings:
        if self.reporting.enabled and self.source(self.reporting.source_id) is None:
            self.sources.append(SourceSettings(id=self.reporting.source_id, type="mcp_report"))
        reporting_source = self.source(self.reporting.source_id)
        if self.reporting.enabled and reporting_source is not None and reporting_source.type != "mcp_report":
            raise ValueError(f"reporting.source_id {self.reporting.source_id!r} must be an mcp_report source")
        ids = [s.id for s in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("source ids must be unique")
        for src in self.sources:
            if src.is_push and src.auth is None:
                src.auth = SourceAuthSettings()
        return self

    @model_validator(mode="after")
    def _embedding_model(self) -> Settings:
        emb = self.workflow.similarity.embeddings
        if emb.enabled:
            if not (emb.base_url and emb.model):
                raise ValueError("similarity.embeddings: base_url and model are required when enabled")
            if emb.model == self.llm.model:
                raise ValueError(
                    "similarity.embeddings.model must be an approved embedding model, not the generative "
                    f"model {self.llm.model!r}"
                )
        return self

    @model_validator(mode="after")
    def _replay_window(self) -> Settings:
        if self.webhook.max_event_age_days >= self.retention.inbox_days:
            raise ValueError(
                "webhook.max_event_age_days must be lower than retention.inbox_days "
                "(otherwise purged idempotency keys reopen a replay window)"
            )
        return self

    @model_validator(mode="after")
    def _released_scoring_policy(self) -> Settings:
        conflicts = registry_conflicts(self.scoring)
        if conflicts:
            raise ValueError(
                f"scoring: version {self.scoring.version} is a released policy; changing {conflicts} "
                "requires a new scoring.version"
            )
        return self

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.app.environment == "production":
            if self.security.auth_mode == "dev":
                raise ValueError("security.auth_mode 'dev' is not allowed in production")
            if self.llm.provider == "mock":
                raise ValueError("llm.provider 'mock' is not allowed in production")
            if self.llm.base_url and not self.llm.base_url.startswith("https://"):
                raise ValueError("llm.base_url must use https in production")
            for src in self.enabled_sources():
                if src.is_push and src.auth is not None and not src.auth.secrets():
                    raise ValueError(
                        f"source {src.id!r}: secrets are not configured "
                        f"(environment variable {src.auth.secrets_env} is empty)"
                    )
            if self.reporting.enabled and not os.environ.get(self.security.pseudonym_key_env):
                raise ValueError(f"reporting: {self.security.pseudonym_key_env} must be set in production")
            if self.notifications.enabled:
                email, zoom = self.notifications.email, self.notifications.zoom
                if email.backend == "smtp" and not email.smtp_host:
                    raise ValueError("notifications.email: smtp_host is required")
                if zoom.backend == "webhook" and not (zoom.webhook_base_url or "").startswith("https://"):
                    raise ValueError("notifications.zoom.webhook_base_url must use https in production")
            if self.ticketing.sink == "jira":
                if not (self.ticketing.base_url or "").startswith("https://"):
                    raise ValueError("ticketing.base_url must use https in production")
                if not self.ticketing.token():
                    raise ValueError(f"ticketing: {self.ticketing.token_env} is empty")
        if self.ticketing.sink == "jira" and not (
            self.ticketing.base_url and self.ticketing.default_project_key
        ):
            raise ValueError("ticketing.sink 'jira' requires base_url and default_project_key")
        return self

    def enabled_sources(self) -> list[SourceSettings]:
        return [s for s in self.sources if s.enabled]

    def source(self, source_id: str) -> SourceSettings | None:
        return next((s for s in self.sources if s.id == source_id), None)

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

# Architecture

BranchOps Intelligence is a **modular monolith**: one Python code base (`src/branchops`) deployed as five
processes: api, worker, dashboard, mcp, and the optional source poller. They share the same domain,
application and infrastructure layers (one image; see [OpenShift deployment](openshift-deployment.md)).

| Process   | Entry point                 | Responsibility                                                     |
|-----------|-----------------------------|--------------------------------------------------------------------|
| API       | `branchops api`             | Push-source ingestion (Jira, ServiceNow, …), read API, health probes, `/metrics` |
| Worker    | `branchops worker`          | Claims inbox events, merges incident state, runs the analysis workflow; drains the ticketing and notification outboxes; purges old attachments |
| Dashboard | `branchops dashboard`       | NiceGUI operational dashboard: read views, re-analysis and re-route requests, ownership administration (`/yonetim`), notifications (`/bildirimler`) |
| Poller    | `branchops poll`            | Optional: reads pull sources (file drops, …) into the inbox; one leader via advisory lock (not in the base manifests) |
| MCP       | `branchops mcp`             | Team-scoped MCP server for other teams' MCP clients (read-mostly: queries plus in-app incident reporting and re-analysis requests); never calls the LLM ([details](mcp-server.md)) |

All of them talk to a single PostgreSQL database. There is no message broker: PostgreSQL is the durable
inbox and the job-coordination mechanism (`FOR UPDATE SKIP LOCKED` + leases).

```mermaid
flowchart LR
    Jira[Jira Data Center / Cloud<br/>webhooks] -- HTTPS + HMAC/token --> API
    SNOW[ServiceNow<br/>Business Rule] -- HTTPS + token --> API
    subgraph OpenShift namespace
        API[API pod<br/>FastAPI] -- INSERT inbox --> PG[(PostgreSQL)]
        Worker[Worker pods<br/>N replicas] -- claim SKIP LOCKED --> PG
        Worker -- OpenAI-compatible HTTPS --> LLM[Internal inference gateway<br/>GLM-5.3-Flash]
        Dash[Dashboard pod<br/>NiceGUI] -- read queries --> PG
        Proxy[oauth2-proxy sidecar] --> Dash
        Proxy2[oauth2-proxy sidecar] --> API
        MCP[MCP pods<br/>FastMCP] -- team-scoped queries, reports --> PG
        Proxy3[oauth2-proxy sidecar] --> MCP
    end
    Users[Bank staff] -- SSO --> Proxy
    Teams[Other teams' MCP clients] -- SSO bearer token --> Proxy3
    Prom[Internal Prometheus] -- scrape --> API & Worker
```

## Layering

```mermaid
flowchart TB
    api[api/ — FastAPI routes, middleware, auth deps] --> application
    dashboard[dashboard/ — NiceGUI pages] --> application
    workers[workers/ — dispatcher, processor] --> application
    workers --> workflows
    workflows[workflows/ — LangGraph graph + state] --> agents
    workflows --> scoring
    agents[agents/ — prompt + schema per logical agent] --> interfaces
    application[application/ — commands, queries, handlers, interfaces] --> domain
    scoring[scoring/ — deterministic engines] --> domain
    templates[templates/ — registry, conditions, validation] --> domain
    infrastructure["infrastructure/ — SQLAlchemy, source adapters (jira/servicenow/csv_import/mcp_report), LLM providers, attachments, notifications, ticketing, OTel"] -. implements .-> interfaces
    domain[domain/ — Pydantic entities, value objects, enums, pure services]
```

Rules enforced by convention and review:

* `domain/`, `scoring/` and `templates/` import nothing from LangGraph, FastAPI, SQLAlchemy or OpenAI.
* Agents depend on the `ModelProvider` protocol (`application/interfaces/model_provider.py`), never on a
  concrete client.
* The LLM never receives tools. All data access (similar-issue retrieval, history) is performed by Python
  code; the model only classifies, extracts, judges and writes text, and every output is validated by
  Pydantic and by deterministic post-checks.

## End-to-end flow (vertical slice)

```mermaid
sequenceDiagram
    participant J as Push source (Jira, ServiceNow)
    participant A as API
    participant DB as PostgreSQL
    participant W as Worker
    participant G as LangGraph workflow
    participant M as Internal LLM
    J->>A: POST /api/ingest/{source} (Jira also: /api/webhooks/jira)
    A->>A: source adapter: authenticate, size limit, JSON parse, normalise to IncidentEvent
    A->>DB: INSERT webhook_inbox ON CONFLICT (idempotency_key) DO NOTHING
    A-->>J: 202 Accepted (or 200 duplicate)
    loop poll
        W->>DB: reap expired leases; SELECT … FOR UPDATE SKIP LOCKED
    end
    W->>W: registry.get(source).normalize(payload, masking)
    W->>DB: merge snapshot into issues (row lock, timestamp-guarded)
    W->>G: run(issue snapshot)
    G->>M: classification → extraction → quality assessment → similarity verify → recommendations
    G->>G: deterministic scoring (Python)
    W->>DB: persist analysis, steps, relationships; mark inbox COMPLETED
```

## Key architectural decisions

| # | Decision | Rationale |
|---|----------|-----------|
| AD-1 | Modular monolith, five processes (api, worker, dashboard, mcp, and the optional source poller) | Small team, single DB, independent scaling of the worker. |
| AD-2 | PostgreSQL inbox instead of Kafka/Redis/Celery | Durable, transactional with domain writes, already operated by the bank. |
| AD-3 | Synchronous SQLAlchemy + thread-pool worker | LLM calls are I/O bound and few per minute; sync code is simpler to audit. |
| AD-4 | Out-of-order safety by **timestamp-guarded merge**, not by delivery order | Jira does not guarantee ordering; each field group is only overwritten by a newer `updated` timestamp. |
| AD-5 | Scores computed only by Python | LLM output is non-deterministic even at temperature 0; scores must be reproducible from stored inputs. |
| AD-6 | Evidence quotes verified against source text | A cheap, deterministic hallucination guard: unverifiable values are downgraded to `UNKNOWN`. |
| AD-7 | LangGraph with Postgres checkpointer | A retried event resumes after the last completed node instead of repeating LLM calls. |
| AD-8 | Templates are files + immutable DB copies | Reviewable in Git, and analyses always reference the exact published version. |
| AD-9 | oauth2-proxy sidecar (OIDC) for SSO | Reuses the bank IdP and its groups for RBAC; the app binds to loopback and only trusts identity headers from the sidecar. |
| AD-11 | **Ports & Adapters for incident sources** | Every source normalises into one canonical `IncidentEvent`; identity is `incident_key = source:external_key`. Everything after the inbox is source-neutral, so a new source is one adapter + configuration (see [sources](sources.md)). |
| AD-12 | **MCP server serves data, not reasoning** | Other teams query BranchOps from their own MCP clients through fixed, parameterised tools; team scopes are SQL predicates on the shared read models, the server never calls the LLM and has no raw-query tool (see [MCP server](mcp-server.md)). |
| AD-13 | **Ownership is data; routing is deterministic** | Teams, applications, screens and routing rules live in PostgreSQL and are edited in the dashboard; `resolve_owner` is a pure function (no model). Notifications and tickets are written to transactional outboxes in the same transaction as the report and delivered by the worker (see [ownership and routing](ownership-and-routing.md)). |
| AD-10 | Outbound URL guard | LLM and telemetry endpoints must resolve to allow-listed internal hosts; public hosts are refused at startup. |

See the other documents in `docs/` for the details of each subsystem.

## Source layout

```
src/branchops/
  api/            FastAPI app factory; routes: webhooks+ingest, issues (incl. analytics, admin), ownership,
                  routing+notifications, health; dependencies, middleware
  application/    commands (ingest incident event, re-analysis), handlers (process inbox event), queries (read models), ports (incl. source adapters)
  domain/         Pydantic entities, value objects, enums, pure services (merge, masking, evidence, injection, signatures)
  agents/         logical agents + structured-output caller + prompt envelope
  workflows/      LangGraph state and graph
  scoring/        deterministic completeness/quality engine and versioned policies
  templates/      template contracts, conditions/validation, registry
  infrastructure/ SQLAlchemy models/session, repositories, source adapters (sources/jira, servicenow, csv_import,
                  mcp_report), LLM providers, attachments, notifications, ticketing, security, observability
                  (jira/adapter.py is a deprecated re-export shim of sources/jira)
  workers/        dispatcher (claim loop, graceful drain, health server), per-event runner (leases, retries),
                  pull-source poller, model-outage recovery scheduler, housekeeping (retention)
  evaluation/     offline evaluation: datasets, record/replay providers, metrics, calibration, reports
  dashboard/      NiceGUI pages and components
  mcp/            MCP server: auth/teams, read tools, reporting / ownership tools, resources, prompts, audit middleware
  config/         settings (YAML + env interpolation, production guards)
  container.py    composition root;  cli.py  `branchops` entry point
templates/        versioned JSON templates      samples/{jira,servicenow,csv}/  synthetic Turkish payloads
migrations/       Alembic                        deploy/        Dockerfile, compose, OpenShift kustomize
```

## Data model

```mermaid
erDiagram
    webhook_inbox ||--o{ analyses : "inbox_event_id"
    issues ||--o{ analyses : has
    issues ||--o{ issue_comments : has
    issues ||--o{ issue_relationships : "source/target"
    analyses ||--o{ agent_steps : records
    source_checkpoints {
      text source_id PK
      text cursor "pull sources: newest event timestamp read"
    }
    template_versions ||..o{ analyses : "template_id+version"
    webhook_inbox {
      uuid id PK
      text idempotency_key UK "source:adapter key"
      text source "jira|snow|csv|…"
      text issue_key "the source's external key"
      text status "PENDING|PROCESSING|COMPLETED|SKIPPED|DEAD_LETTER"
      int attempts
      timestamptz available_at
      text lease_owner
      timestamptz lease_expires_at
      jsonb payload "masked after processing"
    }
    issues {
      text incident_key PK "source:external_key"
      text source
      text external_key "UK with source"
      text issue_key "deprecated (= external_key), one release"
      jsonb snapshot "merged, masked, versioned per attribute"
      tsvector search_vector "turkish FTS"
      text error_signature
      float latest_completeness
      float latest_quality
    }
    analyses {
      uuid id PK
      text status
      text input_hash
      text template_version
      text scoring_policy_version
      text model_id
      jsonb field_scores
      jsonb recommendations
    }
```

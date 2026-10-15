# BranchOps Intelligence

On-premise, Python-only agentic application that analyses operational incidents reported by bank branch
staff. Incidents arrive from several sources — Jira webhooks, ServiceNow Business Rule webhooks, CSV/XLSX/JSONL
imports — through per-source adapters into one source-neutral pipeline. It classifies and structures each incident with the bank's internal
LLM (preferred: **GLM-5.3-Flash** via an OpenAI-compatible gateway), maps it to a versioned template,
computes **deterministic** completeness and quality scores, recommends what information is missing,
detects similar and recurring incidents, and shows everything in an internal NiceGUI dashboard.

It runs entirely inside OpenShift without internet access: no cloud LLMs, no CDN assets, no runtime
package or model downloads, no external telemetry.

```mermaid
flowchart LR
    J[Jira / ServiceNow webhooks] -->|HMAC / token| A[API · FastAPI]
    F[CSV · XLSX · JSONL files] --> P[Poller / import CLI]
    A -->|INSERT, idempotent| PG[(PostgreSQL<br/>inbox + data)]
    P -->|INSERT, idempotent| PG
    W[Worker · LangGraph] -->|FOR UPDATE SKIP LOCKED| PG
    W -->|OpenAI-compatible, TLS| M[Internal LLM gateway<br/>GLM-5.3-Flash]
    D[Dashboard · NiceGUI] --> PG
    C[Other teams' MCP clients] -->|SSO bearer token| X[MCP server · read-only]
    X -->|team-scoped SQL| PG
```

## Features

| Capability | Where |
|---|---|
| Source-agnostic ingestion: per-source adapters (Jira, ServiceNow reference, CSV/XLSX/JSONL) behind push/pull ports, `POST /api/ingest/{source}` | `application/interfaces/source_adapter.py`, `infrastructure/sources/`, [docs](docs/sources.md) |
| Authenticated webhook ingestion (HMAC `X-Hub-Signature`, header token, rotation, size limit, CIDR allow-list) | `api/routes/webhooks.py`, `infrastructure/security/webhook_auth.py` |
| Durable PostgreSQL inbox: idempotency, leases, retries with backoff, dead-letter, crash recovery | `infrastructure/persistence/inbox_repository.py`, `workers/` |
| Out-of-order / partial payload handling (per-attribute version vector, tombstones) | `domain/services/snapshot_merge.py` |
| Classification, extraction, quality assessment, similarity verification, recommendations, reporting agents | `agents/` |
| Bounded LangGraph workflow with Postgres checkpoints, partial results and manual-review status | `workflows/issue_analysis.py` |
| Versioned, immutable JSON templates with conditions and rubrics (7 defaults) | `templates/`, `src/branchops/templates/` |
| Deterministic completeness & quality scoring with versioned policy | `scoring/` |
| Evidence verification (anti-hallucination), PII masking, prompt-injection flags | `domain/services/` |
| Similar / duplicate / recurring incidents (PostgreSQL FTS `turkish` + error signatures) | `agents/similarity.py`, `issue_repository.py` |
| Dashboard: overview, explorer, issue detail, quality analytics, agent executions, dead letters | `dashboard/` |
| In-app incident reporting via MCP: app context, screenshots (re-encoded, blurred, never sent to the LLM), draft check, Jira ticket outbox | `mcp/reporting.py`, `application/commands/`, [docs](docs/embedded-agent-guide.md) |
| Screen ownership registry managed in the dashboard (`/yonetim`), deterministic owner routing, e-mail / Zoom notifications through an outbox (branch operations always CC) | `domain/entities/ownership.py`, `application/commands/routing.py`, [docs](docs/ownership-and-routing.md) |
| Read-only MCP server for other teams (Claude, Cursor, chatbots): team-scoped tools, resources, prompts; audited | `mcp/`, [docs](docs/mcp-server.md) |
| OpenTelemetry metrics (Prometheus) and traces (internal OTLP only) | `infrastructure/observability/` |
| OpenShift manifests: non-root, read-only FS, probes, NetworkPolicies, HPA, oauth2-proxy SSO | `deploy/openshift/` |

## Quick start

Requirements: Docker with Compose v2, GNU make, Python 3.12 (only for `make seed` and the virtualenv
targets). `make help` lists every target.

### Full stack in Docker — Gemma agents + simulated Jira traffic

```bash
make up                      # Gemma via host Ollama (default) + random incidents every 5 minutes
open http://localhost:8090/live   # live agent view: who is doing what, workflow diagram per run
make sim-once                # one more random incident right now
make logs SERVICE=worker
make down                    # stop (make clean-docker also deletes the database volume)
```

| LLM_MODE | Model server | Notes |
|---|---|---|
| `host` (default) | Ollama on this machine, `http://host.docker.internal:11434/v1` | fastest (Metal/GPU); `make model` pulls `gemma3:4b` and creates `branchops-gemma3:4b` (16k context, temperature 0) |
| `docker` | `ollama` compose service (profile `ollama`) | no host install needed; CPU inside Docker, slower |
| `mock` | none | deterministic keyword rules, for offline/CI |

`make up LLM_MODE=docker` / `make up LLM_MODE=mock` switch modes. The **simulator** service posts a
random Turkish branch incident every `SIMULATOR_INTERVAL_SECONDS` (default 300 s, ±10 % jitter) after a
burst of `SIMULATOR_INITIAL_BURST` (default 2) — mixed categories, complete/partial/vague profiles,
near-duplicates, recurring error codes and follow-up comments that add missing information (so score
improvements appear). `make sim-stop` / `make sim-start` pause and resume it. With a local 4B model an
analysis takes roughly 1–2 minutes, well inside the 5-minute arrival interval.

The **Canlı Akış** (`/live`) page refreshes every 2 seconds and shows:
queue/worker KPIs; one card per workflow node (LLM agents and deterministic steps) with the issue it is
working on, the worker, elapsed time, its latest output and 24 h statistics; a LangGraph diagram per
running analysis with node states (waiting / running / done / failed / skipped); recently finished
analyses with their per-node outcome strip and a diagram + step table on demand; incoming webhook events
and active workers.

Other targets: `make seed` (fixed samples), `make overview`, `make up WORKERS=3`, `make health`.

| Service | URL / port | Notes |
|---|---|---|
| api | http://localhost:8080 | `/api/webhooks/jira`, `/api/*`, `/health/*`, `/metrics` |
| dashboard | http://localhost:8090 | dev auth (ADMIN) |
| mcp | http://localhost:8095/mcp | read-only MCP server, dev auth (`make mcp-health`) |
| worker | :8081 inside the container | `/healthz`, `/readyz`, `/metrics` (`make health`) |
| simulator | — | posts random incidents to the api (`simulate` command) |
| postgres | localhost:55432 | `branchops/branchops`, databases `branchops` and `branchops_test` |

The containers run like on OpenShift: arbitrary non-root UID in group 0, read-only root filesystem,
writable `/tmp` only, all capabilities dropped. Configuration: `config/docker.yaml`; overrides via `.env`
(see `.env.example`). Any other OpenAI-compatible endpoint works the same way: set `LLM_BASE_URL`,
`LLM_MODEL`, `LLM_API_KEY` in `.env` and check with `make llm-health`. Ollama and the Gemma download
are a developer-laptop convenience; in the bank the worker uses the internal inference gateway.

The compose build uses the same Dockerfile as production with `WHEELHOUSE_SOURCE=online` (packages from
`PIP_INDEX_URL`, default PyPI; point it at your mirror inside the bank). Production builds use
`make wheelhouse image` (offline, see [OpenShift deployment](docs/openshift-deployment.md)).

### Virtualenv workflow

```bash
make install     # .venv + project + dev tools
make db          # only PostgreSQL from compose
BRANCHOPS_CONFIG=config/local.yaml .venv/bin/branchops migrate
make run-api     # / make run-worker / make run-dashboard / make run-mcp  (config/local.yaml, mock model)
make check       # ruff + mypy --strict + all tests (unit + PostgreSQL integration)
```

## Commands

| Command | Purpose |
|---|---|
| `branchops api` | FastAPI app (`/api/ingest/{source}`, `/api/webhooks/jira`, read API, `/health/*`, `/metrics`) |
| `branchops worker` | Inbox worker; also drains the ticketing and notification outboxes and purges old attachments (`/healthz`, `/readyz`, `/metrics` on `worker.health_port`) |
| `branchops ownership check` | Validate the screen ownership registry (exit 1 on errors) |
| `branchops attachments purge` | Apply `retention.attachments_days` now |
| `branchops import --source csv <file>` | Import a CSV/XLSX/JSONL file through a file-based source (`--dry-run` validates only) |
| `branchops poll [--source id] [--once]` | Read pull sources on their schedule; one leader across replicas (advisory lock) |
| `branchops dashboard` | NiceGUI dashboard |
| `branchops mcp [--stdio]` | Read-only MCP server (streamable HTTP on `mcp.host:port/mcp`; `--stdio` for local development only) |
| `branchops migrate` | DB migrations, checkpoint tables, template publication (immutability check) |
| `branchops templates` | Validate and list templates |
| `branchops llm health\|probe` | Model gateway health / capability detection |
| `branchops replay <event-id>` | Requeue a dead-lettered inbox event |
| `branchops report --days 30` | Model-written operational summary (numbers grounded in SQL metrics) |

## Tests

```bash
make test-unit   # no database, no model
make test        # + PostgreSQL integration / API / end-to-end (uses compose postgres, branchops_test DB)
make test-llm    # optional smoke tests against the real internal endpoint
make lint typecheck
```

CI uses the mock provider; no model endpoint is needed. The PostgreSQL suite drops and recreates the
`public` schema of `TEST_DATABASE_URL` — always point it at a dedicated database.

## Documentation

* [Architecture](docs/architecture.md) — layering, processes, decisions
* [Agents](docs/agents.md) — logical agents, guardrails, adding capabilities
* [Incident sources](docs/sources.md) — source model, configuration, ServiceNow and CSV/XLSX adapters, adding a source
* [Jira integration](docs/jira-integration.md) — webhook setup, payload variants, ordering, retention
* [Templates](docs/templates.md) — template format and how to add one
* [Scoring](docs/scoring.md) — completeness/quality formulas and policies
* [Model integration](docs/model-integration.md) — gateway configuration, capabilities, fallbacks
* [MCP server](docs/mcp-server.md) — connecting MCP clients, team scopes, tool catalogue, audit
* [Ownership and routing](docs/ownership-and-routing.md) — teams/applications/screens, resolution order, notifications, dashboard how-to, CSV
* [Embedded agent guide](docs/embedded-agent-guide.md) — reporting from inside a bank application with app context and screenshots
* [Security](docs/security.md) — controls and threat model
* [OpenShift deployment](docs/openshift-deployment.md) — offline build and deployment
* [Evaluation](docs/evaluation.md) — test strategy, synthetic data, model evaluation

## Assumptions and limitations

* **Jira access is webhook-only.** Historical issues that never produced a webhook are not known to the
  system; there is no Jira backfill (a CSV export can be imported through a `csv_import` source instead). An issue first seen through a partial update without summary/description
  is stored but not analysed until a fuller payload arrives.
* **Model capabilities are configured, not guessed.** The default assumes JSON-object mode only;
  enable `structured_outputs` (JSON schema) after `branchops llm probe` confirms it. Tool calling is never
  used.
* **Similarity uses PostgreSQL full-text search** (`turkish` configuration) plus deterministic error
  signatures. pgvector / embedding retrieval is not implemented (the optional internal embedding model
  was not specified); the `HistoricalIssueReader` port is where it would plug in.
* **The mock provider is a development aid**, not a model: its classifications are keyword-based.
  Quality of real analyses depends on the internal model and must be evaluated with
  `docs/evaluation.md` before go-live.
* **The API never calls the model.** `/api/reports/summary` and the dashboard show deterministic
  report text; model-written narratives are produced by `branchops report` (worker-side credentials).
* **SSO** is delegated to an oauth2-proxy sidecar against the bank's OIDC IdP (groups → roles). Direct
  LDAP/Kerberos integration is not implemented.
* **Recurrence** requires an error code in the error details (signature = application + first code);
  incidents without codes are linked only through verified similarity relationships.
* An analysis persisted as `PARTIAL` after the model was unavailable on the last attempt is not retried
  automatically (its input hash is recorded); use manual re-analysis once the model is back.
* The dashboard keeps UI state per process; run one replica or rely on Route session affinity.
* Turkish is the primary data language; prompts are English with explicit instructions to keep
  values in the original language.

# Incident sources

BranchOps is source-agnostic: incidents can come from Jira, ServiceNow, spreadsheet/CSV imports and, with a
new adapter, any other system (another database, a mailbox, a future webhook). The design is **Ports &
Adapters**: every source adapter normalises its native payload into one canonical, source-neutral
`IncidentEvent`. Everything after the inbox (snapshot merge, LangGraph workflow, scoring, similarity,
dashboard) only sees that canonical shape and does not know which system a record came from.

```mermaid
flowchart LR
    subgraph push [Push sources]
        J[Jira webhook] --> R1[POST /api/ingest/jira<br/>legacy: /api/webhooks/jira]
        S[ServiceNow Business Rule] --> R2[POST /api/ingest/snow]
    end
    subgraph pull [Pull sources]
        F[CSV / XLSX / JSONL] --> I[branchops import]
        D[watch directory] --> P[branchops poll<br/>leader-elected]
    end
    R1 & R2 -->|adapter.authenticate + normalize| IN[(webhook_inbox<br/>source, idempotency key)]
    I & P -->|adapter.read_file / fetch| IN
    IN --> W[worker: registry.get(source).normalize → merge → workflow]
```

## Canonical model (`domain/entities/incident.py`)

| Contract | Notes |
|---|---|
| `IncidentEvent` | `source`, `external_key`, `external_id`, `event_type`, `raw_event_name`, `event_timestamp`, `source_updated_at`, `fields` (only attributes **present** in the payload), `mapped_fields` (canonical analysis fields such as `branch_code`), `comment`, `changed_fields`, `actor`, `group_key` (Jira project / ServiceNow assignment group / mailbox / import batch). |
| `IncidentSnapshot` | Best-known merged state: same attributes plus comments, tombstones and a per-attribute version vector. |
| `incident_key` | `"<source>:<external_key>"`, e.g. `jira:OPS-123`, `snow:INC0012345`, `csv:IMP-1001`. Primary key of `issues`; used by analyses, comments, relationships, the API and the dashboard. |
| `IncidentEventType` | `RECORD_CREATED`, `RECORD_UPDATED`, `RECORD_DELETED`, `COMMENT_CREATED`, `COMMENT_UPDATED`, `COMMENT_DELETED`, `MANUAL_REANALYSIS`, `UNSUPPORTED`. |

The merge rules (out-of-order safety, partial payloads, tombstones) are the same for every source; see
[Jira integration](jira-integration.md#asynchronous-processing-and-ordering). An adapter must therefore
always set `event_timestamp` or `source_updated_at`: an event without a version can never overwrite a value
that already has one.

**Backward compatibility (one release).** `domain/entities/jira.py` keeps `NormalizedJiraEvent`,
`JiraIssueSnapshot` and `JiraComment` as aliases, `JiraEventType` maps the old member names
(`ISSUE_CREATED` → `RECORD_CREATED`, …), the models still accept the old field names (`issue_key`,
`project_key`, `issue_fields`), `infrastructure/jira/adapter.py` re-exports the moved adapter, and the API /
dashboard accept a bare Jira key (`OPS-123` = `jira:OPS-123`). API responses keep `issue_key` (= the
external key), `project_key` and `jira_status` next to the new `incident_key`, `source`, `external_key`,
`group_key` and `source_status`.

## Ports (`application/interfaces/source_adapter.py`)

| Port | Methods | Used by |
|---|---|---|
| `PushSourceAdapter` | `source_id`, `authenticate(*, body, headers, query, client_host) -> mechanism` (raises `SourceAuthError`), `normalize(payload, transform=None)`, `idempotency_key(payload, headers)` | `POST /api/ingest/{source_id}` and the worker |
| `PullSourceAdapter` | `source_id`, `poll_interval_seconds`, `fetch(since, limit) -> Iterable[IncidentEvent]`, `normalize(envelope, transform=None)` | `branchops poll` and the worker |
| `FileImportSourceAdapter` | pull adapter + `read_file(path, *, is_known, imported_at)` | `branchops import` |
| `SourceRegistry` | `get`, `get_push`, `get_pull`, `push_sources()`, `pull_sources()` | built from `settings.sources` in `container.py` (only enabled sources) |

`transform` is the masking function: the API stores the **raw** payload, and the worker masks while
normalising, exactly as before. Pull sources store an *event envelope* (`{"incident_event": …}`) in the
inbox; their `normalize` decodes and masks it. The inbox idempotency key is
`"<source_id>:" + adapter key` (max. 200 characters); identical keys from different sources never collide.

## Configuration

```yaml
webhook:
  max_body_bytes: 1048576          # applies to every push source
sources:
  - id: jira                       # URL segment and incident-key prefix: [a-z][a-z0-9_-]{0,39}
    type: jira_webhook             # jira_webhook | servicenow_webhook | csv_import
    enabled: true
    auth: {mode: hmac_or_token, secrets_env: JIRA_WEBHOOK_SECRETS, token_header: X-BranchOps-Webhook-Token}
    allowed_groups: [OPS]          # formerly jira.allowed_projects; empty = all
    field_mapping: {branch_code: customfield_10101, application: customfield_10102}
  - id: snow
    type: servicenow_webhook
    auth: {mode: token, secrets_env: SERVICENOW_WEBHOOK_SECRETS}
    field_mapping: {branch_code: u_branch_code, application: cmdb_ci}
  - id: csv
    type: csv_import
    poll_interval_seconds: 300
    field_mapping: {summary: Konu, description: Açıklama, branch_code: Sube Kodu, application: Uygulama}
    options: {id_column: Kayıt No, watch_directory: /data/imports}
```

* **Legacy configuration.** Without a `sources` list, the old `jira:` (field mapping, allowed projects)
  and `webhook:` (auth settings) sections are translated into one `jira` source, so existing configuration
  files and `.env` files keep working unchanged. With no configuration at all, there is a single default
  Jira source, as in earlier releases.
* **Disabled or unknown sources** return `404` from `/api/ingest/{id}`; pull sources are not reachable
  over HTTP at all.
* **Production guard.** With `app.environment: production` every enabled push source must have a non-empty
  secret list in its `auth.secrets_env` variable, otherwise the process refuses to start.
* Changing a source id changes the identity of its records; treat ids as permanent.

## Adapters

### Jira (`infrastructure/sources/jira/`)

Unchanged behaviour; see [Jira integration](jira-integration.md).

### ServiceNow (`infrastructure/sources/servicenow/`) — reference adapter

A skeleton for a ServiceNow *Business Rule → outbound REST* integration on the `incident` table. The
expected payload and a sample Business Rule script are documented in the module docstring; adapt the
adapter to the payload the bank's Business Rule actually sends. Mapping: `number` → external key,
`sys_id` → external id, `short_description`/`description`/`state`/`priority`, `assignment_group` → group,
`caller_id`/`opened_by` → reporter, `opened_at`, `sys_updated_on` (version), `comments`/`work_notes`
journal entries → comments with stable ids, `field_mapping` columns → mapped fields. Reference values may
be plain strings or `{"value", "display_value"}` objects. Authentication is a shared token header;
idempotency key `sys_id:sys_updated_on:event`. Samples: `samples/servicenow/`.

### CSV / XLSX / JSONL (`infrastructure/sources/csv_import/`)

Each row is one `RECORD_CREATED` event (`RECORD_UPDATED` when the row has an id that is already known).

* **Identity.** With `options.id_column` the cell is the external key; otherwise the row is
  *content-addressed* (`sha256-<32 hex>`): re-importing an identical row is a no-op, an edited row becomes a
  new record. Prefer an id column whenever the export has one.
* **Columns.** `field_mapping` maps canonical names to headers; snapshot attributes (`summary`,
  `description`, `status`, `priority`, `reporter`, `group_key`, `created_at`, `labels`, `components`) go to
  the record, `updated_at` sets the version, everything else (`branch_code`, `application`, …) is a mapped
  analysis field. Headers match case- and Turkish-diacritics-insensitively (`Sube Kodu` = `ŞUBE KODU`).
  Empty cells never erase known values.
* **Dates** are ISO 8601 or `dd.mm.yyyy[ HH:MM[:SS]]` (configurable `datetime_formats`), naive values in
  `options.timezone` (default `Europe/Istanbul`).
* **XLSX** needs the optional dependency `openpyxl` (`pip install 'branchops-intelligence[xlsx]'`); it is not
  part of `requirements.lock`, so add it to the offline wheelhouse before enabling XLSX in OpenShift.

```bash
branchops import --source csv exports/branch_incidents.csv --dry-run   # parse + validate only
branchops import --source csv exports/branch_incidents.csv             # → inbox → worker
branchops poll --source csv --once                                      # scan options.watch_directory once
```

`branchops import` prints the per-outcome counts and writes a `source_import` audit record. Samples:
`samples/csv/`.

## Polling (`branchops poll`)

One poller process reads every enabled pull source every `poll_interval_seconds`. Run it as a single
Deployment (replicas may be > 1: standby replicas wait on `pg_try_advisory_lock` and take over when the
leader's connection drops). Health endpoints are the same as the worker's (`/healthz`, `/readyz`,
`/metrics` on `worker.health_port`).

The checkpoint (`source_checkpoints.cursor`) is the newest `event_timestamp` (falling back to
`source_updated_at`) of the events read; the next poll asks the adapter for events after it. Adapters
must not stop in the middle of a group of events that share a timestamp (the CSV adapter never splits a
file), and inbox idempotency makes re-reading harmless.

## Adding a source

1. Implement `PushSourceAdapter` or `PullSourceAdapter` under `infrastructure/sources/<name>/`: produce
   `IncidentEvent`s with only the attributes present in the payload, a timestamp, and free text passed
   through `transform`. Raise `SourcePayloadError` for unusable payloads (they are never retried).
2. Add the type to `SourceType` in `config/settings.py` (and to `PUSH_SOURCE_TYPES` for push sources) and
   construct it in `infrastructure/sources/__init__.py:build_adapter`.
3. Add samples under `samples/<source>/` and a unit test asserting the produced events.
4. Configure it under `sources:` — nothing downstream changes.

## Database (migration `0003_multi_source`)

* `issues`: primary key `incident_key`; new `source`, `external_key` (unique together); `issue_id` →
  `external_id`, `project_key` → `group_key`; `issue_key` stays as a non-null legacy column (= external key)
  for one release. Existing rows are backfilled as `jira:<issue_key>`, including stored snapshots.
* `analyses.issue_key`, `issue_comments.issue_key`, `issue_relationships.{source,target}_issue_key` →
  `*incident_key` (backfilled, foreign keys recreated); persisted `canonical_issue` / `similar_issues`
  documents are rewritten to the new keys.
* `webhook_inbox`: new `source` (default `jira`), `project_key` → `group_key`, index
  `(source, status, available_at)`; old `ISSUE_*` event types are renamed to `RECORD_*`.
* New table `source_checkpoints (source_id, cursor, updated_at)`.

The downgrade restores the previous schema and refuses to run while non-Jira records or pending non-Jira
inbox events exist.

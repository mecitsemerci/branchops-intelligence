# Architecture gap analysis

Audit of `main` at `cea81cc`, done by reading the code (not the docs). The baseline was green before any
change: `make lint typecheck test-unit` (283 passed, 1 skipped) and the PostgreSQL integration suite (44 passed,
3 skipped).

Each finding is either:

* **Confirmed**: a defect reproduced or proven from the code, with a concrete failure scenario.
* **Risk**: a weakness that depends on environment, data or configuration and has not been observed.

Severity:

* **P0**: data loss, a security exposure, or wrong numbers presented as correct.
* **P1**: degraded reliability or operability.
* **P2**: hygiene, or a missing capability.

The **Status** column records what this change set did with each finding (see the
[implementation report](implementation-report.md)).

## Summary

| ID | Area | Finding | Kind | Sev. | Status |
|---|---|---|---|---|---|
| EVAL-1 | Evaluation | No executable evaluation harness or labelled dataset; `docs/evaluation.md` describes a manual procedure only | Confirmed | P0 | Implemented |
| EVAL-2 | Evaluation | No record/replay provider: model results cannot be reproduced offline | Confirmed | P0 | Implemented |
| SCO-1 | Scoring | Generic-phrase guard matches only the whole text; a padded vague description passes and can keep a model-proposed GOOD | Confirmed | P0 | Implemented (policy `2026.10-2`) |
| SCO-2 | Scoring | Only the policy *version string* is stored; the credits behind an old version cannot be recovered | Confirmed | P1 | Implemented (policy history) |
| SCO-3 | Scoring | UNKNOWN is reported inconsistently: `missing_required_fields` mixes UNKNOWN with MISSING, `missing_fields` excludes it; analytics and notifications count only MISSING | Confirmed | P1 | Implemented |
| SCO-4 | Scoring | No calibration against expert assessments; no grade bands | Confirmed | P0 | Implemented |
| ING-1 | Ingestion | A non-object `changelog` raises `AttributeError` → HTTP 500 at ingest, and 5 wasted retries in the worker | Confirmed | P1 | Fixed |
| ING-2 | Ingestion | `X-Atlassian-Webhook-Identifier` takes precedence over the body digest but is not covered by the HMAC: a captured, signed delivery can be replayed with a new header value | Confirmed | P1 | Fixed |
| ING-3 | Ingestion | Deeply nested JSON (`RecursionError`) and `\u0000` (JSONB) produce HTTP 500 instead of 400 | Confirmed | P2 | Fixed |
| ING-4 | Ingestion | Jira Data Center < 10 has no webhook secret; the only option is the URL query token, which leaks into proxy access logs | Risk | P1 | Documented (DC compatibility matrix) |
| INB-1 | Inbox | Raw (unmasked) payload is kept for failed, dead-lettered and skipped-on-error events; no purge exists for `webhook_inbox` | Confirmed | P0 | Fixed (redaction on dead letter + `retention.inbox_days`) |
| INB-2 | Inbox | Reaper dead-letters without `processed_at`; replay keeps the stale `last_error`/`outcome` | Confirmed | P2 | Fixed |
| INB-3 | Inbox | `worker.shutdown_grace_seconds` is read nowhere; shutdown waits without bound, the pod is SIGKILLed and events stay locked until the 600 s lease expires and lose an attempt | Confirmed | P1 | Fixed |
| INB-4 | Inbox | Unexpected-error text (may include SQL parameters with incident text) is stored unmasked in `webhook_inbox.last_error` and `analyses.errors` | Confirmed | P1 | Fixed |
| INB-5 | Inbox | Missing integration tests: poison message, retry exhaustion through the runner, reaper dead-letter, per-issue busy lock, crash recovery through the dispatcher, nothing persisted on auth failure | Confirmed | P1 | Implemented |
| REC-1 | Recovery | A model outage longer than about 4 minutes (15/30/60/120 s backoff) yields a permanent NEEDS_REVIEW/PARTIAL analysis: the last attempt runs degraded and `latest_input_hash` then suppresses re-analysis; there is no recovery job | Confirmed | P0 | Implemented (recovery state machine) |
| REC-2 | Recovery | A later degraded analysis replaces a good latest analysis and nulls category/branch/application on the issue | Confirmed | P0 | Fixed |
| REC-3 | Recovery | 401/403/404 from the gateway (rotated key, wrong model name) are permanent `ModelRequestError`s: every analysis silently degrades on attempt 1 | Confirmed | P1 | Fixed (classified as access errors, retried) |
| REC-4 | Recovery | Stale RUNNING analysis rows after a worker crash are never closed | Confirmed | P2 | Fixed |
| REC-5 | Recovery | `StepStatus.DEGRADED` is never used; degraded steps lose their cause (transient vs permanent) | Confirmed | P2 | Fixed (`recoverable` flag) |
| SIM-1 | Similarity | Lexical only: Turkish synonyms ("para sayma makinesi" / "banknot sayım cihazı") never meet in full-text search | Confirmed | P1 | Implemented (domain lexicon + optional embeddings) |
| SIM-2 | Similarity | No POSSIBLE_COMMON_CAUSE relationship; RELATED is assigned by the model without any shared deterministic signal | Confirmed | P1 | Implemented |
| SIM-3 | Similarity | Similarity evidence quotes are not verified against the candidate texts | Confirmed | P1 | Fixed |
| SIM-4 | Similarity | Duplicate gate accepts *same subcategory* as the only shared attribute, so two "timeout" incidents in unrelated applications can become DUPLICATE_CANDIDATE | Confirmed | P1 | Fixed |
| WF-1 | Workflow | Fixed linear pipeline; no bounded historical investigation | Gap | P1 | Implemented (opt-in) |
| WF-2 | Workflow | No overall run budget (tokens / duration); only per-node deadlines | Risk | P2 | Implemented for the investigation step |
| SEC-1 | Security | Model-written text (recommendations, rationales) is stored without PII screening | Risk | P1 | Fixed |
| SEC-2 | Security | No per-model-call audit record (only aggregated step rows; correlation id only in logs) | Confirmed | P1 | Implemented (model audit log) |
| SEC-3 | Security | `verify_evidence` accepts a one-character quote when the stated source matches | Confirmed | P2 | Fixed |
| OPS-1 | Deployment | No LLM availability signal (readiness ignores the model, which is right, but there is no gauge or alert either) | Confirmed | P1 | Implemented (`branchops_model_up`, alert example) |
| OPS-2 | Deployment | Worker HPA scales on CPU, which barely moves for I/O-bound LLM work | Confirmed | P1 | Documented (queue-depth alternative); not changed |
| OPS-3 | Deployment | No preStop / termination grace on api, dashboard and mcp | Risk | P2 | Fixed |
| OPS-4 | Deployment | No CI pipeline in the repository | Confirmed | P2 | Recommended (blocked: CI system not specified) |
| OPS-5 | Deployment | Base image `:latest`, no digest pin | Risk | P2 | Documented |
| OPS-6 | Deployment | Manual `oc apply -k` can start pods before the migrate Job finishes; no expand/contract rule for rollbacks | Risk | P1 | Documented |
| ANA-1 | Analytics | No application-level trend, per-period branch trends or per-category missing fields | Gap | P2 | Implemented |
| ANA-2 | Analytics | Jira status / resolution history is not captured, so reopen and recurrence-after-resolution cannot be computed | Confirmed | P2 | Implemented (status history from the webhook payload) |
| RET-1 | Retention | No retention for `analyses`, `agent_steps`, `audit_log`, outboxes, LangGraph checkpoints of failed runs | Confirmed | P2 | Partly (inbox, failed checkpoints); rest recommended |
| ING-5 | Ingestion | *Found during this work:* purging inbox rows forgets idempotency keys, so a captured signed delivery could be re-submitted after retention | Confirmed | P1 | Fixed (fail-closed replay window `webhook.max_event_age_days` < `retention.inbox_days`) |
| SEC-4 | Security | *Found during this work:* investigation findings quote other incidents, and team-scoped callers must not receive them | Confirmed | P1 | Fixed (only unrestricted callers get the investigation) |

## Findings

### EVAL-1 / EVAL-2: no executable evaluation

* **Current implementation:** `docs/evaluation.md` describes a manual gold-set procedure. The tests use
  `MockModelProvider` (`infrastructure/llm/mock_provider.py`), which is keyword-based, and `ScriptedProvider`
  (`tests/helpers.py`). Each analysis stores `model_id`, `prompt_version`, `template_id/version`,
  `scoring_policy_version` and per-step tokens.
* **Limitation:**
  * There is no dataset format and no runner that computes classification or extraction metrics.
  * There is no way to record a real model's answers and replay them, so no two runs are comparable.
  * Model, template, scoring and similarity errors are not separated.
* **Operational impact:** the go-live decision for GLM-5.3-Flash (or any alternative) cannot be backed by
  numbers, and prompt or template changes cannot be regression-tested.
* **Recommendation:** an offline evaluation package (`src/branchops/evaluation/`), a JSON dataset format, a
  record/replay provider, and a JSON + Markdown report with run metadata and multi-model comparison.
* **Acceptance:**
  * Runs offline with no endpoint.
  * Byte-identical metrics when replaying the same recording.
  * The report carries model id, prompt version, template versions and scoring policy version.

### SCO-1: generic text keeps model-proposed quality

* **Current implementation:**
  * `templates/validation.py:validate_value` compares `reject_generic_phrases` with the *whole* normalised
    text.
  * The description only has `min_words: 12`.
  * `scoring/quality.py:validated_level` caps a level only on validation errors or the INSUFFICIENT state.
* **Limitation:** "Uygulama çalışmıyor." is caught by `min_words` only. A 12+ word description without any
  operational anchor (no code, number, application, device or time) can keep a model-proposed GOOD (credit
  1.0).
* **Impact:** quality scores overstate vague tickets whenever the model is lenient, which is exactly the
  failure the product exists to detect.
* **Recommendation:** a deterministic *specificity* check (anchors: digits, error codes, known application
  or device terms, time expressions). The level of a non-specific summary or description is capped at POOR
  under a new policy version. Historical analyses keep their version.
* **Acceptance:**
  * "Uygulama çalışmıyor." and padded vague text score below the specific "145 numaralı şubede …" example,
    even with a lenient model.
  * Existing scoring tests still pass.

### SCO-2: policy history

* **Current implementation:** `ScoringPolicy.version` (`scoring/policies.py`) is stored per analysis
  (`analyses.scoring_policy_version`). Only the active policy object exists in code.
* **Limitation:** after the numbers change, an old analysis cannot be explained.
* **Recommendation:** an immutable registry of every released policy (`POLICY_HISTORY`), with a unit test
  that the active policy equals its registered entry.

### SCO-3: UNKNOWN versus MISSING

* **Current implementation:**
  * `engine.py:score_issue` puts UNKNOWN fields in `unknown_fields` and also in `missing_required_fields`.
  * Analytics `most_missing_fields` (`application/queries/analytics.py`) and the notification "Eksik
    bilgiler" use only `missing_fields`.
* **Limitation:** "the model could not determine" (extraction failed, ambiguous text) and "the ticket
  does not contain it" are mixed in one list but separated in another.
* **Recommendation:** keep `missing_required_fields` for backward compatibility, but add explicit
  `missing_required_confirmed` and `unknown_required` lists. Analytics report MISSING and UNKNOWN as separate
  counts, and notifications list unknown required fields separately.

### SCO-4: calibration

* **Current implementation:** none. There is no grade concept beyond `low_quality_threshold` (50).
* **Recommendation:** `branchops.evaluation.calibration`:
  * expert-labelled reference assessments;
  * MAE, grade agreement, field-level rubric agreement (exact and weighted κ);
  * confusion matrices;
  * false high-/low-quality rates, broken down by category.
  * Grade bands are configuration of the calibration, not of the scoring policy.

### ING-1: non-object changelog

* **Current implementation:** `infrastructure/sources/jira/adapter.py` evaluates
  `(payload.get("changelog") or {}).get("items")`.
* **Failure:** `"changelog": ["x"]` raises `AttributeError`. `IngestIncidentEvent` only catches
  `SourcePayloadError`, so the API returns 500, Jira retries forever, and a stored event is retried five times.
* **Fix:** a type check that treats a non-object changelog as absent. Regression tests at both the unit and the
  API level.

### ING-2: delivery-id replay

* **Current implementation:** `JiraWebhookAdapter.idempotency_key` prefers `delivery:<X-Atlassian-Webhook-Identifier>`.
* **Failure:** a captured, validly signed request re-sent with a new identifier passes HMAC and gets a new
  idempotency key. The snapshot merge limits the damage but the event is analysed again.
* **Fix:** when the request was authenticated by signature, the key is derived from the signed body digest.
  The header is recorded only for tracing. Jira's own retries carry identical bodies, so they still collapse.

### ING-4: Jira Data Center compatibility

| Jira | Secret (HMAC) | Custom headers | Supported BranchOps mode |
|---|---|---|---|
| Cloud | yes (`X-Hub-Signature`) | no | `hmac` |
| Data Center ≥ 10 | yes | no | `hmac` |
| Data Center 8.x / 9.x | no | no | `token` via an internal reverse proxy that adds `X-BranchOps-Webhook-Token`, or as a last resort `allow_query_token` + CIDR allow-list |

The query token is visible in proxy logs, so prefer the reverse-proxy header. This is documented in
`docs/jira-integration.md`. A CIDR-only mode was deliberately not added, because source IP alone is not
authentication.

### INB-1: raw payload retention

* **Current implementation:**
  * `IngestIncidentEvent` stores the raw payload.
  * `EventProcessor._redacted` masks it only on successful completion (`inbox.complete`).
  * `InboxRepository.fail` and `_REAP_SQL` never redact, and nothing deletes inbox rows.
* **Impact:** PII from dead-lettered events stays in `webhook_inbox.payload` indefinitely, and the table and
  its unique index grow without bound.
* **Fix:**
  * Dead-lettering (by the runner or by the reaper) masks the payload when
    `retention.redact_inbox_payload_after_processing` is on. Replay still works, because masking keeps the
    JSON structure.
  * `retention.inbox_days` (default 30) purges finished rows (`COMPLETED`, `SKIPPED`, `DEAD_LETTER`).
* **Acceptance:** integration tests for redaction on dead letter and for the purge.

### INB-3: unbounded shutdown

* **Current implementation:** `Dispatcher.run_forever` calls `ThreadPoolExecutor.shutdown(wait=True)`.
  `shutdown_grace_seconds` is unused, and the worker pod grace period is 120 s.
* **Fix:** wait up to `shutdown_grace_seconds`, then release the leases of unfinished events back to PENDING
  without consuming an attempt. The stale thread's late `complete` is then rejected by the lease-owner guard.
* **Acceptance:** an integration test showing that a stuck event is immediately claimable after shutdown.

### INB-4: unmasked error text

* `processor.py` stores `f"{type(exc).__name__}: {exc}"` and `issue_analysis.py` stores `str(exc)`.
  SQLAlchemy errors include bound parameters. Both are now masked and bounded before storage.

### REC-1 to REC-5: model recovery

* **Current behaviour:**
  * The LangGraph retry (2 tries) and inbox retries (5 attempts, about 4 minutes in total) run first.
  * The last attempt then runs with `allow_degraded`, which persists NEEDS_REVIEW/PARTIAL and completes the
    event.
  * `IssueRepository.latest_input_hash` treats that degraded analysis as current, so an unchanged webhook
    never re-analyses.
  * `AnalysisRepository._update_issue` replaces the latest pointer by `started_at` alone, so the degraded
    result also replaces a good COMPLETED one and nulls the classification columns.
* **Fix:**
  * A `recoverable` flag per analysis: true when a step degraded because the model was unavailable or the
    deadline was exceeded.
  * A recovery state machine in the new table `analysis_recovery`:

    ```
    RECOVERY_PENDING → RUNNING → COMPLETED
                      ↘ RETRY_WAIT → RUNNING
                      ↘ FAILED_PERMANENT
    ```

  * A scheduled `RecoveryScheduler` in the worker, plus `branchops recover` for manual runs. It enqueues
    bounded, idempotent `RECOVERY` inbox events, only after a successful model health probe, with
    exponential backoff and a per-tick cap.
  * The latest pointer is not replaced by a recoverable degraded result of the same input, nor by a result of
    an older issue version.
  * 401/403/404 are mapped to a retryable `ModelAccessError`.
  * Stale RUNNING rows are closed as FAILED by the reaper.
* **Acceptance:** an outage test where the analysis degrades and, after the model recovers, the scheduler
  produces a COMPLETED analysis. Plus tests for bounded attempts, idempotent enqueue, and no overwrite of
  newer results.

### SIM-1 to SIM-4: similarity

* **Current implementation:**
  * `IssueRepository.find_candidates`: `plainto_tsquery('turkish')` OR-ed, plus the error signature.
  * `agents/similarity.py:shortlist` and `passes_duplicate_gate`.
  * The `SimilarityAgent.verify` verdicts are DUPLICATE_CANDIDATE, RELATED or UNRELATED. Evidence is taken
    as-is.
* **Fix:**
  * A versioned Turkish domain lexicon expands the query with synonyms (deterministic, no download).
  * An optional `EmbeddingProvider` port with an OpenAI-compatible internal endpoint (`similarity.embeddings`,
    off by default, URL-guarded). Vectors are stored in `issue_embeddings` (a `REAL[]` column, no pgvector
    required). Semantic hits are added to the lexical candidates with a `semantic_score` and the
    `semantic_match` signal. Ranking is still done by the deterministic shortlist, not by score fusion.
  * The new relationship POSSIBLE_COMMON_CAUSE. A RELATED verdict without any shared application, signature
    or branch is downgraded to POSSIBLE_COMMON_CAUSE.
  * The duplicate gate needs the same application or the same error signature; same subcategory is no longer
    enough.
  * Evidence quotes are verified against the current issue and the candidate excerpt, and unverified ones are
    marked.
  * Nothing ever merges issues.
* **Embeddings:** GLM-5.3-Flash must not be used as the embedding model. The setting requires a separate
  `embeddings.model`.

### WF-1: adaptive investigation

* A conditional `investigate` node runs after similarity only when `workflow.investigation.enabled` is set and
  at least one verified related/duplicate candidate or recurring signature exists.
* Allow-listed, read-only, typed tools sit behind the `InvestigationReader` port: `search_history`,
  `get_issue_summary`, `compare_error_signatures`, `known_resolutions`, `issue_metadata`. All are
  parameterised repository queries; the model never sees SQL.
* Bounded by tool calls, duration, token budget and depth.
* Conclusions without verified evidence are labelled HYPOTHESIS.

### SEC-1 to SEC-3: data governance

* **Masking before the LLM:** confirmed. `adapter.normalize(..., transform=masker.mask)` runs before the
  snapshot is built.
* **Gaps fixed:**
  * Model-written text is masked before storage.
  * Every model call writes a content-free audit log record: correlation id, agent, model, tokens, latency,
    prompt sha256, outcome.
  * Quotes must be at least 3 characters to verify.
  * Dead-letter payloads are redacted (INB-1).
  * Stored error text is masked (INB-4).
* **Unchanged by design:**
  * Raw model output and reasoning are never stored: `strip_reasoning` drops `<think>` blocks and
    `reasoning_content` is never read.
  * TLS verification is never disabled, and the internal CA bundle is used for every outbound client.

### OPS-1 to OPS-6: deployment

* Readiness of api, worker, dashboard and mcp depends on the database only, which is correct: an LLM
  outage does not stop ingestion.
* New: the worker probes the model periodically and exports `branchops_model_up`, and the recovery scheduler
  uses that probe.
* An example PrometheusRule (`deploy/openshift/examples/prometheusrule.yaml`) alerts on model down, dead
  letters, recovery backlog and inbox age.
* preStop sleeps and termination grace periods were added to the api, dashboard and mcp pods.
* HPA on queue depth needs a custom-metrics adapter, which is not part of the manifests; this is documented.
* Offline build, mirrors, CA, migration ordering, expand/contract rollback and sizing are documented in
  `docs/openshift-deployment.md`.
* Nothing was deployed to an OpenShift cluster during this work. Only `kustomize build` was run.

### ANA-1 / ANA-2: analytics

* **Existing:**
  * `quality_analytics` (by category, by branch, most missing, first→last improvements, recurring
    signatures);
  * `recurring_errors`;
  * a daily overview trend.
* **Added:**
  * application- and branch-level weekly trends;
  * missing and unknown fields per category;
  * quality change after source updates;
  * reopen and recurrence-after-resolution, based on the Jira status and resolution date captured from the
    webhook payload into `issue_status_events`.
* Where no status history exists, the metrics are reported as `UNKNOWN`, with coverage counts, instead of
  zero. Issue counts are never presented as business impact.

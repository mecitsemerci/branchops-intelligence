# Implementation report: hardening and evaluation

Branch `feat/hardening-evaluation`, based on `main` at `cea81cc`. The audit behind this work is in the
[architecture gap analysis](architecture-gap-analysis.md). The architecture is unchanged: Python only,
on-premise and air-gapped, the same processes and the same database. Nothing was rewritten, and every
change is additive or replaces only the defective behaviour.

## Test results

| Gate | Python 3.12 (project) | Python 3.13 |
|---|---|---|
| `ruff check` + `ruff format --check` | pass | pass |
| `mypy --strict` (190 files) | pass | pass |
| Unit tests | 358 passed, 1 skipped | 358 passed, 1 skipped |
| PostgreSQL integration tests | 66 passed, 3 skipped | 66 passed, 3 skipped |
| `kubectl kustomize` base + production overlay | renders | — |

The baseline before the changes was 283 unit tests and 44 integration tests.

Skipped tests, and why:

* **`tests/integration/test_llm_endpoint.py` (3).** They need a real internal model endpoint
  (`BRANCHOPS_LLM_INTEGRATION=1`), and none is reachable from the development environment.
* **`tests/unit/test_source_adapters.py` (1, XLSX).** The optional `openpyxl` package is not installed in
  the development virtualenv.

## 1. Implemented and tested

| Area | What | Main files | Tests |
|---|---|---|---|
| Evaluation framework (P0) | Dataset format; production workflow run offline; metering, recording and replay providers; metrics (classification accuracy/macro F1, extraction P/R and value accuracy, unsupported-field rate, missing-field P/R with UNKNOWN apart, rubric agreement/κ, structured-output validity, similarity recall/precision/FP, duplicate P/R, false merge rate, latency, tokens); error attribution model/template/scoring/similarity; JSON and Markdown reports; multi-model comparison; `branchops eval`; `make eval`; 26-case synthetic Turkish dataset | `src/branchops/evaluation/`, `scripts/build_eval_dataset.py`, `samples/evaluation/` | `tests/unit/test_evaluation.py` (incl. byte-identical metrics across record → replay → replay) |
| Score calibration (P0) | MAE, signed error, grade agreement and confusion, field rubric agreement/κ, false high/low quality rates, by category; rule-only calibration (expert inputs through the engine) vs. system calibration | `evaluation/calibration.py` | `tests/unit/test_calibration.py` |
| Generic text (P0) | Deterministic specificity check; policy `2026.10-2` caps non-specific summary/description at POOR, so a lenient model can no longer lift "Uygulama çalışmıyor." | `domain/services/specificity.py`, `scoring/` | `test_calibration.py` (lenient model end to end) |
| Policy history | `POLICY_HISTORY`; a pinned old version reproduces its exact parameters; changing a released version is refused at startup | `scoring/policies.py`, `config/settings.py` | `test_calibration.py` |
| UNKNOWN ≠ MISSING | `missing_required_confirmed` / `unknown_required`; analytics and notifications count them apart | `scoring/engine.py`, `queries/analytics.py`, notifications | `test_calibration.py`, integration |
| Webhook security (P0) | Signed deliveries deduplicated by the signed body (header-swap replay closed); fail-closed replay window; malformed changelog / NUL / deep JSON → 400 instead of 500; rejected requests persist nothing; Jira DC compatibility matrix | `commands/ingest_webhook.py`, `sources/*/adapter.py` | `tests/integration/test_ingestion_reliability.py`, `test_jira_and_security.py` |
| Inbox reliability (P0) | Dead letters keep only masked payloads; reaper sets `processed_at` and redacts; replay clears the stale outcome; stored errors masked and parameter dumps cut; graceful drain honours `shutdown_grace_seconds` and hands events back without consuming attempts; inbox and checkpoint retention | `inbox_repository.py`, `workers/` | same file (poison message, retry exhaustion, reaper, crash recovery, per-issue lock, drain, retention) |
| Model recovery (P1) | `DEGRADED` steps and `recoverable` analyses; 401/403/404 retried as access errors; latest-analysis protection (no provisional or older overwrite); `analysis_recovery` state machine; scheduler with health probe, backoff, batch cap, duplicate-execution guard; stale RUNNING reaper; `branchops recover`; metrics | `persistence/recovery_repository.py`, `workers/recovery.py`, `analysis_repository.py` | `tests/integration/test_model_recovery.py`, `tests/unit/test_recovery.py` |
| Hybrid similarity (P1) | Exact text, signature and specific error-code signals; Turkish domain lexicon in FTS; optional internal embeddings (`REAL[]`, fallback to FTS); `POSSIBLE_COMMON_CAUSE`; stricter duplicate gate (hard negatives); evidence verified against candidate texts | `agents/similarity.py`, `domain/services/lexicon.py`, `persistence/embedding_repository.py`, `llm/embeddings.py` | `tests/unit/test_similarity_hybrid.py`, `tests/integration/test_similarity_retrieval.py` |
| Adaptive investigation (P1) | Optional `investigate` step: allow-listed read-only typed tools; bounded by calls, depth, time, tokens and cancellation; evidence-checked findings (HYPOTHESIS otherwise); masked and scoped | `agents/investigation.py`, `persistence/investigation_repository.py` | `tests/unit/test_investigation.py`, `tests/integration/test_investigation_db.py` |
| Security (P1) | Model-written text and all evidence quotes masked before storage; content-free per-call model audit log; 3-character minimum for verified quotes; investigation hidden from team-scoped callers | `workflows/issue_analysis.py`, `llm/audit.py`, `domain/services/evidence.py`, `queries/issues.py` | `test_recovery.py`, `test_investigation.py`, integration |
| OpenShift (P1) | preStop and grace periods; manifest policy tests (restricted SCC fields, resources, no GPU, probes, readiness independent of the model, worker grace > drain); `branchops_model_up`; example PrometheusRule | `deploy/openshift/`, `cli.py` | `tests/unit/test_deploy_manifests.py` |
| Operational analytics (P2) | Status history; weekly trends by application, branch and category; missing/unknown per category; quality after updates; reopen and recurrence after resolution, UNKNOWN when there is no data; impact disclaimer | `queries/operations.py`, `issue_repository.py` | `tests/integration/test_operational_analytics.py` |

Migrations `0008`–`0011` are additive only (expand/contract). The previous image runs unchanged against the
new schema.

## 2. Implemented but not integration-tested

* **Live model evaluation (`branchops eval run --provider live`).** The command path is tested with the mock
  and replay providers only. **No GLM-5.3-Flash (or other model) accuracy has been measured.** All published
  numbers come from the keyword mock provider and describe the harness, not a model.
* **Internal embedding endpoint.** `OpenAICompatibleEmbeddingProvider` is tested with an HTTP mock transport.
  Retrieval quality with a real internal embedding model is unmeasured.
* **Dashboard sections** (investigation findings, "Operasyonel eğilimler"). They are built from tested query
  functions, but were not exercised in a browser session.
* **OpenShift behaviour.** It is checked by manifest rendering and policy tests only. No deployment to a
  cluster was made, so SCC admission, preStop timing, oauth2-proxy shutdown and the CA bundle injection were
  not observed.
* **Recovery and model-health tasks inside a running worker process.** The scheduler and probe are tested
  directly; the periodic-task wiring in `branchops worker` is not covered by an automated test.

## 3. Recommended but not implemented

* **Queue-depth autoscaling for workers.** It needs a custom-metrics adapter or KEDA on the platform. CPU HPA
  is unchanged; see [deployment](openshift-deployment.md#resources-probes-scaling).
* **Retention jobs** for `analyses`, `agent_steps`, `audit_log` and the outboxes, according to the bank's
  retention policy (SEC/RET-1).
* **Persisting model-call audit records** in a database table. They are currently structured log records
  only, retained by the logging stack.
* **Real expert annotations.** Replace the synthetic expert scores with two-annotator labels of real, masked
  tickets, and measure inter-annotator agreement. Template weights and policy credits were deliberately *not*
  changed: synthetic labels are not evidence.
* **CIDR-only webhook mode for very old Jira DC.** Deliberately not added; use the reverse-proxy header mode.
* **Status category from Jira** (`statusCategory.key`). It would make resolved detection independent of
  status names; currently the names in `analytics.resolved_statuses` are used.
* **MCP exposure of operational analytics.** The REST API and dashboard have it; MCP tools were not extended.

## 4. Blocked by missing infrastructure

* **CI pipeline (OPS-4).** The repository has no CI system configured and the target (GitLab, Jenkins, Tekton)
  is not specified. `make check` is the gate to run in whatever pipeline the bank uses.
* **Model accuracy for GLM-5.3-Flash and alternatives.** Needs the internal gateway; run
  `branchops eval run --provider live --record …` inside the bank network.
* **Embedding retrieval quality.** Needs an approved internal embedding endpoint.
* **OpenShift verification.** Needs a staging namespace with the bank's registry, CA and SSO.
* **XLSX import test.** Needs the optional `openpyxl` wheel in the environment.

# Evaluation

## Automated test suite

| Layer | Tests | Needs |
|---|---|---|
| Scoring rules | `tests/unit/test_scoring.py` | — |
| Templates, conditions, validation | `tests/unit/test_templates.py` | — |
| Masking, injection detection, evidence, signatures, out-of-order merge | `tests/unit/test_domain_services.py` | — |
| Jira payload adapter, webhook auth, URL guard, RBAC, settings guards | `tests/unit/test_jira_and_security.py` | — |
| Structured output (repair, mode fallback), agent guards | `tests/unit/test_agents.py` | — |
| OpenAI-compatible provider (request shape, error mapping, probe, TLS) | `tests/unit/test_openai_provider.py` | — (mock HTTP transport) |
| Workflow (complete / incomplete / generic / missing branch / missing app / PII / injection / similarity / outage / checkpoint resume) | `tests/unit/test_workflow.py` | — |
| Air-gap assertions | `tests/unit/test_airgap.py` | — |
| Inbox: idempotency, SKIP LOCKED concurrency, per-issue exclusivity, backoff, dead-letter, replay, lease expiry/renewal | `tests/integration/test_inbox.py` | PostgreSQL |
| End-to-end: API validation, vertical slice, partial/out-of-order/comment events, deletion policies, outage → degraded, RBAC, template immutability, Postgres checkpoint resume | `tests/integration/test_end_to_end.py` | PostgreSQL |
| Evaluation framework (metrics, dataset, record/replay, attribution) | `tests/unit/test_evaluation.py` | — |
| Score calibration (generic vs specific, policy history, UNKNOWN vs MISSING) | `tests/unit/test_calibration.py` | — |
| Real model smoke tests | `tests/integration/test_llm_endpoint.py` | internal LLM (`BRANCHOPS_LLM_INTEGRATION=1`) |

All automated tests use the mock or scripted provider; none needs a model endpoint.

## Synthetic Turkish Jira data (`samples/jira/`)

| File | Scenario |
|---|---|
| `complete_incident.json` | EFT timeout with branch, app, version, error code, time, steps, terminal, troubleshooting, impact, evidence |
| `incomplete_incident.json` | card reader issue with few details |
| `generic_description.json` | "Sistem çalışmıyor acil bakılsın." |
| `duplicate_issue.json` | same branch/app/error 20 minutes after the complete incident |
| `related_different_issue.json` | same error code, different branch, two days later (FAST) |
| `missing_branch_code.json` | detailed incident without branch code |
| `missing_application.json` | HTTP 500 without application name |
| `partial_update.json` | update carrying only `description` (+ changelog) |
| `stale_update.json` | older update delivered after a newer one |
| `comment_created.json` / `comment_deleted.json` | comment lifecycle |
| `issue_deleted.json` | deletion (retention policies) |
| `prompt_injection.json` | Turkish/English instructions and chat-template tokens inside the description |
| `cloud_adf_issue.json` | Jira Cloud ADF description |
| `pii_incident.json` | TCKN, IBAN, card number, phone number in the text |

"Duplicate webhook" is tested by sending the same payload twice (body digest) and with the same
`X-Atlassian-Webhook-Identifier`.

## Evaluation framework (`branchops eval`)

`src/branchops/evaluation/` runs the **production** workflow over a labelled dataset offline: the same agents,
prompts, templates and scoring engine. Only the persistence and history ports are replaced by in-memory
implementations. No endpoint is needed unless you choose to run against a model.

```bash
# CI / laptop: keyword mock provider, no network
branchops eval run --dataset samples/evaluation/turkish-branch-incidents.json --out eval-results

# Internal model, recording every answer (explicit: needs llm.provider openai-compatible)
branchops eval run --dataset ... --provider live --record eval-results/glm.rec.json --label glm-5.3-flash
branchops eval run --dataset ... --provider live --model qwen3-32b-instruct --record eval-results/qwen.rec.json

# Reproduce offline, byte-identical metrics, no endpoint
branchops eval run --dataset ... --provider replay --recording eval-results/glm.rec.json --label glm-replay

# Compare two or more models on the same dataset
branchops eval compare eval-results/evaluation-glm-5.3-flash.json eval-results/evaluation-qwen3-32b-instruct.json

# Score calibration against expert assessments (add --recording to include a model run)
branchops eval calibrate --dataset ... --recording eval-results/glm.rec.json --out eval-results/calibration.json
```

**Dataset format** (`evaluation/dataset.py`). One JSON file. Each case holds:

* the original Jira webhook payload;
* the expected category, subcategory and template;
* the expected extracted field values (substring match);
* the fields that are genuinely absent (confirmed MISSING);
* the expected rubric levels;
* the expected relationships to earlier cases (DUPLICATE / RELATED / POSSIBLE_COMMON_CAUSE / UNRELATED);
* whether a prompt injection should be flagged;
* optionally, an expert assessment (quality score, grade, field states).

`samples/evaluation/turkish-branch-incidents.json` is generated by `scripts/build_eval_dataset.py`. It contains
26 synthetic cases: application freeze, money counter connection, printer, scanner, LDAP, transaction timeout,
network, integration, missing branch, ambiguous, similar-but-unrelated, Turkish synonyms and prompt injection
(plus a benign "talimat" control).

**Metrics** (JSON report plus Markdown):

| Group | Metrics |
|---|---|
| Classification | accuracy, macro F1, subcategory and template accuracy |
| Extraction | precision, recall, value accuracy, unsupported field generation rate (a value for a field the record does not contain) |
| Missing fields | precision and recall of confirmed MISSING; UNKNOWN where MISSING was expected is reported apart |
| Rubric levels | agreement and linearly weighted κ |
| Structured output | validity rate, first-pass validity, invalid outputs, model errors |
| Similarity | retrieval recall, relationship precision, false-positive rate, duplicate precision/recall, false merge rate |
| Operations | model latency per case and per call (p50/p95), token consumption |

**Error attribution.** Each disagreement is assigned to the layer that caused it:

* `model`: classification, extraction or rubric level;
* `template`: wrong template although the category is right, or validation rejected a correct value;
* `scoring`: the engine, fed the *expert's* states and levels, disagrees with the expert's grade, which is
  independent of the model;
* `similarity`: not retrieved, wrong relationship, or false duplicate.

**Reproducibility.**

* Every report carries the model id, prompt version, template versions, scoring policy version, lexicon
  version and the dataset hash.
* A recording stores model *outputs* keyed by case and schema, never prompts.
* Replay is strict: a request that is not in the recording fails the case instead of calling a model.
* `tests/unit/test_evaluation.py` asserts identical metrics across record → replay → replay.

**Limits.**

* The in-memory history ranks candidates by token overlap, not `ts_rank_cd`. Retrieval recall measures the
  candidate logic (lexicon expansion, signatures, shortlist), not PostgreSQL ranking.
* Mock-provider numbers measure the harness, not a model. **No GLM-5.3-Flash accuracy has been measured in
  this repository.** That needs a live run inside the bank network.

### Score calibration

`evaluation/calibration.py` compares scores with expert reference assessments. Two comparisons are made:

* **`scoring_rules`**: the engine scores the expert's own field states and levels. A deviation here is caused
  by template weights or policy credits, never by the model.
* **`system`**: the scores of a (replayed) run.

Each comparison reports:

* MAE and mean signed error;
* grade agreement and the LOW/MEDIUM/HIGH confusion matrix (bands: LOW < `low_quality_threshold` ≤ MEDIUM <
  75 ≤ HIGH, configurable with `--high`);
* field-level rubric agreement and κ, plus the rubric confusion matrix;
* the false high-quality rate (expert below threshold, system at or above);
* the false low-quality rate;
* a breakdown by category;
* the ten largest deviations.

Grade bands are a reporting convention and never change stored scores.

The synthetic expert scores in the shipped dataset are illustrative. With them, the engine scores detailed
records about 15 points below the "expert" (mean signed error −14.5 on 14 cases). Do **not** change template
weights or policy credits based on synthetic labels. Replace them with two-annotator labels of real, masked
tickets and check inter-annotator agreement first. If `scoring_rules` still shows a systematic deviation,
publish new template versions or a new policy version (see [scoring](scoring.md#5-policies)).

## Evaluating the internal model before go-live

Scores are deterministic *given* the extracted fields and levels, so model evaluation focuses on the
three model judgements. Recommended procedure:

1. **Build a gold set** of 150–300 real, masked tickets per major category, annotated by two branch
   operations experts: category/subcategory, per-field presence (value + quote), per-field rubric level,
   and pairs of related/duplicate tickets. Measure inter-annotator agreement (Cohen's κ) first.
2. **Run the pipeline offline** against the gold set with the production configuration (same templates,
   policy and prompt version), e.g. by replaying the tickets as webhooks into a staging namespace.
3. **Metrics**

   | Judgement | Metric | Suggested acceptance |
   |---|---|---|
   | Classification | macro-F1 over categories, accuracy on subcategory | F1 ≥ 0.85 |
   | Extraction presence | per-field precision/recall of SUFFICIENT/INSUFFICIENT vs MISSING | precision ≥ 0.9 (hallucinations are worse than misses) |
   | Hallucination rate | share of extracted values not supported by the text (should be ~0 after evidence verification) | ≤ 1 % |
   | Quality levels | weighted κ vs annotators; mean absolute difference of the resulting quality score | κ ≥ 0.6, MAE ≤ 10 points |
   | Similarity | precision@5 of RELATED/DUPLICATE verdicts | ≥ 0.8 |
   | Robustness | injection set: classification unchanged and `NEEDS_REVIEW` raised | 100 % flagged |
   | Operations | structured-output failure rate, p95 latency per step, tokens per analysis | failure < 2 %, p95 < 30 s/step |

4. **Stability**: run the gold set three times; temperature 0 is not guaranteed deterministic, so report
   the variance of quality scores per ticket (target: standard deviation ≤ 5 points).
5. **Record** the model id, gateway version, prompt version, template versions and scoring policy
   version with the results; re-run whenever any of them changes (they are part of each analysis' input
   hash, so production re-analysis is explicit).

The data for these metrics is already stored per analysis (`analyses`, `agent_steps`), so the same
queries can be used for continuous monitoring after go-live, together with the Agent Executions page.

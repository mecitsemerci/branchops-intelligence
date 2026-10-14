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

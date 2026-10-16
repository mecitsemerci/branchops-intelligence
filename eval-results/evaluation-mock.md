# BranchOps evaluation

| | mock |
|---|---|
| model_id | mock-rule-based |
| prompt_version | 2026.10-3 |
| scoring_policy_version | 2026.10-2 |
| lexicon_version | 2026.10-1 |
| dataset | turkish-branch-incidents v1 (26 cases) |
| templates | application-incident@1.0.0, authentication-incident@1.0.0, generic-operational-incident@1.0.0, integration-incident@1.0.0, network-incident@1.0.0, peripheral-device-incident@1.0.0, transaction-incident@1.0.0 |

## Metrics

| | mock |
|---|---|
| Classification accuracy | 0.958 |
| Classification macro F1 | 0.964 |
| Subcategory accuracy | 0.909 |
| Template accuracy | 0.958 |
| Extraction precision | 0.939 |
| Extraction recall | 0.767 |
| Extracted value accuracy | 0.946 |
| Unsupported field generation rate | 0.032 |
| Missing-field precision | 0.849 |
| Missing-field recall | 0.957 |
| UNKNOWN where MISSING expected | 0 |
| Rubric level agreement | 0.765 |
| Rubric weighted κ | 0.713 |
| Structured output validity | 1 |
| First-pass validity | 1 |
| Similarity retrieval recall | 1 |
| Relationship precision | 0.8 |
| Relationship false-positive rate | 0 |
| Duplicate precision | 1 |
| Duplicate recall | 0.5 |
| False merge rate | 0 |
| Links to unlabelled cases (not scored) | 9 |
| Injection detection accuracy | 1 |
| Model latency per case, mean (ms) | 5 |
| Model latency per call, p95 (ms) | 1 |
| Tokens per case, mean | 7977.9 |
| Tokens, total | 207425 |

## Errors by layer

| | mock |
|---|---|
| model | 50 |
| scoring | 6 |
| similarity | 1 |

## Errors by scenario tag

| Tag | Cases | With errors | Errors by layer |
|---|---|---|---|
| ambiguous | 4 | 1 | model 1 |
| application_freeze | 3 | 2 | model 7, scoring 1 |
| duplicate | 2 | 2 | model 3, similarity 1 |
| generic_description | 2 | 0 | – |
| hard_negative | 1 | 1 | model 2 |
| integration | 2 | 1 | model 4, scoring 1 |
| ldap_authentication | 1 | 1 | model 2 |
| missing_branch | 4 | 2 | model 2 |
| money_counter | 2 | 2 | model 6, scoring 1 |
| network | 2 | 1 | model 3, scoring 1 |
| printer | 4 | 4 | model 7 |
| prompt_injection | 3 | 2 | model 2 |
| scanner | 1 | 1 | model 5, scoring 1 |
| similar_unrelated | 5 | 3 | model 5 |
| specific_reference | 1 | 1 | model 6, scoring 1 |
| transaction_timeout | 4 | 4 | model 13, scoring 1, similarity 1 |
| turkish_synonyms | 1 | 1 | model 1 |

## Case details

| Case | Status | Category | Quality | Errors |
|---|---|---|---|---|
| app-freeze-detailed | COMPLETED | TRANSACTION | 66.8 | model.classification; model.template_selection; model.extraction_wrong_value(business_impact); model.extraction_missed(reproduction_steps); model.extraction_missed(troubleshooting_steps); model.rubric_level(summary); scoring.grade_disagreement |
| app-freeze-vague | COMPLETED | APPLICATION | 3.49 | – |
| money-counter-connection | COMPLETED | PERIPHERAL_DEVICE | 58.06 | model.extraction_wrong_value(application); model.extraction_wrong_value(error_details); model.extraction_missed(frequency); model.extraction_missed(environment); model.rubric_level(summary); scoring.grade_disagreement |
| money-counter-synonym-duplicate | COMPLETED | PERIPHERAL_DEVICE | 51.18 | model.extraction_missed(application) |
| printer-paper-jam | COMPLETED | PERIPHERAL_DEVICE | 48.82 | model.extraction_missed(frequency); model.extraction_missed(environment); model.rubric_level(summary) |
| scanner-failure | COMPLETED | PERIPHERAL_DEVICE | 57.63 | model.extraction_wrong_value(business_impact); model.extraction_missed(frequency); model.extraction_missed(environment); model.extraction_missed(troubleshooting_steps); model.rubric_level(summary); scoring.grade_disagreement |
| ldap-login-failure | COMPLETED | AUTHENTICATION | 51.72 | model.extraction_missed(application); model.rubric_level(summary) |
| cash-deposit-host-timeout | COMPLETED | TRANSACTION | 56.49 | model.extraction_missed(branch_code); model.extraction_wrong_value(application); model.extraction_missed(application_version); model.extraction_missed(frequency); model.rubric_level(summary); model.rubric_level(branch_code); scoring.grade_disagreement |
| generic-not-working | COMPLETED | OTHER | 3.61 | – |
| padded-vague-description | COMPLETED | OTHER | 12.5 | – |
| eft-gateway-timeout | COMPLETED | TRANSACTION | 48.66 | model.extraction_missed(application); model.extraction_missed(application_version); model.extraction_missed(frequency) |
| report-timeout-unrelated | COMPLETED | APPLICATION | 51.18 | model.subcategory; model.extraction_unsupported(business_process) |
| network-outage | COMPLETED | NETWORK | 28.97 | model.extraction_missed(business_impact); model.extraction_missed(occurrence_time); model.extraction_missed(environment); scoring.grade_disagreement |
| eft-timeout-duplicate | COMPLETED | TRANSACTION | 42.06 | model.extraction_missed(branch_code); model.extraction_missed(application); similarity.relationship |
| network-slowness-neighbour | COMPLETED | NETWORK | 31.52 | – |
| core-banking-integration | COMPLETED | INTEGRATION | 46.94 | model.extraction_missed(application); model.extraction_unsupported(business_impact); model.extraction_missed(frequency); model.rubric_level(summary); scoring.grade_disagreement |
| kps-integration | COMPLETED | INTEGRATION | 43.47 | – |
| loan-screen-missing-branch | COMPLETED | TRANSACTION | 31.75 | model.extraction_missed(troubleshooting_steps) |
| passbook-missing-branch | COMPLETED | PERIPHERAL_DEVICE | 15.7 | model.extraction_missed(troubleshooting_steps) |
| ambiguous-screen-warning | COMPLETED | APPLICATION | 3.49 | – |
| ambiguous-card-problem | COMPLETED | OTHER | 8.94 | model.extraction_unsupported(business_impact) |
| printer-same-error-other-branch | COMPLETED | PERIPHERAL_DEVICE | 43.82 | model.extraction_unsupported(business_process); model.extraction_missed(environment) |
| injection-card-reader | NEEDS_REVIEW | PERIPHERAL_DEVICE | 41.94 | model.extraction_unsupported(business_impact) |
| injection-english-scanner | NEEDS_REVIEW | PERIPHERAL_DEVICE | 20.43 | – |
| benign-instruction-word | COMPLETED | PERIPHERAL_DEVICE | 44.57 | model.extraction_missed(frequency) |
| app-freeze-other-branch | COMPLETED | APPLICATION | 54.46 | model.extraction_unsupported(business_impact) |

Model accuracy shown here is only as good as the provider used: the mock provider is keyword-based and its numbers say nothing about GLM-5.3-Flash or any other model.

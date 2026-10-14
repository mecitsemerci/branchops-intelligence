# Templates

Templates define, per incident category, which canonical fields matter, how much they weigh, when they
apply, how values are validated and how their quality is judged.

* Location: `templates/<template_id>/<version>.json` (directory and file name must match the content).
* Loaded at start-up by `TemplateRegistry`; invalid templates stop the process.
* `branchops migrate` publishes every `published` version to `template_versions` with a SHA-256 of its
  canonical JSON. **A published version can never change**: if a file's content differs from the stored
  hash, migration fails. Publish a new version instead.
* New analyses use the **latest published version** of the selected template. Every analysis stores
  `template_id` + `template_version`, and older versions remain loadable, so historical scores stay
  explainable.

## Default templates

| template_id | Category | Subcategories |
|---|---|---|
| `application-incident` | APPLICATION | SCREEN_ERROR, PERFORMANCE, DATA_DISPLAY, CRASH |
| `peripheral-device-incident` | PERIPHERAL_DEVICE | CARD_READER, PRINTER, PASSBOOK_PRINTER, CASH_RECYCLER, SIGNATURE_PAD, SCANNER |
| `authentication-incident` | AUTHENTICATION | LOGIN_FAILURE, PASSWORD_RESET, ACCOUNT_LOCKED, TOKEN_OTP, AUTHORIZATION_ROLE |
| `transaction-incident` | TRANSACTION | EFT_FAST, CASH_OPERATION, FX, LOAN, CARD_TRANSACTION, ACCOUNT_OPENING |
| `network-incident` | NETWORK | CONNECTIVITY_LOSS, SLOWNESS, VPN, WIFI |
| `integration-incident` | INTEGRATION | CORE_BANKING, CREDIT_BUREAU, PAYMENT_SYSTEM, CUSTOMER_REGISTRY, CARD_SYSTEM |
| `generic-operational-incident` | OTHER (fallback) | — |

The classification taxonomy shown to the model is **derived from the templates**, so adding a
category/subcategory to a template makes it selectable.

## Format

```json
{
  "template_id": "transaction-incident",
  "version": "1.0.0",
  "status": "published",
  "name": "İşlem Olayı",
  "description": "…",
  "category": "TRANSACTION",
  "subcategories": ["EFT_FAST", "CASH_OPERATION"],
  "is_fallback": false,
  "applicable_when": null,
  "fields": [
    {
      "name": "branch_code",
      "label": "Şube kodu",
      "weight": 8,
      "required": true,
      "required_when": null,
      "applicable_when": null,
      "allow_not_applicable": false,
      "validation": {"pattern": "^\\d{3,5}$"},
      "rubric": {
        "criteria": "Sorunun yaşandığı şubenin kodu.",
        "good": "Geçerli şube kodu belirtilmiş.",
        "acceptable": "Şube adı var ama kod yok.",
        "poor": "Şube bilgisi belirsiz."
      },
      "recommendation": "Sorunun yaşandığı şubenin kodunu (ör. 1234) ekleyin."
    }
  ]
}
```

| Key | Meaning |
|---|---|
| `name` | one of the canonical fields: summary, description, branch_code, application, application_version, category, subcategory, business_process, business_impact, error_details, occurrence_time, reproduction_steps, frequency, environment, troubleshooting_steps, evidence |
| `weight` | relative weight (0 < w ≤ 100) in both scores |
| `required` / `required_when` | required unconditionally / when the condition holds — drives `missing_required_fields` and recommendation priority |
| `applicable_when` | the field is excluded (NOT_APPLICABLE) when the condition is false |
| `allow_not_applicable` | the model may declare the field not applicable (otherwise such a claim counts as MISSING) |
| `validation` | `pattern` (full match), `min_length`, `max_length`, `min_words`, `allowed_values`, `reject_generic_phrases` — failures make the field INSUFFICIENT and cap its rubric level at POOR |
| `rubric` | criteria and level descriptions given to the QualityAssessmentAgent |
| `recommendation` | deterministic hint used for missing/insufficient fields (and as the model's starting point) |

### Conditions

Conditions are evaluated against the canonical values (`category`, `subcategory` and every field value):

```json
{"all": [
  {"field": "category", "op": "equals", "value": "NETWORK"},
  {"not": {"field": "subcategory", "op": "in", "value": ["WIFI"]}},
  {"any": [{"field": "frequency", "op": "matches", "value": "(her|sürekli)"},
           {"field": "branch_code", "op": "present"}]}
]}
```

Operators: `equals`, `not_equals`, `in`, `not_in`, `present`, `absent`, `matches` (case-insensitive regex).
Comparisons are Turkish-aware case-insensitive.

## Template selection

Deterministic: among the latest published templates of the classified category whose
`applicable_when` holds, a template listing the subcategory wins over a generic one; ties break by
`template_id`. No match (or classification unavailable) → the fallback template.

## Adding or changing a template

1. Copy an existing file to `templates/<new-id>/1.0.0.json` (or, to change a template,
   `templates/<id>/<next-version>.json` — never edit a published file).
2. Adjust category, subcategories, fields, weights, rubrics and Turkish recommendation texts.
3. `branchops templates` — validates the schema and lints (needs a required field, no
   required-but-conditional fields, one fallback overall).
4. `pytest tests/unit/test_templates.py` and review score changes on the evaluation set
   (`docs/evaluation.md`).
5. Deploy; the migration job publishes the new version. Use manual re-analysis if existing issues should
   be re-scored with it.

`"status": "draft"` versions are loaded and validated but never selected or published, which lets a new
version be reviewed in Git before release. To retire a template, remove its directory: it stops being
selectable, while its published versions remain in `template_versions` and every stored analysis keeps
its field scores, so history stays intact.

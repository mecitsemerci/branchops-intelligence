"""Deterministic evaluation of template conditions and field validation rules."""

from __future__ import annotations

import re
from collections.abc import Mapping

from branchops.domain.services.evidence import normalize_for_match
from branchops.templates.model import Condition, FieldDefinition, IssueTemplate

ConditionContext = Mapping[str, str | None]


def evaluate(condition: Condition | None, ctx: ConditionContext) -> bool:
    if condition is None:
        return True
    if condition.all is not None:
        return all(evaluate(c, ctx) for c in condition.all)
    if condition.any is not None:
        return any(evaluate(c, ctx) for c in condition.any)
    if condition.not_ is not None:
        return not evaluate(condition.not_, ctx)
    if condition.field is None:  # unreachable: enforced by the Condition model validator
        return False
    raw = ctx.get(condition.field)
    value = raw.strip() if isinstance(raw, str) else raw
    present = value is not None and value != ""
    match condition.op:
        case "present":
            return present
        case "absent":
            return not present
        case "equals":
            return present and _eq(value, condition.value)
        case "not_equals":
            return not (present and _eq(value, condition.value))
        case "in":
            return present and any(_eq(value, v) for v in condition.value)
        case "not_in":
            return not (present and any(_eq(value, v) for v in condition.value))
        case "matches":
            return present and re.search(str(condition.value), str(value), re.IGNORECASE) is not None
    return False


def _eq(a: object, b: object) -> bool:
    return normalize_for_match(str(a)) == normalize_for_match(str(b))


def is_applicable(field: FieldDefinition, ctx: ConditionContext) -> bool:
    return evaluate(field.applicable_when, ctx)


def is_required(field: FieldDefinition, ctx: ConditionContext) -> bool:
    if field.required:
        return True
    return field.required_when is not None and evaluate(field.required_when, ctx)


def validate_value(field: FieldDefinition, value: str | None) -> list[str]:
    """Return human-readable validation errors (Turkish) for a present value."""
    if value is None or value.strip() == "":
        return []
    rule = field.validation
    text = value.strip()
    errors: list[str] = []
    if rule.pattern and not re.fullmatch(rule.pattern, text):
        errors.append(f"{field.label} beklenen formata uymuyor")
    if rule.min_length is not None and len(text) < rule.min_length:
        errors.append(f"{field.label} çok kısa (en az {rule.min_length} karakter)")
    if rule.max_length is not None and len(text) > rule.max_length:
        errors.append(f"{field.label} çok uzun (en fazla {rule.max_length} karakter)")
    if rule.min_words is not None and len(text.split()) < rule.min_words:
        errors.append(f"{field.label} yetersiz ayrıntı içeriyor (en az {rule.min_words} kelime)")
    if rule.allowed_values is not None and not any(_eq(text, v) for v in rule.allowed_values):
        errors.append(f"{field.label} izin verilen değerlerden biri değil")
    normalized = normalize_for_match(text)
    for phrase in rule.reject_generic_phrases:
        if normalized == normalize_for_match(phrase):
            errors.append(f"{field.label} genel/belirsiz bir ifade içeriyor: '{phrase}'")
    return errors


def lint_template(template: IssueTemplate) -> list[str]:
    """Static checks beyond the Pydantic schema; returns problems (empty == valid)."""
    problems: list[str] = []
    if not any(f.required for f in template.fields):
        problems.append("template has no unconditionally required field")
    total = sum(f.weight for f in template.fields)
    if total <= 0:
        problems.append("template weights sum to zero")
    for f in template.fields:
        if f.required and f.applicable_when is not None:
            problems.append(f"field '{f.name}' is required but conditionally applicable")
    return problems

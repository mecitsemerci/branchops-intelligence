from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from branchops.templates.model import Condition, IssueTemplate
from branchops.templates.registry import TemplateError, TemplateRegistry
from branchops.templates.validation import evaluate, validate_value
from tests.helpers import TEMPLATES

EXPECTED_DEFAULTS = {
    "application-incident",
    "peripheral-device-incident",
    "authentication-incident",
    "transaction-incident",
    "network-incident",
    "integration-incident",
    "generic-operational-incident",
}


def test_default_templates_load(registry: TemplateRegistry) -> None:
    assert {t.template_id for t in registry.latest_published()} == EXPECTED_DEFAULTS
    assert registry.fallback().template_id == "generic-operational-incident"
    assert "OTHER" in registry.taxonomy()


@pytest.mark.parametrize(
    ("category", "subcategory", "expected"),
    [
        ("APPLICATION", "CRASH", "application-incident"),
        ("PERIPHERAL_DEVICE", "CARD_READER", "peripheral-device-incident"),
        ("AUTHENTICATION", None, "authentication-incident"),
        ("TRANSACTION", "EFT_FAST", "transaction-incident"),
        ("NETWORK", "VPN", "network-incident"),
        ("INTEGRATION", "CREDIT_BUREAU", "integration-incident"),
        ("OTHER", None, "generic-operational-incident"),
        (None, None, "generic-operational-incident"),
        ("UNKNOWN_CATEGORY", None, "generic-operational-incident"),
    ],
)
def test_template_selection(
    registry: TemplateRegistry, category: str | None, subcategory: str | None, expected: str
) -> None:
    ctx = {"category": category, "subcategory": subcategory}
    assert registry.select(category, subcategory, ctx).template_id == expected


def test_selection_prefers_latest_version(tmp_path: Path) -> None:
    for d in TEMPLATES.iterdir():
        target = tmp_path / d.name
        target.mkdir()
        for f in d.glob("*.json"):
            (target / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    data = json.loads((TEMPLATES / "network-incident" / "1.0.0.json").read_text(encoding="utf-8"))
    data["version"] = "1.1.0"
    (tmp_path / "network-incident" / "1.1.0.json").write_text(json.dumps(data), encoding="utf-8")
    reg = TemplateRegistry.from_directory(tmp_path)
    assert reg.select("NETWORK", "VPN", {}).version == "1.1.0"
    assert reg.get("network-incident", "1.0.0") is not None  # history retained


def test_file_location_must_match_identity(tmp_path: Path) -> None:
    for d in TEMPLATES.iterdir():
        (tmp_path / d.name).mkdir()
        for f in d.glob("*.json"):
            (tmp_path / d.name / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "network-incident" / "1.0.0.json").rename(tmp_path / "network-incident" / "9.9.9.json")
    with pytest.raises(TemplateError, match="location"):
        TemplateRegistry.from_directory(tmp_path)


def test_registry_requires_fallback(registry: TemplateRegistry) -> None:
    non_fallback = [t for t in registry.all_versions() if not t.is_fallback]
    with pytest.raises(TemplateError, match="fallback"):
        TemplateRegistry(non_fallback)


def test_content_hash_is_stable_and_sensitive(registry: TemplateRegistry) -> None:
    t = registry.latest_published()[0]
    assert t.content_hash() == IssueTemplate.model_validate(t.model_dump(by_alias=True)).content_hash()
    changed = t.model_copy(update={"description": t.description + "!"})
    assert changed.content_hash() != t.content_hash()


def test_template_schema_rejects_unknown_field_and_bad_version() -> None:
    rubric = {"criteria": "c", "good": "g", "acceptable": "a", "poor": "p"}
    base = {
        "template_id": "x-test",
        "version": "1.0.0",
        "name": "x",
        "category": "APPLICATION",
        "fields": [
            {
                "name": "summary",
                "label": "s",
                "weight": 1,
                "required": True,
                "rubric": rubric,
                "recommendation": "ekleyin",
            }
        ],
    }
    IssueTemplate.model_validate(base)
    with pytest.raises(ValidationError):
        IssueTemplate.model_validate({**base, "version": "1.0"})
    bad_field = {**base, "fields": [{**base["fields"][0], "name": "not_a_field"}]}  # type: ignore[index]
    with pytest.raises(ValidationError):
        IssueTemplate.model_validate(bad_field)
    with pytest.raises(ValidationError):
        IssueTemplate.model_validate({**base, "fields": base["fields"] * 2})  # type: ignore[operator]


class TestConditions:
    def test_leaf_operators(self) -> None:
        ctx = {"category": "NETWORK", "subcategory": "VPN", "branch_code": None, "frequency": "her işlemde"}
        assert evaluate(Condition(field="category", op="equals", value="network"), ctx)
        assert evaluate(Condition(field="subcategory", op="in", value=["VPN", "WIFI"]), ctx)
        assert evaluate(Condition(field="subcategory", op="not_in", value=["WIFI"]), ctx)
        assert evaluate(Condition(field="branch_code", op="absent"), ctx)
        assert not evaluate(Condition(field="branch_code", op="present"), ctx)
        assert evaluate(Condition(field="frequency", op="matches", value="her|sürekli"), ctx)
        assert evaluate(Condition(field="category", op="not_equals", value="APPLICATION"), ctx)

    def test_combinators(self) -> None:
        cond = Condition.model_validate(
            {
                "all": [
                    {"field": "category", "op": "equals", "value": "NETWORK"},
                    {"not": {"field": "subcategory", "op": "equals", "value": "WIFI"}},
                    {
                        "any": [
                            {"field": "branch_code", "op": "present"},
                            {"field": "category", "op": "present"},
                        ]
                    },
                ]
            }
        )
        assert evaluate(cond, {"category": "NETWORK", "subcategory": "VPN"})
        assert not evaluate(cond, {"category": "NETWORK", "subcategory": "WIFI"})

    def test_invalid_condition_shapes(self) -> None:
        with pytest.raises(ValidationError):
            Condition.model_validate({"field": "x"})
        with pytest.raises(ValidationError):
            Condition.model_validate({"field": "x", "op": "in", "value": "notalist"})
        with pytest.raises(ValidationError):
            Condition.model_validate({"field": "x", "op": "present", "all": []})


def test_value_validation_rules(registry: TemplateRegistry) -> None:
    t = registry.get("application-incident", "1.0.0")
    assert t is not None
    summary = t.field_def("summary")
    branch = t.field_def("branch_code")
    assert summary is not None and branch is not None
    assert validate_value(branch, "1234") == []
    assert validate_value(branch, "Kadıköy") != []
    assert validate_value(summary, "Sorun var") != []  # generic phrase + too few words
    assert validate_value(summary, "Gişe ekranında EFT onayında hata kodu alınıyor") == []
    assert validate_value(summary, None) == []

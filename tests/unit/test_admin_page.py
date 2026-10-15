"""Smoke tests for the /yonetim admin page module and its pure helpers."""

from __future__ import annotations

from branchops.dashboard.pages import admin


def test_register_is_callable() -> None:
    assert callable(admin.register)
    assert callable(admin.test_notification_button) and callable(admin.unowned_screens_card)


def test_parse_tags_and_blank() -> None:
    assert admin.parse_tags("kasa, eft ,  ,kasa") == ["kasa", "eft"]
    assert admin.parse_tags(None) == []
    assert (
        admin.blank_to_none("  ") is None and admin.blank_to_none("x") == "x" and admin.blank_to_none(3) == 3
    )


def test_shorten_json_and_moved() -> None:
    assert admin.shorten_json(None) == "—"
    assert admin.shorten_json({"b": 1, "a": "ş"}) == '{"a": "ş", "b": 1}'
    long = admin.shorten_json({"k": "x" * 500}, limit=20)
    assert len(long) == 20 and long.endswith("…")
    assert admin.moved([1, 2, 3], 0, -1) == [1, 2, 3]
    assert admin.moved([1, 2, 3], 2, -1) == [1, 3, 2]
    assert admin.moved([1, 2, 3], 0, 1) == [2, 1, 3]


def test_tree_nodes_and_rule_summary() -> None:
    tree = [
        {
            "id": 1,
            "key": "eft",
            "name": "EFT",
            "active": True,
            "owner_team_key": "payments",
            "default_screen_owner_team_key": None,
            "screens": [
                {
                    "id": 5,
                    "key": "send",
                    "title": "Gönder",
                    "owner_team_key": "pay-ui",
                    "route_pattern": "/eft",
                    "active": False,
                }
            ],
        }
    ]
    nodes = admin.tree_nodes(tree)
    assert nodes[0]["id"] == "app:1" and "sahip: payments" in nodes[0]["label"]
    child = nodes[0]["children"][0]
    assert child["id"] == "screen:5" and "pay-ui" in child["label"] and "/eft" in child["label"]
    assert child["label"].endswith("[pasif]")
    rule = {
        "match": {"category": "TRANSACTION", "error_code_glob": "EFT-*"},
        "notify_to_team_ids": [1],
        "cc_team_ids": [9],
        "channels": ["email"],
    }
    summary = admin.rule_summary(rule, {1: "payments"})
    assert summary == "category=TRANSACTION, error_code_glob=EFT-* → kime: payments · bilgi: 9 · kanal: email"
    assert admin.team_options([{"id": 1, "name": "Ödeme", "key": "pay", "active": False}]) == {
        1: "Ödeme (pay) [pasif]"
    }

"""Ownership registry on PostgreSQL: CRUD with change log, CSV import/export, REST and MCP read tools."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client

from branchops.api.app import create_app
from branchops.application.commands.ownership import OwnershipService
from branchops.config.settings import Settings
from branchops.container import Container
from branchops.domain.entities.ownership import (
    ApplicationInput,
    MatchedLevel,
    RoutingInput,
    RuleInput,
    RuleMatch,
    ScreenInput,
    TeamInput,
    TeamKind,
)
from branchops.infrastructure.database.session import Database
from branchops.infrastructure.persistence.ownership_repository import OwnershipError
from tests.helpers import make_settings
from tests.integration.test_mcp_server import run_as

pytestmark = pytest.mark.postgres


def seed(own: OwnershipService) -> dict[str, Any]:
    ops = own.save_team(TeamInput(key="branch-ops", name="Şube Operasyon", kind=TeamKind.BRANCH_OPS), "admin")
    eft = own.save_team(TeamInput(key="eft-team", name="EFT Ekibi", email="eft@bank.internal"), "admin")
    pay = own.save_team(TeamInput(key="payments", name="Ödemeler"), "admin")
    app = own.save_application(
        ApplicationInput(key="eft", name="EFT Uygulaması", owner_team_id=pay["id"]), "admin"
    )
    screen = own.save_screen(
        ScreenInput(
            application_id=app["id"],
            key="eft-new",
            title="Yeni EFT",
            route_pattern="/eft/new",
            owner_team_id=eft["id"],
        ),
        "admin",
    )
    return {"ops": ops, "eft": eft, "pay": pay, "app": app, "screen": screen}


@pytest.fixture
def settings(db: Database) -> Settings:
    return make_settings(database={"url": db.engine.url.render_as_string(hide_password=False)})


@pytest.fixture
def own(db: Database, settings: Settings) -> OwnershipService:
    return OwnershipService(db, settings)


def test_crud_change_log_and_resolution(own: OwnershipService) -> None:
    ids = seed(own)
    d = own.resolve(RoutingInput(app_key="eft", route="/eft/new"))
    assert (d.owner_team_key, d.matched_level) == ("eft-team", MatchedLevel.SCREEN_ROUTE)
    assert d.cc_team_ids == (ids["ops"]["id"],)

    own.bulk_set_screen_owner([ids["screen"]["id"]], ids["pay"]["id"], "ayse")
    assert own.resolve(RoutingInput(app_key="eft", screen_key="eft-new")).owner_team_key == "payments"
    own.set_active("team", ids["pay"]["id"], False, "ayse")
    # Screen and application owner inactive -> branch ops fallback.
    fallback = own.resolve(RoutingInput(app_key="eft", screen_key="eft-new"))
    assert fallback.matched_level is MatchedLevel.BRANCH_OPS_FALLBACK and fallback.unowned_screen

    with pytest.raises(OwnershipError, match="already exists"):
        own.save_team(TeamInput(key="eft-team", name="kopya"), "admin")
    with pytest.raises(OwnershipError, match="still owns"):
        own.delete("team", ids["pay"]["id"], "admin")
    rule = own.save_rule(
        RuleInput(name="işlem", match=RuleMatch(category="TRANSACTION"), cc_team_ids=[ids["eft"]["id"]]),
        "admin",
    )
    with pytest.raises(OwnershipError, match="routing rule"):
        own.delete("team", ids["eft"]["id"], "admin")
    own.delete("rule", rule["id"], "admin")

    changes = own.changes()
    assert [(c["entity"], c["action"]) for c in changes[:4]] == [
        ("rule", "delete"),
        ("rule", "create"),
        ("team", "deactivate"),
        ("screen", "owner_change"),
    ]
    owner_change = changes[3]
    assert owner_change["actor"] == "ayse"
    assert owner_change["before"]["owner_team_id"] == ids["eft"]["id"]
    assert owner_change["after"]["owner_team_id"] == ids["pay"]["id"]
    assert len(own.changes(entity="team")) == 4  # 3 creates + deactivate


def test_csv_import_export_is_all_or_nothing(own: OwnershipService) -> None:
    seed(own)
    good = (
        "application_key,screen_key,title,route_pattern,owner_team_key\n"
        "eft,eft-new,Yeni EFT (v2),/eft/new,payments\n"
        "eft,eft-list,EFT Listesi,/eft/list,eft-team\n"
        "crm,customer-search,Müşteri Arama,/customers,eft-team\n"
    )
    dry = own.import_csv(good, "admin", dry_run=True)
    assert (dry.created_screens, dry.updated_screens, dry.created_applications) == (2, 1, 1)
    assert len(own.tree()) == 1  # dry run changed nothing
    bad = good + "eft,x,Başlık,,nobody\n"
    report = own.import_csv(bad, "admin")
    assert report.errors == ["line 5: unknown owner_team_key 'nobody'"]
    assert len(own.tree()) == 1  # nothing applied
    report = own.import_csv(good, "admin")
    assert not report.errors and report.created_screens == 2
    exported = own.export_csv().splitlines()
    assert exported[0] == "application_key,screen_key,title,route_pattern,owner_team_key"
    assert "crm,customer-search,Müşteri Arama,/customers,eft-team" in exported
    assert own.import_csv("\n".join(exported) + "\n", "admin").unchanged == 3


def test_rule_preview_counts_recent_incidents(own: OwnershipService, db: Database) -> None:
    from sqlalchemy import text

    with db.session() as s:
        for n, (cat, sig) in enumerate(
            [("TRANSACTION", "eft|EFT-504"), ("TRANSACTION", None), ("NETWORK", None)]
        ):
            s.execute(
                text(
                    "INSERT INTO issues (incident_key, source, external_key, issue_key, snapshot, "
                    "is_deleted, category, error_signature) VALUES (:k, 'jira', :e, :e, '{}', false, :c, :s)"
                ),
                {"k": f"jira:OPS-{n}", "e": f"OPS-{n}", "c": cat, "s": sig},
            )
    ids = seed(own)
    rule = RuleInput(
        name="eft kodları", match=RuleMatch(error_code_glob="EFT-*"), cc_team_ids=[ids["ops"]["id"]]
    )
    assert own.preview_rule(rule).matched == 1
    by_category = rule.model_copy(update={"match": RuleMatch(category="transaction")})
    preview = own.preview_rule(by_category)
    assert (preview.matched, preview.scanned) == (2, 3)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    container = Container.build(settings)
    with TestClient(create_app(settings, container)) as c:
        yield c


def test_rest_admin_and_read_endpoints(client: TestClient) -> None:
    team = client.post(
        "/api/admin/ownership/teams",
        json={
            "key": "branch-ops",
            "name": "Şube Operasyon",
            "kind": "branch_ops",
            "zoom_webhook_secret_ref": "ZOOM_OPS",
        },
    )
    assert team.status_code == 201
    eft = client.post("/api/admin/ownership/teams", json={"key": "eft-team", "name": "EFT"}).json()
    assert (
        client.post("/api/admin/ownership/teams", json={"key": "eft-team", "name": "xx"}).status_code == 409
    )
    assert client.post("/api/admin/ownership/teams", json={"key": "BAD KEY", "name": "x"}).status_code == 422
    app = client.post(
        "/api/admin/ownership/applications", json={"key": "eft", "name": "EFT", "owner_team_id": eft["id"]}
    ).json()
    csv_body = (
        "application_key,screen_key,title,route_pattern,owner_team_key\n"
        "eft,eft-new,Yeni EFT,/eft/new,eft-team\n"
    )
    imported = client.post("/api/admin/ownership/import", content=csv_body.encode())
    assert imported.status_code == 200 and imported.json()["created_screens"] == 1
    resolved = client.get("/api/ownership/resolve", params={"app": "eft", "route": "/eft/new"}).json()
    assert resolved["owner"]["key"] == "eft-team" and resolved["screen_title"] == "Yeni EFT"
    assert [t["key"] for t in resolved["cc"]] == ["branch-ops"]
    teams = client.get("/api/ownership/teams").json()
    assert all("zoom_webhook_secret_ref" not in t for t in teams)
    assert client.get("/api/admin/ownership/teams").json()[0]["zoom_webhook_secret_ref"] == "ZOOM_OPS"
    assert app["owner_team_id"] == eft["id"]
    assert len(client.get("/api/admin/ownership/changes").json()) == 4


def test_rest_admin_requires_admin(db: Database) -> None:
    settings = make_settings(
        database={"url": db.engine.url.render_as_string(hide_password=False)},
        security={"auth_mode": "dev", "dev_roles": ["VIEWER"]},
    )
    with TestClient(create_app(settings, Container.build(settings))) as c:
        assert c.get("/api/admin/ownership/teams").status_code == 403
        assert c.get("/api/ownership/teams").status_code == 200


def test_mcp_resolve_owner(db: Database) -> None:
    own = OwnershipService(db, make_settings())
    seed(own)

    async def scenario(client: Client[Any]) -> tuple[Any, Any]:
        teams = (await client.call_tool("list_teams", {})).structured_content
        resolved = (
            await client.call_tool("resolve_owner", {"app_id": "eft", "screen_id": "eft-new"})
        ).structured_content
        return teams, resolved

    teams, resolved = run_as(db, "ali", ["branchops-branch-ops"], scenario)
    assert [t["key"] for t in teams["items"]] == ["branch-ops", "eft-team", "payments"]
    assert resolved["owner"]["key"] == "eft-team" and resolved["matched_level"] == "SCREEN_KEY"
    assert [t["key"] for t in resolved["cc"]] == ["branch-ops"]
    assert resolved["screen_title"] == "Yeni EFT"

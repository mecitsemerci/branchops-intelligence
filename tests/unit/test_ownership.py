"""Ownership registry: resolve_owner precedence and fallback, routing rules, route patterns, working hours."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from branchops.domain.entities.ownership import (
    Application,
    MatchedLevel,
    OwnershipRegistry,
    RoutingClassification,
    RoutingInput,
    RoutingRule,
    RuleInput,
    RuleMatch,
    Screen,
    Team,
    TeamInput,
    TeamKind,
    resolve_owner,
    route_path,
    route_regex,
)
from branchops.domain.services.working_hours import parse_working_hours, within_working_hours

BRANCH_OPS = Team(1, "branch-ops", "Şube Operasyon", TeamKind.BRANCH_OPS, email="subeops@bank.internal")
EFT_TEAM = Team(2, "eft-team", "EFT Ekibi")
PAYMENTS = Team(3, "payments", "Ödemeler")
SECURITY = Team(4, "security", "Güvenlik", TeamKind.SECURITY)
RETIRED = Team(5, "retired", "Eski Ekip", active=False)
CARDS = Team(6, "cards", "Kartlar")

EFT = Application(
    10, "eft", "EFT Uygulaması", owner_team_id=PAYMENTS.id, default_screen_owner_team_id=EFT_TEAM.id
)
CRM = Application(11, "crm", "Müşteri Yönetimi", owner_team_id=CARDS.id)
SCREENS = (
    Screen(100, EFT.id, "eft-new", "Yeni EFT", owner_team_id=EFT_TEAM.id, route_pattern="/eft/new"),
    Screen(101, EFT.id, "eft-detail", "EFT Detay", owner_team_id=PAYMENTS.id, route_pattern="/eft/{id}"),
    Screen(102, EFT.id, "eft-any", "EFT Diğer", owner_team_id=CARDS.id, route_pattern="/eft/*"),
    Screen(103, EFT.id, "eft-old", "Eski ekran", owner_team_id=RETIRED.id),
)
RULES = (
    RoutingRule(
        200,
        "kart hataları güvenliğe",
        10,
        RuleMatch(error_code_glob="CARD-*"),
        (SECURITY.id,),
        (CARDS.id,),
        ("email",),
    ),
    RoutingRule(201, "pasif kural", 5, RuleMatch(category="TRANSACTION"), (SECURITY.id,), active=False),
    RoutingRule(202, "işlem kategorisi", 20, RuleMatch(category="TRANSACTION"), (), (PAYMENTS.id,)),
)
REGISTRY = OwnershipRegistry(
    teams=(BRANCH_OPS, EFT_TEAM, PAYMENTS, SECURITY, RETIRED, CARDS),
    applications=(EFT, CRM),
    screens=SCREENS,
    rules=RULES,
)


def resolve(
    ctx: RoutingInput | None, cls: RoutingClassification | None = None, registry: OwnershipRegistry = REGISTRY
):  # type: ignore[no-untyped-def]
    return resolve_owner(ctx, cls, registry, branch_ops_team_key="branch-ops")


class TestPrecedence:
    def test_screen_key_wins(self) -> None:
        d = resolve(RoutingInput(app_key="eft", screen_key="eft-detail", route="/eft/new"))
        assert (d.owner_team_key, d.matched_level, d.screen_key) == (
            "payments",
            MatchedLevel.SCREEN_KEY,
            "eft-detail",
        )
        assert BRANCH_OPS.id in d.cc_team_ids and not d.unowned_screen

    def test_route_pattern_most_specific_first(self) -> None:
        assert resolve(RoutingInput(app_key="eft", route="/eft/new")).screen_key == "eft-new"
        d = resolve(RoutingInput(app_key="eft", route="https://eft.bank.internal/eft/12345?x=1#top"))
        assert (d.screen_key, d.matched_level) == ("eft-detail", MatchedLevel.SCREEN_ROUTE)
        assert resolve(RoutingInput(app_key="eft", route="/eft/12/receipt")).screen_key == "eft-any"

    def test_inactive_screen_owner_falls_to_application_default(self) -> None:
        d = resolve(RoutingInput(app_key="eft", screen_key="eft-old"))
        assert (d.owner_team_key, d.matched_level) == (
            "eft-team",
            MatchedLevel.APPLICATION_DEFAULT_SCREEN_OWNER,
        )
        assert any("inactive" in r for r in d.reasons)

    def test_unknown_screen_uses_application_owner_chain(self) -> None:
        d = resolve(RoutingInput(app_key="crm", screen_key="customer-search"))
        assert (d.owner_team_key, d.matched_level) == ("cards", MatchedLevel.APPLICATION_OWNER)
        assert d.screen_unregistered and not d.unowned_screen

    def test_application_by_name_from_classification(self) -> None:
        d = resolve(None, RoutingClassification(application="müşteri yönetimi"))
        assert d.owner_team_key == "cards" and d.application_key == "crm"

    def test_rule_owner_when_no_application(self) -> None:
        d = resolve(RoutingInput(app_key="unknown-app", error_code="CARD-17"))
        assert (d.owner_team_key, d.matched_level) == ("security", MatchedLevel.ROUTING_RULE)
        assert set(d.cc_team_ids) == {CARDS.id, BRANCH_OPS.id} and d.channels == ("email",)
        assert d.matched_rule_ids == (200,)

    def test_rules_add_recipients_to_screen_owner(self) -> None:
        d = resolve(
            RoutingInput(app_key="eft", screen_key="eft-new", error_code="card-99"),
            RoutingClassification(category="TRANSACTION"),
        )
        assert d.owner_team_key == "eft-team"
        assert d.to_team_ids == (EFT_TEAM.id, SECURITY.id)
        assert d.cc_team_ids == (CARDS.id, PAYMENTS.id, BRANCH_OPS.id)
        assert d.matched_rule_ids == (200, 202)  # inactive rule 201 ignored, priority order

    def test_fallback_to_branch_ops(self) -> None:
        d = resolve(RoutingInput(app_key="nope", screen_key="x"))
        assert (d.owner_team_key, d.matched_level) == ("branch-ops", MatchedLevel.BRANCH_OPS_FALLBACK)
        assert d.unowned_screen and d.cc_team_ids == ()  # owner already; never duplicated in CC
        quiet = resolve(None)
        assert quiet.matched_level is MatchedLevel.BRANCH_OPS_FALLBACK and not quiet.unowned_screen

    def test_inactive_application_is_ignored(self) -> None:
        registry = OwnershipRegistry(
            teams=REGISTRY.teams,
            applications=(Application(12, "old", "Old", owner_team_id=PAYMENTS.id, active=False),),
        )
        assert (
            resolve(RoutingInput(app_key="old"), registry=registry).matched_level
            is MatchedLevel.BRANCH_OPS_FALLBACK
        )

    def test_deterministic(self) -> None:
        ctx = RoutingInput(app_key="eft", route="/eft/9", error_code="CARD-1", branch_code="1001")
        assert all(resolve(ctx) == resolve(ctx) for _ in range(5))


class TestRules:
    def test_match_needs_a_condition_and_recipients(self) -> None:
        with pytest.raises(ValidationError):
            RuleMatch()
        with pytest.raises(ValidationError, match="recipient"):
            RuleInput(name="boş", match=RuleMatch(category="X"))
        with pytest.raises(ValidationError, match="unknown channels"):
            RuleInput(name="kanal", match=RuleMatch(category="X"), cc_team_ids=[1], channels=["sms"])

    def test_branch_glob_and_application_key(self) -> None:
        rule = RoutingRule(
            300, "1xxx şubeleri", 1, RuleMatch(branch_code_glob="1*", application_key="crm"), (SECURITY.id,)
        )
        registry = OwnershipRegistry(teams=REGISTRY.teams, applications=REGISTRY.applications, rules=(rule,))
        hit = resolve(RoutingInput(app_key="crm", branch_code="1001"), registry=registry)
        assert SECURITY.id in hit.to_team_ids
        miss = resolve(RoutingInput(app_key="crm", branch_code="2001"), registry=registry)
        assert SECURITY.id not in miss.to_team_ids


class TestRoutes:
    def test_patterns(self) -> None:
        assert route_regex("/customers/{id}/accounts").match("/customers/42/accounts/")
        assert not route_regex("/customers/{id}/accounts").match("/customers/42/43/accounts")
        assert route_regex("/eft/*").match("/eft/a/b")
        assert route_path("https://app.bank.internal/eft/new?x=1") == "/eft/new"
        assert route_path("/eft/new#frag") == "/eft/new"


class TestTeams:
    def test_team_input_validation(self) -> None:
        TeamInput(
            key="eft-team",
            name="EFT",
            working_hours="Pzt-Cum 08:30-17:30",
            zoom_webhook_secret_ref="ZOOM_EFT",
        )
        with pytest.raises(ValidationError):
            TeamInput(key="EFT Team", name="EFT")
        with pytest.raises(ValidationError):
            TeamInput(key="eft", name="EFT", working_hours="her zaman")
        with pytest.raises(ValidationError):
            TeamInput(key="eft", name="EFT", zoom_webhook_secret_ref="literal-secret-value")

    def test_working_hours(self) -> None:
        spec = "Mon-Fri 08:30-17:30; Sat 09:00-13:00"
        assert len(parse_working_hours(spec)) == 2
        assert within_working_hours(spec, datetime(2026, 10, 15, 7, 0, tzinfo=UTC))  # Thu 10:00 +03
        assert not within_working_hours(spec, datetime(2026, 10, 15, 15, 0, tzinfo=UTC))  # 18:00 +03
        assert within_working_hours(spec, datetime(2026, 10, 17, 7, 0, tzinfo=UTC))  # Sat 10:00
        assert not within_working_hours(spec, datetime(2026, 10, 18, 7, 0, tzinfo=UTC))  # Sun
        assert within_working_hours(None, datetime(2026, 10, 18, 3, 0, tzinfo=UTC))
        assert within_working_hours("Mon-Fri 09:00-17:00 UTC", datetime(2026, 10, 15, 16, 30, tzinfo=UTC))


class TestHardening:
    def test_route_patterns_are_validated_and_linear(self) -> None:
        from branchops.domain.entities.ownership import ScreenInput, validate_route_pattern

        for bad in ("/a*b/*", "/{a}{b}", "eft", "/x/*/y"):
            with pytest.raises(ValueError):
                validate_route_pattern(bad)
        with pytest.raises(ValidationError):
            ScreenInput(application_id=1, key="s", title="Ekran", owner_team_id=1, route_pattern="/a*a*a*b")
        assert not route_regex("/a*a*a*b").match("/" + "a" * 400)  # legacy invalid pattern never matches
        assert route_regex("/eft/*").match("/eft") and not route_regex("/eft/*").match("/efta")

    def test_csv_export_neutralises_formulas(self) -> None:
        from branchops.infrastructure.persistence.ownership_repository import csv_safe, csv_unsafe

        assert csv_safe('=HYPERLINK("http://x")') == '\'=HYPERLINK("http://x")'
        assert csv_safe("Yeni EFT") == "Yeni EFT"
        assert csv_unsafe(csv_safe("+90 hattı")) == "+90 hattı"
        assert csv_unsafe("'normal") == "'normal"

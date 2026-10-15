from __future__ import annotations

from branchops.domain.enums import JiraEventType
from branchops.domain.services.snapshot_merge import merge_event
from branchops.infrastructure.jira.adapter import JiraWebhookAdapter
from branchops.simulator.generator import SCENARIOS, IncidentGenerator

ADAPTER = JiraWebhookAdapter({"branch_code": "customfield_10101", "application": "customfield_10102"})


def test_generated_issues_are_valid_jira_payloads() -> None:
    gen = IncidentGenerator(seed=42, start_number=10_000)
    keys = set()
    profiles = set()
    for _ in range(60):
        issue = gen.new_issue()
        event = ADAPTER.normalize(issue.payload)
        assert event.event_type is JiraEventType.ISSUE_CREATED
        assert event.issue_key == issue.key
        assert event.issue_fields["summary"]
        assert event.issue_fields["description"]
        keys.add(issue.key)
        profiles.add(issue.profile)
    assert len(keys) == 60
    assert profiles == {"complete", "partial", "poor"}


def test_generation_is_reproducible_with_seed() -> None:
    a = [IncidentGenerator(seed=1, start_number=1).new_issue().payload for _ in range(1)]
    b = [IncidentGenerator(seed=1, start_number=1).new_issue().payload for _ in range(1)]
    assert a[0]["issue"]["fields"]["description"] == b[0]["issue"]["fields"]["description"]


def test_follow_up_adds_missing_information_as_comment() -> None:
    gen = IncidentGenerator(seed=3, start_number=100)
    issue = gen.new_issue()
    while issue.profile == "complete":
        issue = gen.new_issue()
    snapshot = merge_event(None, ADAPTER.normalize(issue.payload)).snapshot
    follow = None
    for _ in range(20):
        follow = gen.follow_up()
        if follow and follow["issue"]["key"] == issue.key:
            break
    assert follow is not None
    event = ADAPTER.normalize(follow)
    assert event.event_type is JiraEventType.COMMENT_CREATED
    if event.issue_key == snapshot.issue_key:
        merged = merge_event(snapshot, event)
        assert merged.content_changed
        assert merged.snapshot.active_comments()[0].body.startswith("Ek bilgi:")


def test_scenarios_cover_all_template_categories() -> None:
    keys = {s.key for s in SCENARIOS}
    assert {"eft", "cash", "card_reader", "printer", "login", "network", "kkb", "kps", "crash"} <= keys

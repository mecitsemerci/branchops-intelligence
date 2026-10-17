"""Bounded historical investigation: allow-listed tools, budgets, cancellation, evidence-checked findings."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from typing import Any

import pytest

from branchops.agents.base import StructuredCaller, StructuredOutputError
from branchops.agents.investigation import InvestigationAgent, InvestigationBudget
from branchops.application.interfaces.investigation import HistoryItem
from branchops.application.interfaces.model_provider import CompletionRequest
from branchops.domain.enums import StepStatus
from branchops.templates.registry import TemplateRegistry
from tests.helpers import ROOT, InMemoryHistory, MemorySink, ScriptedProvider, make_settings
from tests.unit.test_agents import _candidate
from tests.unit.test_workflow import initial, snapshot_from

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
CURRENT = {
    "incident_key": "jira:CUR-1",
    "summary": "EFT gönderiminde zaman aşımı",
    "error_signature": "x|EFT-504",
}
SOURCES = {"summary": "EFT gönderiminde zaman aşımı"}
OLD = HistoryItem(
    incident_key="jira:OLD-1",
    summary="EFT işlemleri EFT-504 hatasıyla düşüyor",
    description_excerpt="Gateway timeout",
    category="TRANSACTION",
    application="Şube Bankacılık",
    branch_code="2201",
    error_signature="x|EFT-504",
    created_at=T0,
)


class FakeReader:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def search_history(self, query: str, limit: int, exclude: str) -> list[HistoryItem]:
        self.asked.append(f"search:{query}")
        return [OLD]

    def issue_summary(self, incident_key: str) -> HistoryItem | None:
        self.asked.append(f"summary:{incident_key}")
        if incident_key == OLD.incident_key:
            return OLD
        return HistoryItem(incident_key, "EFT hatası", None, "TRANSACTION", None, "1234", None, T0)

    def same_signature(self, error_signature: str, limit: int, exclude: str) -> list[HistoryItem]:
        return [OLD]

    def known_resolutions(self, error_signature: str, limit: int, exclude: str) -> list[HistoryItem]:
        return []


def budget(**kw: Any) -> InvestigationBudget:
    base: dict[str, Any] = {"max_tool_calls": 3, "max_depth": 3, "max_seconds": 30, "max_tokens": 10_000}
    return InvestigationBudget(**{**base, **kw})


def call(tool: str, **arguments: str) -> Any:
    return lambda _r: json.dumps({"action": "call_tool", "tool": tool, "arguments": arguments})


def conclude(*findings: dict[str, Any]) -> Any:
    return lambda _r: json.dumps({"action": "conclude", "findings": list(findings)})


def agent(*answers: Any) -> tuple[InvestigationAgent, FakeReader, ScriptedProvider]:
    provider = ScriptedProvider({"InvestigationOutput": list(answers)})
    reader = FakeReader()
    return InvestigationAgent(StructuredCaller(provider, max_repair_attempts=0), reader), reader, provider


def run(a: InvestigationAgent, **kw: Any) -> Any:
    return a.run(current=CURRENT, candidates=[OLD], sources=SOURCES, correlation_id="c", budget=budget(**kw))


class TestFindings:
    def test_unsupported_observations_become_hypotheses(self) -> None:
        a, _, _ = agent(
            call("get_issue_summary", incident_key="jira:OLD-1"),
            conclude(
                {
                    "statement": "Aynı hata kodu daha önce görülmüş.",
                    "kind": "OBSERVATION",
                    "evidence": [{"source": "incident:jira:OLD-1", "quote": "EFT-504 hatasıyla düşüyor"}],
                    "related_incidents": ["jira:OLD-1", "jira:NOT-SURFACED"],
                },
                {"statement": "Kök neden EFT gateway sertifikası.", "kind": "OBSERVATION", "evidence": []},
                {
                    "statement": "Uydurma alıntı.",
                    "kind": "OBSERVATION",
                    "evidence": [{"source": "incident:jira:OLD-1", "quote": "bu metin hiçbir yerde yok"}],
                },
            ),
        )
        result, stats = run(a)
        assert result.status == "COMPLETED" and result.rounds == 2
        kinds = [(f.kind, [e.verified for e in f.evidence]) for f in result.findings]
        assert kinds == [("OBSERVATION", [True]), ("HYPOTHESIS", []), ("HYPOTHESIS", [False])]
        assert result.findings[0].related_incidents == ["jira:OLD-1"]  # unknown keys dropped
        assert [c.tool for c in result.tool_calls] == ["get_issue_summary"]
        assert stats.prompt_tokens == result.prompt_tokens > 0


class TestBounds:
    def test_only_surfaced_incident_keys_can_be_inspected(self) -> None:
        a, reader, _ = agent(call("issue_metadata", incident_key="jira:SECRET-9"), conclude())
        result, _ = run(a)
        assert result.tool_calls[0].error and "candidates" in result.tool_calls[0].error
        assert "summary:jira:SECRET-9" not in reader.asked

    def test_tool_call_budget_forces_a_conclusion(self) -> None:
        a, reader, provider = agent(*([call("search_history", query="EFT")] * 5), conclude())
        # The third turn must conclude; this model keeps asking for tools, so the call is rejected by the
        # schema and the step fails (the workflow degrades it) instead of running more tools.
        with pytest.raises(StructuredOutputError, match="must be 'conclude'"):
            run(a, max_tool_calls=2, max_depth=5)
        assert reader.asked == ["search:EFT", "search:EFT"]
        assert len(provider.calls_for("InvestigationOutput")) == 3

    def test_token_budget_and_cancellation(self) -> None:
        a, _, _ = agent(*([call("search_history", query="EFT")] * 5))
        result, _ = run(a, max_tokens=20, max_depth=5, max_tool_calls=5)
        assert result.status == "BUDGET_EXHAUSTED" and result.stopped_reason == "token budget exhausted"
        assert len(result.tool_calls) == 2  # each scripted call costs 15 tokens
        cancel = threading.Event()
        cancel.set()
        b, _, provider = agent(conclude())
        cancelled, _ = b.run(
            current=CURRENT,
            candidates=[OLD],
            sources=SOURCES,
            correlation_id="c",
            budget=budget(cancel=cancel),
        )
        assert cancelled.stopped_reason == "cancelled" and provider.requests == []


FINDING = {
    "statement": "Aynı hata kodu daha önce görülmüş.",
    "kind": "OBSERVATION",
    "evidence": [{"source": "incident:jira:OLD-1", "quote": "EFT-504 hatasıyla düşüyor"}],
    "related_incidents": ["jira:OLD-1"],
}


class TestMinimumToolCalls:
    """``min_tool_calls``: the conclude action is withheld until the model has consulted a tool."""

    @staticmethod
    def _agent(
        responders: dict[str, list[Any]], repairs: int = 1
    ) -> tuple[InvestigationAgent, ScriptedProvider]:
        provider = ScriptedProvider(responders)
        return InvestigationAgent(
            StructuredCaller(provider, max_repair_attempts=repairs), FakeReader()
        ), provider

    def test_first_turn_offers_no_conclude_action(self) -> None:
        a, provider = self._agent(
            {
                "InvestigationToolCall": [call("get_issue_summary", incident_key="jira:OLD-1")],
                "InvestigationOutput": [conclude(FINDING)],
            }
        )
        result, _ = run(a, min_tool_calls=1)
        first, second = provider.requests
        assert first.schema_name == "InvestigationToolCall"
        assert (first.json_schema or {})["properties"]["action"]["const"] == "call_tool"
        assert "conclude" not in json.dumps(first.json_schema)
        assert "conclude" not in first.messages[0].content.split("JSON Schema:")[1]
        assert "You have not consulted any tool yet" in first.messages[-1].content
        assert second.schema_name == "InvestigationOutput" and "conclude" in json.dumps(second.json_schema)
        assert "You have not consulted" not in second.messages[-1].content
        assert result.status == "COMPLETED" and [c.tool for c in result.tool_calls] == ["get_issue_summary"]
        assert result.findings[0].kind == "OBSERVATION"

    def test_conclusion_without_tools_is_repaired_once_then_degraded(self) -> None:
        a, provider = self._agent({"InvestigationToolCall": [conclude(FINDING), conclude(FINDING)]})
        result, stats = run(a, min_tool_calls=1)
        assert len(provider.requests) == 2  # the first answer and the single repair turn
        assert "not valid for the schema" in provider.requests[1].messages[-1].content
        assert result.status == "DEGRADED" and result.stopped_reason == "CONCLUDED_WITHOUT_TOOLS"
        assert result.tool_calls == [] and result.rounds == 1
        assert [(f.kind, [e.verified for e in f.evidence]) for f in result.findings] == [
            ("OBSERVATION", [True])
        ]
        assert stats.attempts == 2 and result.prompt_tokens > 0

    def test_repair_turn_that_calls_a_tool_continues_normally(self) -> None:
        a, _ = self._agent(
            {
                "InvestigationToolCall": [conclude(FINDING), call("search_history", query="EFT")],
                "InvestigationOutput": [conclude(FINDING)],
            }
        )
        result, _ = run(a, min_tool_calls=1)
        assert result.status == "COMPLETED" and [c.tool for c in result.tool_calls] == ["search_history"]

    def test_invalid_answers_other_than_a_conclusion_still_fail(self) -> None:
        bad = lambda _r: json.dumps({"action": "call_tool", "tool": "drop_table"})  # noqa: E731
        a, _ = self._agent({"InvestigationToolCall": [bad, bad]})
        with pytest.raises(StructuredOutputError):
            run(a, min_tool_calls=1)

    def test_zero_restores_the_previous_behaviour(self) -> None:
        a, provider = self._agent({"InvestigationOutput": [conclude(FINDING)]})
        result, _ = run(a, min_tool_calls=0)
        assert [r.schema_name for r in provider.requests] == ["InvestigationOutput"]
        assert "You have not consulted" not in provider.requests[0].messages[-1].content
        assert result.status == "COMPLETED" and result.tool_calls == []

    def test_minimum_never_overrides_the_tool_budget(self) -> None:
        a, provider = self._agent(
            {
                "InvestigationToolCall": [call("search_history", query="EFT")],
                "InvestigationOutput": [conclude(FINDING)],
            }
        )
        result, _ = run(a, min_tool_calls=3, max_tool_calls=1)
        assert [r.schema_name for r in provider.requests] == ["InvestigationToolCall", "InvestigationOutput"]
        assert result.status == "COMPLETED"

    def test_system_prompt_asks_for_turkish_findings(self) -> None:
        from branchops.agents.investigation import SYSTEM_PROMPT

        assert "finding statement in Turkish" in SYSTEM_PROMPT and "original language" in SYSTEM_PROMPT


class TestWorkflowStep:
    def _workflow(self, registry: TemplateRegistry, enabled: bool, history: InMemoryHistory) -> Any:
        from branchops.workflows.issue_analysis import IssueAnalysisWorkflow

        settings = make_settings(workflow={"investigation": {"enabled": enabled}})
        sink = MemorySink()
        return IssueAnalysisWorkflow(
            caller=StructuredCaller(ScriptedProvider()),
            registry=registry,
            policy=settings.scoring,
            history=history,
            sink=sink,
            settings=settings.workflow,
            investigation_reader=FakeReader(),
        )

    def test_runs_only_with_relevant_history(self, registry: TemplateRegistry) -> None:
        related = InMemoryHistory([_candidate("jira:BOPS-90")])
        wf = self._workflow(registry, True, related)
        result = wf.run(initial(snapshot_from("complete_incident")), thread_id="inv-1")
        step = next(s for s in result.steps if s.step == "investigate")
        assert step.status is StepStatus.SUCCEEDED
        assert result.investigation is not None and result.investigation.status == "COMPLETED"
        assert {f.kind for f in result.investigation.findings} <= {"OBSERVATION", "HYPOTHESIS"}

        nothing = self._workflow(registry, True, InMemoryHistory())
        quiet = nothing.run(initial(snapshot_from("complete_incident")), thread_id="inv-2")
        assert next(s for s in quiet.steps if s.step == "investigate").status is StepStatus.SKIPPED
        assert quiet.investigation is None

    def test_model_written_quotes_are_masked_before_storage(self, registry: TemplateRegistry) -> None:
        from branchops.agents.base import StructuredCaller as Caller
        from branchops.domain.services.masking import SensitiveDataMasker
        from branchops.workflows.issue_analysis import IssueAnalysisWorkflow

        def leaky(_r: CompletionRequest) -> str:
            return json.dumps(
                {
                    "action": "conclude",
                    "findings": [
                        {
                            "statement": "Müşteri 0532 123 45 67 aradı.",
                            "kind": "OBSERVATION",
                            "evidence": [{"source": "summary", "quote": "TCKN 10000000146"}],
                        }
                    ],
                }
            )

        settings = make_settings(workflow={"investigation": {"enabled": True}})
        wf = IssueAnalysisWorkflow(
            caller=Caller(ScriptedProvider({"InvestigationOutput": [leaky]})),
            registry=registry,
            policy=settings.scoring,
            history=InMemoryHistory([_candidate("jira:BOPS-90")]),
            sink=MemorySink(),
            settings=settings.workflow,
            investigation_reader=FakeReader(),
            text_filter=SensitiveDataMasker().mask_str,
        )
        result = wf.run(initial(snapshot_from("complete_incident")), thread_id="inv-mask")
        stored = result.model_dump_json()
        assert "0532 123 45 67" not in stored and "10000000146" not in stored
        assert result.investigation is not None and result.investigation.findings[0].kind == "HYPOTHESIS"

    def test_disabled_by_default(self, registry: TemplateRegistry) -> None:
        related = InMemoryHistory([_candidate("jira:BOPS-90")])
        result = self._workflow(registry, False, related).run(
            initial(snapshot_from("complete_incident")), thread_id="inv-3"
        )
        assert next(s for s in result.steps if s.step == "investigate").status is StepStatus.SKIPPED

    def test_requests_carry_no_sql_and_only_allow_listed_tools(self) -> None:
        a, _, provider = agent(conclude())
        run(a)
        request: CompletionRequest = provider.calls_for("InvestigationOutput")[0]
        schema = json.dumps(request.json_schema or {})
        for tool in ("search_history", "get_issue_summary", "issue_metadata", "known_resolutions"):
            assert tool in schema
        assert "sql" not in schema.lower()


class TestMockProviderDrivesInvestigation:
    """LLM_MODE=mock: the mock must make the investigate step succeed or skip, never fail."""

    @staticmethod
    def _ask(prompt: str) -> Any:
        from branchops.agents.investigation import InvestigationOutput
        from branchops.application.interfaces.model_provider import ChatMessage
        from branchops.infrastructure.llm.mock_provider import MockModelProvider

        request = CompletionRequest(
            messages=[ChatMessage("user", prompt)],
            agent="investigation",
            correlation_id="c",
            schema_name="InvestigationOutput",
        )
        content = MockModelProvider().complete(request).content
        return InvestigationOutput.model_validate_json(content, context={"must_conclude": True})

    @pytest.mark.parametrize(
        "prompt",
        [
            "",
            "<candidates>not json</candidates>",
            '<candidates>{"candidates": [{"summary": "anahtar yok"}], "must_conclude": false}</candidates>',
            '<candidates>{"candidates": [{"incident_key": "jira:OLD-1"}], "remaining_tool_calls": 0}'
            "</candidates>",
        ],
    )
    def test_concludes_when_no_valid_tool_call_is_possible(self, prompt: str) -> None:
        assert self._ask(prompt).action == "conclude"

    @pytest.mark.parametrize(
        ("candidates", "history", "tool"),
        [
            ([{"incident_key": "jira:OLD-1"}], [], "get_issue_summary"),
            ([], [], "known_resolutions"),
            ([{"summary": "anahtar yok"}], [], "known_resolutions"),
        ],
    )
    def test_calls_a_tool_while_one_is_required(
        self, candidates: list[dict[str, Any]], history: list[Any], tool: str
    ) -> None:
        payload = {"candidates": candidates, "remaining_tool_calls": 4, "tool_required": True}
        prompt = f"<candidates>{json.dumps(payload)}</candidates><history>{json.dumps(history)}</history>"
        from branchops.agents.investigation import InvestigationToolCall
        from branchops.application.interfaces.model_provider import ChatMessage
        from branchops.infrastructure.llm.mock_provider import MockModelProvider

        request = CompletionRequest(
            messages=[ChatMessage("user", prompt)],
            agent="investigation",
            correlation_id="c",
            schema_name="InvestigationToolCall",
        )
        out = InvestigationToolCall.model_validate_json(MockModelProvider().complete(request).content)
        assert out.tool == tool

    def test_demo_configuration_completes_the_step(self, registry: TemplateRegistry) -> None:
        import yaml

        from branchops.infrastructure.llm.mock_provider import MockModelProvider
        from branchops.workflows.issue_analysis import IssueAnalysisWorkflow

        docker = yaml.safe_load((ROOT / "config" / "docker.yaml").read_text("utf-8"))
        demo = docker["workflow"]["investigation"]
        assert demo["enabled"] is True and demo["min_tool_calls"] == 1
        settings = make_settings(workflow={"investigation": demo})
        wf = IssueAnalysisWorkflow(
            caller=StructuredCaller(MockModelProvider()),
            registry=registry,
            policy=settings.scoring,
            history=InMemoryHistory([_candidate("jira:BOPS-90")]),
            sink=MemorySink(),
            settings=settings.workflow,
            investigation_reader=FakeReader(),
        )
        result = wf.run(initial(snapshot_from("complete_incident")), thread_id="inv-mock")
        step = next(s for s in result.steps if s.step == "investigate")
        assert step.status is StepStatus.SUCCEEDED
        assert result.investigation is not None and result.investigation.status == "COMPLETED"
        assert [c.tool for c in result.investigation.tool_calls] == ["get_issue_summary"]

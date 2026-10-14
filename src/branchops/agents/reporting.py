"""ReportingAgent: narrative operational summary over deterministic metrics.

All numbers come from database queries. The model only phrases them; any number in its output that does
not appear in the metrics payload causes the narrative to be rejected and replaced by a deterministic one.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, ValidationInfo, model_validator

from branchops.agents.base import StructuredCaller, StructuredOutputError
from branchops.agents.prompts import json_block
from branchops.application.interfaces.model_provider import ModelError

AGENT_NAME = "reporting"
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, bool):
        return found
    if isinstance(value, int | float):
        for form in {f"{value}", f"{value:.0f}", f"{value:.1f}", f"{value:.2f}"}:
            found.add(form.rstrip("0").rstrip(".") if "." in form else form)
        return found
    if isinstance(value, str):
        return {n.replace(",", ".") for n in _NUMBER_RE.findall(value)}
    if isinstance(value, dict):
        for k, v in value.items():
            found |= _numbers(k) | _numbers(v)
    if isinstance(value, list):
        for v in value:
            found |= _numbers(v)
    return found


def unsupported_numbers(text: str, metrics: dict[str, Any]) -> list[str]:
    allowed = _numbers(metrics)
    bad: list[str] = []
    for raw in _NUMBER_RE.findall(text):
        n = raw.replace(",", ".")
        normalized = n.rstrip("0").rstrip(".") if "." in n else n
        if normalized not in allowed and n not in allowed:
            bad.append(raw)
    return bad


class ReportOutput(BaseModel):
    title: str = Field(max_length=160)
    summary: str = Field(max_length=2000)
    highlights: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _numbers_grounded(self, info: ValidationInfo) -> ReportOutput:
        ctx: dict[str, Any] = info.context or {}
        metrics = ctx.get("metrics")
        if metrics is not None:
            text = " ".join([self.title, self.summary, *self.highlights])
            bad = unsupported_numbers(text, metrics)
            if bad:
                raise ValueError(f"numbers not present in metrics: {bad[:10]}")
        return self


SYSTEM_PROMPT = """\
You write short operational summaries (in Turkish) for bank branch operations management.
Use only the figures in <metrics>; do not compute new numbers, percentages or trends that are not given.
Do not output your reasoning. Output JSON only.
"""


def deterministic_report(metrics: dict[str, Any]) -> ReportOutput:
    o = metrics.get("overview", {})
    highlights = [
        f"Toplam kayıt: {o.get('total_issues', 0)}",
        f"Ortalama tamlık: {o.get('avg_completeness', '-')}",
        f"Ortalama kalite: {o.get('avg_quality', '-')}",
        f"Düşük kaliteli kayıt: {o.get('low_quality_issues', 0)}",
    ]
    top_missing = metrics.get("most_missing_fields") or []
    if top_missing:
        first = top_missing[0]
        highlights.append(f"En sık eksik alan: {first.get('field')} ({first.get('count')})")
    return ReportOutput(
        title="Operasyonel kayıt kalitesi özeti",
        summary=" · ".join(highlights),
        highlights=highlights,
    )


class ReportingAgent:
    def __init__(self, caller: StructuredCaller | None) -> None:
        self._caller = caller

    def run(self, metrics: dict[str, Any], correlation_id: str) -> tuple[ReportOutput, str]:
        """Returns (report, source) where source is ``model`` or ``deterministic``."""
        if self._caller is None:
            return deterministic_report(metrics), "deterministic"
        try:
            result = self._caller.call(
                agent=AGENT_NAME,
                system_prompt=SYSTEM_PROMPT,
                user_prompt=json_block("metrics", metrics),
                schema=ReportOutput,
                correlation_id=correlation_id,
                validation_context={"metrics": metrics},
            )
        except (StructuredOutputError, ModelError):
            return deterministic_report(metrics), "deterministic"
        return result.value, "model"

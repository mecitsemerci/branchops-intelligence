"""Analysis workflow nodes and per-node state derivation for the live view."""

from __future__ import annotations

from typing import Any

WORKFLOW_NODES: tuple[tuple[str, str, bool], ...] = (
    # (node, Turkish label, is_llm_agent)
    ("normalize", "Normalizasyon", False),
    ("classify", "Sınıflandırma Ajanı", True),
    ("select_template", "Şablon Seçimi", False),
    ("extract", "Çıkarım Ajanı", True),
    ("assess_quality", "Kalite Değerlendirme Ajanı", True),
    ("score", "Puanlama Motoru", False),
    ("similarity", "Benzerlik Ajanı", True),
    ("recommend", "Öneri Ajanı", True),
    ("persist", "Kaydetme", False),
)


def step_node(step: str) -> str:
    """Map a recorded step name (e.g. ``similarity.verify``) to its workflow node."""
    return step.split(".", 1)[0]


def node_states(steps: list[dict[str, Any]], current_step: str | None, running: bool) -> dict[str, str]:
    """Per workflow node: pending | running | succeeded | failed | skipped."""
    states = {node: "pending" for node, _, _ in WORKFLOW_NODES}
    for st in steps:
        node = step_node(st["step"])
        status = st["status"]
        mapped = {
            "SUCCEEDED": "succeeded",
            "FAILED": "failed",
            "DEGRADED": "failed",
            "SKIPPED": "skipped",
        }.get(status, "succeeded")
        previous = states.get(node, "pending")
        # a node with several steps (similarity.retrieve + .verify): failure wins, then success over skip
        if previous == "failed" or (previous == "succeeded" and mapped == "skipped"):
            continue
        states[node] = mapped
    if running and current_step in states:
        states[current_step] = "running"
    return states

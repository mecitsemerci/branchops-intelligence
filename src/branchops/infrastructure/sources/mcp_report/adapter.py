"""Incidents reported through the MCP server (``report_incident`` / ``add_incident_update``).

The MCP command builds the canonical :class:`IncidentEvent` itself and stores it as an event envelope in the
inbox; this adapter only decodes (and masks) it in the worker. There is no HTTP endpoint for this source.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from branchops.application.interfaces.source_adapter import TextTransform, decode_event_envelope
from branchops.domain.entities.incident import IncidentEvent


class McpReportAdapter:
    def __init__(self, source_id: str) -> None:
        self.source_id = source_id

    def normalize(self, payload: Mapping[str, Any], transform: TextTransform | None = None) -> IncidentEvent:
        return decode_event_envelope(self.source_id, payload, transform)

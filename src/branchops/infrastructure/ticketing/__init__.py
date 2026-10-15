"""Ticketing sinks (Jira REST) and the outbox handler that drives them."""

from __future__ import annotations

from branchops.application.interfaces.ticketing import TicketingSink
from branchops.config.settings import Settings


def build_ticketing_sink(settings: Settings) -> TicketingSink | None:
    if settings.ticketing.sink == "jira":
        from branchops.infrastructure.ticketing.jira_sink import JiraTicketingSink

        return JiraTicketingSink(settings.ticketing, settings.network)
    return None

"""Deprecated Jira-named aliases of the source-neutral incident contracts (kept for one release).

New code should import from :mod:`branchops.domain.entities.incident`.
"""

from __future__ import annotations

from branchops.domain.entities.incident import IncidentComment, IncidentEvent, IncidentSnapshot

JiraComment = IncidentComment
JiraIssueSnapshot = IncidentSnapshot
NormalizedJiraEvent = IncidentEvent

__all__ = ["JiraComment", "JiraIssueSnapshot", "NormalizedJiraEvent"]

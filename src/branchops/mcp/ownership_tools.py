"""MCP read tools over the ownership registry: who will receive a report, before it is submitted."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from branchops.application.commands.ownership import OwnershipService
from branchops.domain.entities.ownership import RoutingInput
from branchops.mcp.runtime import McpRuntime

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
OWNERSHIP_TOOLS = frozenset({"list_teams", "resolve_owner"})


class TeamOut(BaseModel):
    key: str
    name: str
    kind: str = Field(description="branch_ops, app_owner, platform, security or other.")
    email: str | None
    oncall_contact: str | None
    working_hours: str | None = Field(description="e.g. 'Mon-Fri 08:30-17:30' (Europe/Istanbul).")


class TeamList(BaseModel):
    items: list[TeamOut]


class OwnerResolution(BaseModel):
    owner: TeamOut | None = Field(description="Team that will own the report.")
    to: list[TeamOut] = Field(description="Teams notified directly (owner + routing rules).")
    cc: list[TeamOut] = Field(description="Teams in copy; always includes branch operations.")
    matched_level: str = Field(
        description="SCREEN_KEY, SCREEN_ROUTE, APPLICATION_DEFAULT_SCREEN_OWNER, APPLICATION_OWNER, "
        "ROUTING_RULE or BRANCH_OPS_FALLBACK."
    )
    unowned_screen: bool = Field(description="No owner found: branch operations receives it as fallback.")
    application_name: str | None
    screen_title: str | None
    reasons: list[str]


def _team(t: dict[str, Any] | None) -> TeamOut | None:
    return TeamOut.model_validate(t) if t else None


def register_ownership_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    service = OwnershipService(runtime.db, runtime.settings)

    @mcp.tool(annotations=READ_ONLY)
    async def list_teams() -> TeamList:
        """List the active teams that receive incident reports, with public contacts and working hours."""
        runtime.current_caller()
        teams = await runtime.call(service.teams)
        return TeamList(items=[TeamOut.model_validate(t) for t in teams if t["active"]])

    @mcp.tool(annotations=READ_ONLY)
    async def resolve_owner(
        app_id: Annotated[
            str, Field(max_length=64, description="Application key of the host app, e.g. 'eft'.")
        ],
        screen_id: Annotated[
            str | None, Field(max_length=64, description="Screen key, e.g. 'eft-transfer'.")
        ] = None,
        route: Annotated[
            str | None,
            Field(max_length=500, description="Current route or internal URL path, e.g. '/eft/new'."),
        ] = None,
    ) -> OwnerResolution:
        """Tell the user who will receive the report for this application/screen before they submit it.

        Deterministic: screen owner, then application default screen owner, application owner, routing rules,
        else branch operations. Branch operations is always in copy.
        """
        runtime.current_caller()

        def run() -> dict[str, Any]:
            registry = service.registry()
            decision = service.resolve(
                RoutingInput(app_key=app_id, screen_key=screen_id, route=route), None, registry
            )
            return service.describe(decision, registry)

        d = await runtime.call(run)
        return OwnerResolution(
            owner=_team(d["owner"]),
            to=[x for x in (_team(t) for t in d["to"]) if x],
            cc=[x for x in (_team(t) for t in d["cc"]) if x],
            matched_level=str(d["matched_level"]),
            unowned_screen=bool(d["unowned_screen"]),
            application_name=d["application_name"],
            screen_title=d["screen_title"],
            reasons=list(d["reasons"]),
        )

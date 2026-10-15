"""``branchops mcp``: FastMCP app named "branchops" over streamable HTTP (``/mcp``) with probes.

Topology in OpenShift is the dashboard's: the server binds to 127.0.0.1 behind an oauth2-proxy sidecar that
authenticates the caller and forwards identity headers; :class:`McpAuthMiddleware` trusts them only from the
loopback peer. ``/healthz``, ``/readyz`` and ``/metrics`` are served without authentication, like the
worker's probe endpoints.
"""

from __future__ import annotations

import logging
from typing import Any

import anyio.to_thread
import fastmcp
from fastmcp import FastMCP
from starlette.middleware import Middleware as StarletteMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from branchops.application.queries.read_models import ReadModels
from branchops.config.settings import Settings
from branchops.container import Container
from branchops.domain.services.masking import SensitiveDataMasker
from branchops.infrastructure.observability.telemetry import setup_telemetry
from branchops.infrastructure.security.auth import resolve_principal
from branchops.mcp.auth import Caller, McpAuthMiddleware, RateLimiter, resolve_teams, validate_mcp_settings
from branchops.mcp.middleware import ToolGuardMiddleware
from branchops.mcp.prompts import register_prompts
from branchops.mcp.resources import register_resources
from branchops.mcp.runtime import McpRuntime
from branchops.mcp.tools import TOOL_NAMES, register_tools

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "BranchOps Intelligence: read-only access to analysed bank-branch operational incidents (Jira, "
    "ServiceNow, imports). Scores are 0-100 and computed deterministically from versioned templates; "
    "evidence quotes are verified against the source text. You only see incidents your team may see. "
    "Incident keys look like 'jira:OPS-123'. Start with search_incidents or get_quality_overview; read "
    "branchops://glossary for category, field and status meanings."
)


def build_server(
    settings: Settings, container: Container, *, stdio: bool = False
) -> tuple[FastMCP, McpRuntime]:
    validate_mcp_settings(settings, TOOL_NAMES)
    # Belt and braces next to FASTMCP_CHECK_FOR_UPDATES=off (branchops.mcp): never call PyPI.
    fastmcp.settings.check_for_updates = "off"
    runtime = McpRuntime(
        settings=settings,
        db=container.db,
        read_models=ReadModels(settings.scoring.low_quality_threshold),
        templates=container.registry,
        masker=SensitiveDataMasker(settings.security.masking_extra_patterns),
    )
    if stdio:
        runtime.stdio_caller = stdio_caller(settings)
    server: FastMCP = FastMCP(
        name="branchops",
        instructions=INSTRUCTIONS,
        version="0.1.0",
        mask_error_details=True,  # unexpected exceptions never leak internals; ToolError messages pass
    )
    server.add_middleware(ToolGuardMiddleware(runtime))
    register_tools(server, runtime)
    register_resources(server, runtime)
    register_prompts(server)
    _register_probes(server, runtime)
    return server, runtime


def _register_probes(server: FastMCP, runtime: McpRuntime) -> None:
    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> Response:
        return PlainTextResponse("ok")

    @server.custom_route("/readyz", methods=["GET"])
    async def readyz(_request: Request) -> Response:
        try:
            ok = await anyio.to_thread.run_sync(runtime.db.ping)
        except Exception:
            ok = False
        return PlainTextResponse("ready" if ok else "not ready", status_code=200 if ok else 503)

    @server.custom_route("/metrics", methods=["GET"])
    async def metrics(_request: Request) -> Response:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def stdio_caller(settings: Settings) -> Caller:
    """The local developer for ``--stdio`` (no HTTP, so no proxy headers): dev auth only, never production."""
    if settings.app.environment == "production":
        raise ValueError("mcp: the stdio transport is not allowed in production")
    if settings.security.auth_mode != "dev":
        raise ValueError("mcp: the stdio transport requires security.auth_mode 'dev'")
    principal = resolve_principal({}, None, settings.security)
    teams = resolve_teams(principal, settings)
    if not teams:
        raise ValueError("mcp: the dev principal (security.dev_groups) matches no mcp team")
    return Caller(principal, teams)


def http_app(server: FastMCP, settings: Settings) -> Any:
    """Starlette app: streamable HTTP at ``settings.mcp.path`` behind the authentication middleware.

    Stateless JSON mode: every request is self-contained, so replicas scale horizontally without session
    affinity and nothing is buffered as a long-lived stream in the proxy.
    """
    limiter = RateLimiter(settings.mcp.rate_limit_per_minute)
    return server.http_app(
        path=settings.mcp.path,
        transport="streamable-http",
        stateless_http=True,
        json_response=True,
        middleware=[
            StarletteMiddleware(McpAuthMiddleware, settings=settings, path=settings.mcp.path, limiter=limiter)
        ],
    )


def run(settings: Settings, *, stdio: bool = False) -> None:
    setup_telemetry(settings, role="mcp")
    container = Container.build(settings)
    try:
        server, _runtime = build_server(settings, container, stdio=stdio)
        if stdio:
            logger.warning("mcp server on stdio (development only)")
            server.run(transport="stdio", show_banner=False)
            return
        import uvicorn

        logger.info("mcp server starting", extra={"host": settings.mcp.host, "port": settings.mcp.port})
        uvicorn.run(
            http_app(server, settings),
            host=settings.mcp.host,
            port=settings.mcp.port,
            proxy_headers=False,
            server_header=False,
            log_config=None,
        )
    finally:
        container.close()

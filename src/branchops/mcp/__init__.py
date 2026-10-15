"""MCP server (``branchops mcp``) exposing verified, team-scoped incident data to MCP clients.

The server never calls the LLM and never runs model- or client-written SQL: every read tool is a fixed,
parameterised query from ``application/queries`` with the caller's team scope applied in SQL. The only
writes are request_reanalysis and the reporting tools, which enqueue inbox / outbox rows.
"""

import importlib
import os
import warnings

# fastmcp reads these at import time. The server must never reach the internet (air-gapped deployment):
# no PyPI update check, no CLI banner. Set before any ``fastmcp`` import in this package.
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")
os.environ.setdefault("FASTMCP_SHOW_CLI_BANNER", "false")

# fastmcp 2.x imports authlib.jose for its (unused here) JWT provider, and authlib forces its deprecation
# warnings to "always" when imported; register the ignore filter after that so start-up logs stay clean.
_authlib_deprecate = importlib.import_module("authlib.deprecate")
warnings.filterwarnings("ignore", category=_authlib_deprecate.AuthlibDeprecationWarning)

"""FastMCP middleware: per-team tool visibility, and audit / metrics / logging of every tool call."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Sequence
from typing import Any

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import Tool, ToolResult

from branchops.domain.services.masking import SensitiveDataMasker
from branchops.infrastructure.observability.logging import correlation_id_var
from branchops.infrastructure.observability.telemetry import get_instruments
from branchops.infrastructure.persistence.audit import audit
from branchops.mcp.auth import AccessDeniedError
from branchops.mcp.runtime import McpRuntime, tool_caller_var
from branchops.mcp.tools import TOOL_NAMES

logger = logging.getLogger("branchops.mcp")

_SECRET_KEY_RE = re.compile(r"(?i)token|secret|password|passwd|authorization|api[_-]?key|cookie")
MAX_AUDIT_VALUE_CHARS = 200
MAX_AUDIT_ITEMS = 20
MAX_AUDIT_DEPTH = 3
# Same message for "hidden" and "does not exist": a caller cannot probe which tools other teams have.
UNKNOWN_TOOL = "Unknown tool: {name}"
UNKNOWN_TOOL_LABEL = "<unknown>"


def audit_arguments(arguments: dict[str, Any] | None, masker: SensitiveDataMasker) -> dict[str, Any]:
    """Tool arguments for the audit log: secrets dropped, free text PII-masked and shortened.

    Runs before schema validation (also for denied calls), so the input is arbitrary JSON: nesting depth,
    item counts and string lengths are bounded *before* masking. Strings are cut to a window larger than
    the stored length plus the longest PII pattern, masked, then cut again, so no partial PII survives.
    """

    def clean(key: str, value: Any, depth: int) -> Any:
        if _SECRET_KEY_RE.search(key):
            return "***"
        if isinstance(value, str):
            masked = masker.mask_text(value[: MAX_AUDIT_VALUE_CHARS * 2]) or ""
            return masked if len(masked) <= MAX_AUDIT_VALUE_CHARS else masked[:MAX_AUDIT_VALUE_CHARS] + "…"
        if isinstance(value, bool | int | float) or value is None:
            return value
        if depth >= MAX_AUDIT_DEPTH:
            return "…"
        if isinstance(value, list):
            return [clean(key, v, depth + 1) for v in value[:MAX_AUDIT_ITEMS]]
        if isinstance(value, dict):
            return {
                str(k)[:64]: clean(str(k), v, depth + 1) for k, v in list(value.items())[:MAX_AUDIT_ITEMS]
            }
        return clean(key, str(value)[: MAX_AUDIT_VALUE_CHARS * 2], depth)

    return {str(k)[:64]: clean(str(k), v, 0) for k, v in list((arguments or {}).items())[:MAX_AUDIT_ITEMS]}


def result_rows(result: ToolResult | None) -> int:
    """Number of records returned: ``len(items)`` for list results, otherwise 1 (0 for no result)."""
    if result is None:
        return 0
    data = result.structured_content
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            return len(items)
        if isinstance(data.get("result"), list):
            return len(data["result"])
    return 1


class ToolGuardMiddleware(Middleware):
    def __init__(self, runtime: McpRuntime) -> None:
        self._runtime = runtime

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        caller = self._runtime.request_caller()
        tools = await call_next(context)
        return [t for t in tools if caller.allows_tool(t.name)]

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        name = context.message.name
        # The requested name is client-controlled: only catalogue names become metric labels, log fields and
        # audit resource ids (unbounded label cardinality / oversized audit rows otherwise).
        tool = name if name in TOOL_NAMES else UNKNOWN_TOOL_LABEL
        caller = self._runtime.request_caller()
        token = correlation_id_var.set(f"mcp:{uuid.uuid4().hex[:24]}")
        caller_token = tool_caller_var.set(caller.for_tool(name))
        started = time.perf_counter()
        outcome, error, result = "ok", None, None
        try:
            if not caller.allows_tool(name):
                outcome = "denied"
                raise ToolError(UNKNOWN_TOOL.format(name=name[:100]))
            result = await call_next(context)
            return result
        except ToolError as exc:
            outcome = "denied" if outcome == "denied" else "rejected"
            error = str(exc)[:300]
            raise
        except AccessDeniedError:
            outcome = "denied"
            raise
        except Exception as exc:
            outcome = "error"
            error = type(exc).__name__
            raise
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            details = {
                "team": caller.team_label,
                "arguments": audit_arguments(context.message.arguments, self._runtime.masker),
                "rows": result_rows(result),
                "duration_ms": duration_ms,
                "outcome": outcome,
                **({"error": error} if error else {}),
                **({"requested_tool": name[:MAX_AUDIT_VALUE_CHARS]} if tool != name else {}),
            }
            try:
                await self._runtime.call(lambda: self._write_audit(caller.username, tool, details))
            except Exception:  # an audit failure must be visible but must not mask the tool outcome
                logger.exception("mcp audit write failed", extra={"tool": tool})
            labels = {"tool": tool, "team": caller.team_label, "outcome": outcome}
            instruments = get_instruments()
            instruments.mcp_tool_calls.add(1, labels)
            instruments.mcp_tool_latency_ms.record(duration_ms, labels)
            logger.info(
                f"mcp tool {tool} {outcome}",
                extra={
                    "actor": caller.username,
                    "team": caller.team_label,
                    "tool": tool,
                    "outcome": outcome,
                    "rows": details["rows"],
                    "duration_ms": duration_ms,
                },
            )
            tool_caller_var.reset(caller_token)
            correlation_id_var.reset(token)

    def _write_audit(self, actor: str, tool: str, details: dict[str, Any]) -> None:
        with self._runtime.db.session() as s:
            audit(
                s,
                actor=actor,
                action="mcp_tool_call",
                resource_type="mcp_tool",
                resource_id=tool,
                details=details,
            )

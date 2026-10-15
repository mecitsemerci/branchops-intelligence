"""Shared state of the MCP process: database, read models, templates and the current caller."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TypeVar

import anyio.to_thread
from fastmcp.server.dependencies import get_http_request
from sqlalchemy.orm import Session

from branchops.application.queries.read_models import ReadModels
from branchops.config.settings import Settings
from branchops.domain.services.masking import SensitiveDataMasker
from branchops.infrastructure.database.session import Database
from branchops.mcp.auth import CALLER_STATE_KEY, AccessDeniedError, Caller
from branchops.mcp.output import TextRenderer
from branchops.templates.registry import TemplateRegistry

T = TypeVar("T")
_PII_CHARS = re.compile(r"[\d@]")
_INCIDENT_KEY_TOKEN = re.compile(r"(?:[a-z][a-z0-9_-]*:)?[A-Za-z][A-Za-z0-9_]*-\d{1,10}")

# The caller narrowed to the teams that grant the tool being called (set by ToolGuardMiddleware).
tool_caller_var: ContextVar[Caller | None] = ContextVar("mcp_tool_caller", default=None)


@dataclass
class McpRuntime:
    settings: Settings
    db: Database
    read_models: ReadModels
    templates: TemplateRegistry
    masker: SensitiveDataMasker
    # Set only for the development stdio transport (no HTTP request, dev principal).
    stdio_caller: Caller | None = None

    def current_caller(self) -> Caller:
        """The caller of the running tool (narrowed to the teams granting it), else the request's caller."""
        if (narrowed := tool_caller_var.get()) is not None:
            return narrowed
        return self.request_caller()

    def request_caller(self) -> Caller:
        """The caller authenticated by :class:`~branchops.mcp.auth.McpAuthMiddleware` for this request."""
        try:
            request = get_http_request()
        except RuntimeError:
            if self.stdio_caller is not None:
                return self.stdio_caller
            raise AccessDeniedError(401, "unauthenticated", "no authenticated request") from None
        caller = request.scope.get("state", {}).get(CALLER_STATE_KEY)
        if not isinstance(caller, Caller):
            raise AccessDeniedError(401, "unauthenticated", "no authenticated request")
        return caller

    def renderer(
        self, caller: Caller, source: str | None, group_key: str | None, branch_code: str | None
    ) -> TextRenderer:
        return TextRenderer(
            masker=self.masker,
            max_chars=self.settings.mcp.max_text_chars,
            mask=not caller.full_text_for(source, group_key, branch_code),
        )

    def search_text(self, caller: Caller, text: str) -> str:
        """Free-text search input, safe against PII probing for callers that see masked text.

        The search runs on the stored (unmasked-for-this-caller) text, so masking the query alone is not
        enough: PostgreSQL tokenises differently from the masker (``4111 1111``, ``ayse.yilmaz example.com``
        evade the masker but still match). Unless *every* team of the caller returns full text, every
        token that contains a digit or ``@`` is dropped, except incident-key-like tokens (``OPS-123``);
        the masked PII classes (TCKN, IBAN, card, phone, e-mail) all need digits or ``@``.
        """
        if caller.full_text_everywhere:
            return text
        kept = [t for t in text.split() if _INCIDENT_KEY_TOKEN.fullmatch(t) or not _PII_CHARS.search(t)]
        return self.masker.mask_text(" ".join(kept)) or ""

    async def query(self, fn: Callable[[Session], T]) -> T:
        """Run a synchronous read in a worker thread (the event loop never blocks on PostgreSQL)."""

        def run() -> T:
            with self.db.session() as s:
                return fn(s)

        return await anyio.to_thread.run_sync(run)

    async def call(self, fn: Callable[[], T]) -> T:
        return await anyio.to_thread.run_sync(fn)

    @property
    def page_size_limit(self) -> int:
        return self.settings.mcp.max_page_size

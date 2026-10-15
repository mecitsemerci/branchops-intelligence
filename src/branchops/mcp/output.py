"""Size bounds and per-team masking for text returned by MCP tools."""

from __future__ import annotations

from dataclasses import dataclass, field

from branchops.domain.services.masking import SensitiveDataMasker


@dataclass
class TextRenderer:
    """Applies the team's field mode and the size limit to free text, recording what was truncated.

    Truncated values end with an explicit marker (``…[truncated: 4000 of 9120 chars]``) and the field path
    is added to :attr:`truncated` so clients can tell a cut value from a short one.
    """

    masker: SensitiveDataMasker
    max_chars: int
    mask: bool
    truncated: list[str] = field(default_factory=list)

    def __call__(self, text: str | None, path: str, *, limit: int | None = None) -> str | None:
        if text is None:
            return None
        value = (self.masker.mask_text(text) or "") if self.mask else text
        return self.clip(value, path, limit=limit)

    def clip(self, text: str, path: str, *, limit: int | None = None) -> str:
        cap = min(limit or self.max_chars, self.max_chars)
        if len(text) <= cap:
            return text
        self.truncated.append(path)
        return f"{text[:cap]}…[truncated: {cap} of {len(text)} chars]"

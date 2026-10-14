"""Immutable value objects."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from branchops.domain.enums import FieldState, ProvenanceSource

MAX_QUOTE_LENGTH = 400


class EvidenceRef(BaseModel):
    """A pointer to the part of the source issue that supports a value.

    ``source`` is one of ``summary``, ``description``, ``comment:<id>`` or ``field:<jira field id>``.
    ``verified`` is set by Python (never by the model) after checking that ``quote`` really occurs in
    the referenced source text.
    """

    model_config = ConfigDict(frozen=True)

    source: str = Field(min_length=1, max_length=120)
    quote: str = Field(default="", max_length=MAX_QUOTE_LENGTH)
    verified: bool = False


class Provenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: ProvenanceSource = ProvenanceSource.NONE
    source_path: str | None = None
    model_id: str | None = None
    recorded_at: datetime | None = None


class ExtractedField(BaseModel):
    """A canonical field value together with its information state and provenance."""

    model_config = ConfigDict(frozen=True)

    value: str | None = None
    state: FieldState = FieldState.MISSING
    evidence: tuple[EvidenceRef, ...] = ()
    provenance: Provenance = Provenance()
    note: str | None = Field(default=None, max_length=500)

    @property
    def has_value(self) -> bool:
        return self.value is not None and self.value.strip() != ""

    @classmethod
    def missing(cls, note: str | None = None) -> ExtractedField:
        return cls(state=FieldState.MISSING, note=note)

    @classmethod
    def unknown(cls, note: str | None = None) -> ExtractedField:
        return cls(state=FieldState.UNKNOWN, note=note)

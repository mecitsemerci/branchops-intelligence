"""Canonical issue model: the structured, provenance-carrying view of an operational incident."""

from __future__ import annotations

from datetime import datetime
from typing import Final

from pydantic import AliasChoices, BaseModel, Field

from branchops.domain.enums import FieldState
from branchops.domain.value_objects import ExtractedField

# Fields that templates can weight and score. Order is the display order.
CANONICAL_FIELDS: Final[tuple[str, ...]] = (
    "summary",
    "description",
    "branch_code",
    "application",
    "application_version",
    "category",
    "subcategory",
    "business_process",
    "business_impact",
    "error_details",
    "occurrence_time",
    "reproduction_steps",
    "frequency",
    "environment",
    "troubleshooting_steps",
    "evidence",
)

# Fields the extraction agent is asked to extract from free text.
LLM_EXTRACTABLE_FIELDS: Final[tuple[str, ...]] = (
    "branch_code",
    "application",
    "application_version",
    "business_process",
    "business_impact",
    "error_details",
    "occurrence_time",
    "reproduction_steps",
    "frequency",
    "environment",
    "troubleshooting_steps",
    "evidence",
)

FIELD_LABELS_TR: Final[dict[str, str]] = {
    "summary": "Özet",
    "description": "Açıklama",
    "branch_code": "Şube kodu",
    "application": "Uygulama",
    "application_version": "Uygulama sürümü",
    "category": "Kategori",
    "subcategory": "Alt kategori",
    "business_process": "İş süreci",
    "business_impact": "İş etkisi",
    "error_details": "Hata detayı",
    "occurrence_time": "Oluşma zamanı",
    "reproduction_steps": "Tekrarlama adımları",
    "frequency": "Sıklık",
    "environment": "Ortam",
    "troubleshooting_steps": "Yapılan kontroller",
    "evidence": "Kanıt (ekran görüntüsü / log)",
}


class CanonicalIssue(BaseModel):
    # Legacy names (``issue_key``, ``project``) are accepted for analyses persisted before multi-source.
    incident_key: str = Field(validation_alias=AliasChoices("incident_key", "issue_key"))
    source: str = "jira"
    group_key: str | None = Field(default=None, validation_alias=AliasChoices("group_key", "project"))
    status: str | None = None
    source_created_at: datetime | None = None
    source_updated_at: datetime | None = None
    fields: dict[str, ExtractedField] = Field(default_factory=dict)

    def field(self, name: str) -> ExtractedField:
        return self.fields.get(name, ExtractedField.missing())

    def value(self, name: str) -> str | None:
        f = self.field(name)
        return f.value if f.has_value else None

    def states(self) -> dict[str, FieldState]:
        return {name: self.field(name).state for name in CANONICAL_FIELDS}

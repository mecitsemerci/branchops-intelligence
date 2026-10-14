"""Versioned issue-template contracts."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from branchops.domain.entities.issue import CANONICAL_FIELDS

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

ConditionOp = Literal["equals", "not_equals", "in", "not_in", "present", "absent", "matches"]


class Condition(BaseModel):
    """Declarative condition evaluated against the canonical issue context.

    Either a combinator (``all`` / ``any`` / ``not``) or a leaf (``field`` + ``op`` [+ ``value``]).
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    all: list[Condition] | None = None
    any: list[Condition] | None = None
    not_: Condition | None = Field(default=None, alias="not")
    field: str | None = None
    op: ConditionOp | None = None
    value: Any = None

    @model_validator(mode="after")
    def _shape(self) -> Condition:
        combinators = [x for x in (self.all, self.any, self.not_) if x is not None]
        is_leaf = self.field is not None
        if len(combinators) + int(is_leaf) != 1:
            raise ValueError("condition must be exactly one of all/any/not/leaf")
        if is_leaf:
            if self.op is None:
                raise ValueError("leaf condition requires 'op'")
            if self.op in ("in", "not_in") and not isinstance(self.value, list):
                raise ValueError(f"op '{self.op}' requires a list value")
            if self.op in ("equals", "not_equals", "matches") and self.value is None:
                raise ValueError(f"op '{self.op}' requires a value")
            if self.op == "matches":
                re.compile(str(self.value))
        return self


class ValidationRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pattern: str | None = None
    min_length: int | None = Field(default=None, ge=0)
    max_length: int | None = Field(default=None, ge=1)
    min_words: int | None = Field(default=None, ge=0)
    allowed_values: list[str] | None = None
    reject_generic_phrases: list[str] = Field(default_factory=list)

    @field_validator("pattern")
    @classmethod
    def _compile(cls, v: str | None) -> str | None:
        if v is not None:
            re.compile(v)
        return v


class Rubric(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    criteria: str
    good: str
    acceptable: str
    poor: str


class FieldDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    label: str
    weight: float = Field(gt=0, le=100)
    required: bool = False
    required_when: Condition | None = None
    applicable_when: Condition | None = None
    allow_not_applicable: bool = False
    validation: ValidationRule = ValidationRule()
    rubric: Rubric
    recommendation: str = Field(min_length=5)

    @field_validator("name")
    @classmethod
    def _canonical(cls, v: str) -> str:
        if v not in CANONICAL_FIELDS:
            raise ValueError(f"unknown canonical field '{v}'")
        return v


class IssueTemplate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    template_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    version: str
    status: Literal["draft", "published"] = "published"
    name: str
    description: str = ""
    category: str = Field(pattern=r"^[A-Z_]+$")
    subcategories: list[str] = Field(default_factory=list)
    is_fallback: bool = False
    applicable_when: Condition | None = None
    fields: list[FieldDefinition] = Field(min_length=1)

    @field_validator("version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not SEMVER_RE.match(v):
            raise ValueError("version must be MAJOR.MINOR.PATCH")
        return v

    @model_validator(mode="after")
    def _unique_fields(self) -> IssueTemplate:
        names = [f.name for f in self.fields]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate field definitions: {sorted(dupes)}")
        return self

    @property
    def key(self) -> tuple[str, str]:
        return (self.template_id, self.version)

    def field_def(self, name: str) -> FieldDefinition | None:
        return next((f for f in self.fields if f.name == name), None)

    def content_hash(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json", by_alias=True), sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def version_tuple(self) -> tuple[int, int, int]:
        major, minor, patch = (int(x) for x in self.version.split("."))
        return (major, minor, patch)

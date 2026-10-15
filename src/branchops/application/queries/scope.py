"""Row-level visibility scopes applied as SQL predicates.

A :class:`QueryScope` is a set of grants; a row is visible when it matches **any** grant, and it matches a
grant when its source, group key and branch code are each allowed (``None`` = any value). The default
scope is unrestricted, so callers that do not pass one (REST API, dashboard) see everything exactly as
before. Scopes are enforced inside the query (``WHERE``), never by filtering results afterwards.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, false, or_
from sqlalchemy.sql.elements import ColumnElement

from branchops.infrastructure.database.models import Issue

WILDCARD = "*"


def _allow(values: Iterable[str] | None) -> frozenset[str] | None:
    if values is None:
        return None
    allowed = frozenset(v for v in values if v)
    return None if WILDCARD in allowed else allowed


@dataclass(frozen=True)
class ScopeGrant:
    """Allowed values per dimension; ``None`` = unrestricted, an empty set = nothing."""

    sources: frozenset[str] | None = None
    group_keys: frozenset[str] | None = None
    branch_codes: frozenset[str] | None = None

    @classmethod
    def of(
        cls,
        *,
        sources: Iterable[str] | None = None,
        group_keys: Iterable[str] | None = None,
        branch_codes: Iterable[str] | None = None,
    ) -> ScopeGrant:
        """Build a grant from configuration lists in which ``"*"`` means unrestricted."""
        return cls(_allow(sources), _allow(group_keys), _allow(branch_codes))

    @property
    def unrestricted(self) -> bool:
        return self.sources is None and self.group_keys is None and self.branch_codes is None

    def matches(self, source: str | None, group_key: str | None, branch_code: str | None) -> bool:
        return all(
            allowed is None or (value is not None and value in allowed)
            for allowed, value in (
                (self.sources, source),
                (self.group_keys, group_key),
                (self.branch_codes, branch_code),
            )
        )


@dataclass(frozen=True)
class QueryScope:
    """Union of grants; ``grants=None`` = unrestricted, ``grants=()`` = no rows at all."""

    grants: tuple[ScopeGrant, ...] | None = None

    @classmethod
    def unrestricted(cls) -> QueryScope:
        return cls(None)

    @classmethod
    def any_of(cls, grants: Iterable[ScopeGrant]) -> QueryScope:
        grants = tuple(grants)
        return cls(None) if any(g.unrestricted for g in grants) else cls(grants)

    @property
    def is_unrestricted(self) -> bool:
        return self.grants is None

    def matches(self, source: str | None, group_key: str | None, branch_code: str | None) -> bool:
        return self.grants is None or any(g.matches(source, group_key, branch_code) for g in self.grants)

    def predicate(self, model: type[Issue] = Issue) -> ColumnElement[bool] | None:
        """SQLAlchemy predicate over ``issues`` columns, or ``None`` when unrestricted."""
        if self.grants is None:
            return None
        clauses: list[ColumnElement[bool]] = []
        for g in self.grants:
            parts: list[ColumnElement[bool]] = []
            if g.sources is not None:
                parts.append(model.source.in_(sorted(g.sources)) if g.sources else false())
            if g.group_keys is not None:
                parts.append(model.group_key.in_(sorted(g.group_keys)) if g.group_keys else false())
            if g.branch_codes is not None:
                parts.append(model.branch_code.in_(sorted(g.branch_codes)) if g.branch_codes else false())
            clauses.append(and_(*parts))
        return or_(*clauses) if clauses else false()

    def sql(self, alias: str = "") -> tuple[str, dict[str, Any]]:
        """``(" AND (...)", params)`` for raw ``text()`` queries over ``issues``; ``("", {})`` if unrestricted.

        ``alias`` is the table alias of ``issues`` in the query (``"i"`` -> ``i.source``). Values are bound
        parameters (PostgreSQL arrays), never interpolated.
        """
        if self.grants is None:
            return "", {}
        prefix = f"{alias}." if alias else ""
        params: dict[str, Any] = {}
        clauses: list[str] = []
        for n, g in enumerate(self.grants):
            parts: list[str] = []
            for column, allowed in (
                ("source", g.sources),
                ("group_key", g.group_keys),
                ("branch_code", g.branch_codes),
            ):
                if allowed is None:
                    continue
                name = f"scope_{n}_{column}"
                params[name] = sorted(allowed)
                parts.append(f"{prefix}{column} = ANY(:{name})")
            clauses.append("(" + (" AND ".join(parts) or "TRUE") + ")")
        return f" AND ({' OR '.join(clauses) or 'FALSE'})", params


UNRESTRICTED = QueryScope.unrestricted()

"""In-memory registry of versioned templates loaded from ``templates/<template_id>/<version>.json``."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from branchops.templates.model import IssueTemplate
from branchops.templates.validation import ConditionContext, evaluate, lint_template


class TemplateError(Exception):
    pass


class TemplateRegistry:
    def __init__(self, templates: Iterable[IssueTemplate]) -> None:
        self._by_key: dict[tuple[str, str], IssueTemplate] = {}
        for t in templates:
            if t.key in self._by_key:
                raise TemplateError(f"duplicate template version {t.template_id}@{t.version}")
            problems = lint_template(t)
            if problems:
                raise TemplateError(f"{t.template_id}@{t.version}: {'; '.join(problems)}")
            self._by_key[t.key] = t
        if not any(t.is_fallback and t.status == "published" for t in self._by_key.values()):
            raise TemplateError("at least one published fallback template is required")

    @classmethod
    def from_directory(cls, root: Path) -> TemplateRegistry:
        if not root.is_dir():
            raise TemplateError(f"template directory not found: {root}")
        templates: list[IssueTemplate] = []
        for path in sorted(root.glob("*/*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                template = IssueTemplate.model_validate(data)
            except (ValueError, OSError) as exc:
                raise TemplateError(f"invalid template file {path}: {exc}") from exc
            if path.parent.name != template.template_id or path.stem != template.version:
                raise TemplateError(f"{path}: file location must be <template_id>/<version>.json")
            templates.append(template)
        return cls(templates)

    def get(self, template_id: str, version: str) -> IssueTemplate | None:
        return self._by_key.get((template_id, version))

    def published(self) -> list[IssueTemplate]:
        return [t for t in self._by_key.values() if t.status == "published"]

    def all_versions(self) -> list[IssueTemplate]:
        return sorted(self._by_key.values(), key=lambda t: (t.template_id, t.version_tuple()))

    def latest_published(self) -> list[IssueTemplate]:
        latest: dict[str, IssueTemplate] = {}
        for t in self.published():
            cur = latest.get(t.template_id)
            if cur is None or t.version_tuple() > cur.version_tuple():
                latest[t.template_id] = t
        return sorted(latest.values(), key=lambda t: t.template_id)

    def taxonomy(self) -> dict[str, list[str]]:
        """Category -> allowed subcategories, derived from the latest published templates."""
        tax: dict[str, set[str]] = {}
        for t in self.latest_published():
            tax.setdefault(t.category, set()).update(t.subcategories)
        return {k: sorted(v) for k, v in sorted(tax.items())}

    def category_descriptions(self) -> dict[str, str]:
        """Category -> description (template name + description), for the classification prompt."""
        out: dict[str, list[str]] = {}
        for t in self.latest_published():
            out.setdefault(t.category, []).append(f"{t.name}: {t.description}".strip(": "))
        return {k: " / ".join(v) for k, v in sorted(out.items())}

    def fallback(self) -> IssueTemplate:
        return next(t for t in self.latest_published() if t.is_fallback)

    def select(self, category: str | None, subcategory: str | None, ctx: ConditionContext) -> IssueTemplate:
        """Pick the most specific applicable template; deterministic for a given input."""
        candidates: list[tuple[int, IssueTemplate]] = []
        for t in self.latest_published():
            if t.is_fallback or category is None or t.category != category:
                continue
            if not evaluate(t.applicable_when, ctx):
                continue
            if subcategory and subcategory in t.subcategories:
                candidates.append((2, t))
            elif not t.subcategories or not subcategory:
                candidates.append((1, t))
            else:
                candidates.append((0, t))
        if not candidates:
            return self.fallback()
        candidates.sort(key=lambda c: (-c[0], c[1].template_id))
        return candidates[0][1]

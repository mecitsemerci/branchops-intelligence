"""Publishes template versions to the database and enforces immutability of published versions."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from branchops.infrastructure.database.models import TemplateVersion
from branchops.templates.model import IssueTemplate
from branchops.templates.registry import TemplateError, TemplateRegistry


class TemplateImmutabilityError(TemplateError):
    pass


class TemplateStore:
    def sync(self, session: Session, registry: TemplateRegistry) -> list[str]:
        """Insert newly published versions; fail if a published version's content changed.

        Returns the list of ``id@version`` that were newly published.
        """
        published: list[str] = []
        existing = {
            (row.template_id, row.version): row.content_sha256
            for row in session.execute(select(TemplateVersion)).scalars()
        }
        for template in registry.published():
            digest = template.content_hash()
            key = (template.template_id, template.version)
            if key in existing:
                if existing[key] != digest:
                    raise TemplateImmutabilityError(
                        f"published template {template.template_id}@{template.version} was modified; "
                        "publish a new version instead"
                    )
                continue
            session.execute(
                insert(TemplateVersion)
                .values(
                    template_id=template.template_id,
                    version=template.version,
                    content=template.model_dump(mode="json", by_alias=True),
                    content_sha256=digest,
                )
                .on_conflict_do_nothing()
            )
            published.append(f"{template.template_id}@{template.version}")
        return published

    def load(self, session: Session, template_id: str, version: str) -> IssueTemplate | None:
        row = session.get(TemplateVersion, (template_id, version))
        return IssueTemplate.model_validate(row.content) if row else None

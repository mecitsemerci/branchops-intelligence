"""Pull-source checkpoints (``source_checkpoints``): the cursor up to which a source has been read."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from branchops.infrastructure.database.models import SourceCheckpoint


class CheckpointRepository:
    def get(self, session: Session, source_id: str) -> str | None:
        return session.execute(
            select(SourceCheckpoint.cursor).where(SourceCheckpoint.source_id == source_id)
        ).scalar_one_or_none()

    def get_datetime(self, session: Session, source_id: str) -> datetime | None:
        cursor = self.get(session, source_id)
        return datetime.fromisoformat(cursor) if cursor else None

    def save(self, session: Session, source_id: str, cursor: str | None) -> None:
        stmt = insert(SourceCheckpoint).values(source_id=source_id, cursor=cursor)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["source_id"], set_={"cursor": stmt.excluded.cursor, "updated_at": func.now()}
            )
        )

    def all(self, session: Session) -> dict[str, tuple[str | None, datetime]]:
        rows = session.execute(
            select(SourceCheckpoint.source_id, SourceCheckpoint.cursor, SourceCheckpoint.updated_at)
        ).all()
        return {r.source_id: (r.cursor, r.updated_at) for r in rows}

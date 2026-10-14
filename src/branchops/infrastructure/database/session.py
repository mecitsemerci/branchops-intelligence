"""Engine and session factory."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from branchops.config.settings import DatabaseSettings


def create_db_engine(settings: DatabaseSettings) -> Engine:
    connect_args: dict[str, object] = {
        "options": f"-c statement_timeout={settings.statement_timeout_ms}",
        "application_name": "branchops-intelligence",
    }
    if settings.sslmode:
        connect_args["sslmode"] = settings.sslmode
    if settings.sslrootcert:
        connect_args["sslrootcert"] = settings.sslrootcert
    return create_engine(
        settings.url,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


class Database:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope: commits on success, rolls back on error."""
        session = self._factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def ping(self) -> bool:
        from sqlalchemy import text

        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True

"""Alembic environment wired to BranchOps settings."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from branchops.config.settings import load_settings
from branchops.infrastructure.database.models import Base

target_metadata = Base.metadata


def _url() -> str:
    override = context.config.attributes.get("database_url")
    return str(override) if override else load_settings().database.url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = context.config.attributes.get("connection")
    if connectable is not None:
        context.configure(connection=connectable, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

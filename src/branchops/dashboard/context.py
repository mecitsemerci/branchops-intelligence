"""Dashboard dependencies (built once per process)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from sqlalchemy.orm import Session

from branchops.application.queries.read_models import ReadModels
from branchops.config.settings import Settings
from branchops.infrastructure.database.session import Database
from branchops.templates.registry import TemplateRegistry

T = TypeVar("T")


@dataclass
class DashboardContext:
    settings: Settings
    db: Database
    registry: TemplateRegistry
    read: ReadModels

    def query(self, fn: Callable[[Session], T]) -> T:
        with self.db.session() as session:
            return fn(session)

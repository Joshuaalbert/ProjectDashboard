"""Repository selection helpers for durable service storage."""

from __future__ import annotations

from pathlib import Path

from projdash.service.ladybug_repository import LadybugProjectRepository
from projdash.service.repository import ProjectRepository
from projdash.service.sqlite_repository import (
    SQLiteProjectRepository,
    is_sqlite_path,
    migrate_default_ladybug_if_needed,
)


def create_project_repository(db_path: str | Path) -> ProjectRepository:
    """Create a project repository for the storage path suffix."""
    path = Path(db_path).expanduser().resolve()
    if is_sqlite_path(path):
        return SQLiteProjectRepository(path)
    return LadybugProjectRepository(path)


def bootstrap_project_repository(db_path: str | Path) -> ProjectRepository:
    """Initialize storage, migrating default Ladybug data when appropriate."""
    path = Path(db_path).expanduser().resolve()
    if is_sqlite_path(path):
        migrate_default_ladybug_if_needed(path)
    repository = create_project_repository(path)
    initialize_schema = getattr(repository, "initialize_schema", None)
    if callable(initialize_schema):
        initialize_schema()
    return repository

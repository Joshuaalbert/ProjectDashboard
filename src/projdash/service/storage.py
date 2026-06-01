"""Repository selection helpers for durable service storage."""

from __future__ import annotations

from pathlib import Path

from projdash.service.repository import ProjectRepository
from projdash.service.sqlite_repository import SQLiteProjectRepository


def create_project_repository(db_path: str | Path) -> ProjectRepository:
    """Create a SQLite project repository for the storage path."""
    path = Path(db_path).expanduser().resolve()
    return SQLiteProjectRepository(path)


def bootstrap_project_repository(db_path: str | Path) -> ProjectRepository:
    """Initialize SQLite storage and return a repository."""
    path = Path(db_path).expanduser().resolve()
    repository = SQLiteProjectRepository(path)
    initialize_schema = getattr(repository, "initialize_schema", None)
    if callable(initialize_schema):
        initialize_schema()
    return repository

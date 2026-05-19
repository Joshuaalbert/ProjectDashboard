"""Initialize the durable service database."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from projdash.service.ladybug_repository import LadybugProjectRepository
from projdash.service.sqlite_repository import (
    SQLiteProjectRepository,
    is_sqlite_path,
    migrate_ladybug_to_sqlite,
)


def main() -> None:
    """CLI entrypoint for database bootstrap."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Path to the service database.")
    parser.add_argument(
        "--storage",
        choices=["sqlite", "ladybug", "auto"],
        default=os.environ.get("PROJDASH_STORAGE", "auto"),
        help="Storage backend to initialize. Defaults to suffix-based auto detection.",
    )
    parser.add_argument(
        "--migrate-from-ladybug",
        help=(
            "Optional LadybugDB source. When the SQLite target is empty, bootstrap "
            "copies this source into SQLite before startup. The source is not deleted."
        ),
    )
    parser.add_argument(
        "--force-migration",
        action="store_true",
        help="Replace existing SQLite repository rows during Ladybug migration.",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    storage = _resolve_storage(args.storage, db_path)

    if storage == "sqlite" and args.migrate_from_ladybug:
        source = Path(args.migrate_from_ladybug)
        if source.exists():
            result = migrate_ladybug_to_sqlite(
                source,
                db_path,
                force=args.force_migration,
            )
            if result.migrated:
                print(
                    "Migrated ProjectDashboard service database "
                    f"from {result.source_path} to {result.target_path} "
                    f"({result.project_count} projects, {result.entity_count} rows). "
                    f"Source preserved at {result.source_path}"
                )
            else:
                print(
                    "Skipped Ladybug migration: "
                    f"{result.skipped_reason}; source preserved at {result.source_path}"
                )
        else:
            print(f"Skipped Ladybug migration; source does not exist: {source}")

    repository = SQLiteProjectRepository(db_path) if storage == "sqlite" else (
        LadybugProjectRepository(db_path)
    )
    repository.initialize_schema()
    close = getattr(repository, "close", None)
    if callable(close):
        close()
    print(f"Initialized ProjectDashboard service database at {args.db}")


def _resolve_storage(storage: str, db_path: Path) -> str:
    if storage == "auto":
        return "sqlite" if is_sqlite_path(db_path) else "ladybug"
    if storage == "sqlite" and db_path.suffix.casefold() == ".lbug":
        raise ValueError("Refusing to initialize SQLite storage at a .lbug path.")
    if storage == "ladybug" and is_sqlite_path(db_path):
        raise ValueError("Refusing to initialize Ladybug storage at a SQLite path.")
    return storage


if __name__ == "__main__":
    main()

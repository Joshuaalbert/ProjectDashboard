"""Initialize the durable service database."""

from __future__ import annotations

import argparse
from pathlib import Path

from projdash.service.sqlite_repository import SQLiteProjectRepository


def main() -> None:
    """CLI entrypoint for database bootstrap."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Path to the service database.")
    parser.add_argument(
        "--storage",
        choices=["sqlite", "auto"],
        default="sqlite",
        help="Storage backend to initialize. SQLite is the only supported backend.",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    _resolve_storage(args.storage, db_path)
    repository = SQLiteProjectRepository(db_path)
    repository.initialize_schema()
    close = getattr(repository, "close", None)
    if callable(close):
        close()
    print(f"Initialized ProjectDashboard service database at {args.db}")


def _resolve_storage(storage: str, db_path: Path) -> str:
    if storage == "auto":
        storage = "sqlite"
    if storage != "sqlite":
        raise ValueError("ProjectDashboard only supports SQLite storage.")
    return storage


if __name__ == "__main__":
    main()

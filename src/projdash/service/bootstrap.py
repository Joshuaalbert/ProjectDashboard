"""Initialize the durable service database."""

from __future__ import annotations

import argparse

from projdash.service.ladybug_repository import LadybugProjectRepository


def main() -> None:
    """CLI entrypoint for database bootstrap."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Path to the LadybugDB database.")
    args = parser.parse_args()

    repository = LadybugProjectRepository(args.db)
    repository.initialize_schema()
    print(f"Initialized ProjectDashboard service database at {args.db}")


if __name__ == "__main__":
    main()

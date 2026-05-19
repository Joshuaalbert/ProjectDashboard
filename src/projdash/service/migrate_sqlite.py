"""CLI for copying a LadybugDB projection into SQLite."""

from __future__ import annotations

import argparse

from projdash.service.sqlite_repository import migrate_ladybug_to_sqlite


def main() -> None:
    """Run the Ladybug-to-SQLite migration."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-ladybug", required=True, help="Source .lbug path.")
    parser.add_argument("--to-sqlite", required=True, help="Target .sqlite path.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace the target SQLite file if it already exists.",
    )
    args = parser.parse_args()

    summary = migrate_ladybug_to_sqlite(
        args.from_ladybug,
        args.to_sqlite,
        overwrite=args.overwrite,
    )
    if not summary.migrated:
        print(
            "Skipped ProjectDashboard service database migration "
            f"from {summary['source']} to {summary['target']}: "
            f"{summary.skipped_reason}"
        )
        return
    print(
        "Migrated ProjectDashboard service database "
        f"from {summary['source']} to {summary['target']} "
        f"({summary['projects']} projects, {summary['processes']} processes)"
    )


if __name__ == "__main__":
    main()

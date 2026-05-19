"""SQLite-backed repository adapter and Ladybug migration helpers."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from projdash.service.errors import ServiceValidationError
from projdash.service.models import (
    BlockerRecord,
    CalendarWeeklyWindowCommand,
    MilestoneRecord,
    ProcessRecord,
    ProcessRevisionRecord,
    ProjectRecord,
    ResourceCalendarOverrideCommand,
    ResourceHolidayCommand,
    RoleRequirementCommand,
    ScheduleSnapshotRecord,
    SlackCollectionCursorRecord,
    SlackEncryptedTokenRecord,
    SlackOutboxRecord,
    SlackProjectConfigRecord,
    SlackResourceMappingRecord,
    SlackRunRecord,
)
from projdash.service.repository import (
    InMemoryProjectRepository,
    RecordDict,
    RetiredProcessRecord,
)
from projdash.service.results import CommandErrorResult, CommandResult

SQLITE_SCHEMA_VERSION = 1
SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}
GENERATION_METADATA_KEY = "generation"
PERSISTING_METHOD_PREFIXES = (
    "add_",
    "clear_",
    "collapse_",
    "create_",
    "deactivate_",
    "delete_",
    "finish_",
    "mark_",
    "record_",
    "remove_",
    "rename_",
    "reopen_",
    "replace_",
    "resolve_",
    "set_",
    "start_",
    "update_",
    "upsert_",
)

ENTITY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS repository_entity(
    kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    project_id TEXT,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(kind, entity_id)
)
"""

COMMAND_REPLAY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS command_replay(
    command_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY(command_id, payload_hash)
)
"""

METADATA_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS repository_metadata(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class SQLiteMigrationResult:
    """Result from migrating a LadybugDB snapshot into SQLite."""

    migrated: bool
    source_path: str
    target_path: str
    project_count: int
    entity_count: int
    process_count: int = 0
    command_replay_count: int = 0
    backup_path: str | None = None
    skipped_reason: str | None = None

    def __getitem__(self, key: str) -> Any:
        """Return compatibility keys for migration print helpers."""
        values = {
            "migrated": self.migrated,
            "source": self.source_path,
            "target": self.target_path,
            "projects": self.project_count,
            "processes": self.process_count,
            "entities": self.entity_count,
            "command_replay_records": self.command_replay_count,
            "backup": self.backup_path,
            "skipped_reason": self.skipped_reason,
        }
        return values[key]


class SQLiteProjectRepository:
    """Durable SQLite adapter using the existing in-memory repository semantics.

    The service already applies mutating commands against a staged
    ``InMemoryProjectRepository`` and commits with ``replace_with``. This adapter
    keeps that contract and stores the projection as typed SQLite rows, updating
    only changed rows on commit.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(Path(db_path).expanduser().resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self.initialize_schema()
        self._loaded_generation = self._database_generation()
        self._projection = self._load_projection()

    def __getattr__(self, name: str) -> Any:
        projection = self.__dict__.get("_projection")
        if projection is not None and hasattr(projection, name):
            if callable(getattr(projection, name)):
                return self._projection_method(
                    name,
                    persist=name.startswith(PERSISTING_METHOD_PREFIXES),
                )
            with self._lock:
                self._reload_projection_if_stale()
                return getattr(self._projection, name)
        raise AttributeError(name)

    def initialize_schema(self) -> None:
        """Create SQLite schema and performance pragmas if needed."""
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute(METADATA_TABLE_SQL)
            self._conn.execute(ENTITY_TABLE_SQL)
            self._conn.execute(COMMAND_REPLAY_TABLE_SQL)
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS repository_entity_project_idx
                ON repository_entity(project_id, kind)
                """
            )
            self._conn.execute(
                """
                INSERT INTO repository_metadata(key, value)
                VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(SQLITE_SCHEMA_VERSION),),
            )
            self._conn.execute(
                """
                INSERT INTO repository_metadata(key, value)
                VALUES(?, '0')
                ON CONFLICT(key) DO NOTHING
                """,
                (GENERATION_METADATA_KEY,),
            )
            self._conn.commit()
            if "_projection" in self.__dict__:
                self._loaded_generation = self._database_generation()
                self._projection = self._load_projection()

    def clone(self) -> InMemoryProjectRepository:
        """Return a transactional in-memory snapshot of durable state."""
        with self._lock:
            self._reload_projection_if_stale()
            clone = self._projection.clone()
            clone._sqlite_base_generation = self._loaded_generation
            return clone

    def replace_with(self, other: Any) -> None:
        """Persist a staged in-memory repository and replace the live projection."""
        if not isinstance(other, InMemoryProjectRepository):
            raise TypeError("SQLiteProjectRepository stages in-memory repositories")
        with self._lock:
            expected_generation = getattr(other, "_sqlite_base_generation", None)
            self._reload_projection_if_stale()
            previous = _repository_entity_rows(self._projection)
            incoming = _repository_entity_rows(other)
            self._loaded_generation = self._persist_entity_delta(
                previous,
                incoming,
                expected_generation=expected_generation,
            )
            self._projection = other.clone()

    def cache_version(self, project_id: str | None = None) -> int:
        """Return the current database generation for service projection caches."""
        return self._database_generation()

    def load_command_replay_cache(
        self,
    ) -> dict[uuid.UUID, dict[str, CommandResult | CommandErrorResult]]:
        """Load durable command replay records for service idempotency."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT command_id, payload_hash, result_json
                FROM command_replay
                ORDER BY command_id, payload_hash
                """
            ).fetchall()
        cache: dict[uuid.UUID, dict[str, CommandResult | CommandErrorResult]] = {}
        for row in rows:
            payload = json.loads(row["result_json"])
            command_id = uuid.UUID(str(row["command_id"]))
            cache.setdefault(command_id, {})[row["payload_hash"]] = (
                CommandResult.model_validate(payload)
                if payload.get("ok")
                else CommandErrorResult.model_validate(payload)
            )
        return cache

    def replace_command_replay_cache(
        self,
        cache: dict[Any, dict[str, CommandResult | CommandErrorResult]],
    ) -> None:
        """Persist the service command replay cache."""
        now = dt.datetime.now(dt.UTC).isoformat()
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM command_replay")
            self._conn.executemany(
                """
                INSERT INTO command_replay(
                    command_id,
                    payload_hash,
                    result_json,
                    applied_at
                )
                VALUES(?, ?, ?, ?)
                """,
                [
                    (
                        str(command_id),
                        payload_hash,
                        _json_dumps(result.model_dump(mode="json")),
                        now,
                    )
                    for command_id, records in cache.items()
                    for payload_hash, result in records.items()
                ],
            )

    def table_names(self) -> set[str]:
        """Return SQLite table names visible to this repository."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            ).fetchall()
        return {str(row["name"]) for row in rows}

    def column_names(self, table_name: str) -> set[str]:
        """Return column names for a SQLite table."""
        with self._lock:
            rows = self._conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(row["name"]) for row in rows}

    def entity_count(self) -> int:
        """Return persisted repository entity row count."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS row_count FROM repository_entity"
            ).fetchone()
        return int(row["row_count"])

    def close(self) -> None:
        """Close the SQLite connection."""
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()

    def _projection_method(self, name: str, *, persist: bool):
        def wrapper(*args, **kwargs):
            with self._lock:
                self._reload_projection_if_stale()
                if not persist:
                    return getattr(self._projection, name)(*args, **kwargs)

                previous = _repository_entity_rows(self._projection)
                staged = self._projection.clone()
                result = getattr(staged, name)(*args, **kwargs)
                incoming = _repository_entity_rows(staged)
                self._loaded_generation = self._persist_entity_delta(
                    previous,
                    incoming,
                    expected_generation=self._loaded_generation,
                )
                self._projection = staged
                return result

        return wrapper

    def _reload_projection_if_stale(self) -> None:
        current_generation = self._database_generation()
        if current_generation == getattr(self, "_loaded_generation", None):
            return
        self._projection = self._load_projection()
        self._loaded_generation = current_generation

    def _database_generation(self) -> int:
        row = self._conn.execute(
            """
            SELECT value
            FROM repository_metadata
            WHERE key = ?
            """,
            (GENERATION_METADATA_KEY,),
        ).fetchone()
        return int(row["value"]) if row is not None else 0

    def _persist_entity_delta(
        self,
        previous: dict[tuple[str, str], tuple[str | None, str]],
        incoming: dict[tuple[str, str], tuple[str | None, str]],
        *,
        expected_generation: int | None = None,
    ) -> int:
        now = dt.datetime.now(dt.UTC).isoformat()
        removed = sorted(set(previous) - set(incoming))
        changed = [
            (kind, entity_id, project_id, payload_json)
            for (kind, entity_id), (project_id, payload_json) in sorted(incoming.items())
            if previous.get((kind, entity_id)) != (project_id, payload_json)
        ]
        if not removed and not changed:
            return self._database_generation()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            current_generation = self._database_generation()
            if (
                expected_generation is not None
                and expected_generation != current_generation
            ):
                raise ServiceValidationError(
                    code="sqlite_concurrent_update",
                    message=(
                        "SQLite repository changed after this transaction was staged."
                    ),
                    details={
                        "expected_generation": expected_generation,
                        "actual_generation": current_generation,
                    },
                )
            next_generation = current_generation + 1
            self._conn.executemany(
                """
                DELETE FROM repository_entity
                WHERE kind = ? AND entity_id = ?
                """,
                removed,
            )
            self._conn.executemany(
                """
                INSERT INTO repository_entity(
                    kind,
                    entity_id,
                    project_id,
                    payload_json,
                    updated_at
                )
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(kind, entity_id) DO UPDATE SET
                    project_id = excluded.project_id,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                [
                    (kind, entity_id, project_id, payload_json, now)
                    for kind, entity_id, project_id, payload_json in changed
                ],
            )
            self._conn.execute(
                """
                UPDATE repository_metadata
                SET value = ?
                WHERE key = ?
                """,
                (str(next_generation), GENERATION_METADATA_KEY),
            )
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
            return next_generation

    def _load_projection(self) -> InMemoryProjectRepository:
        repository = InMemoryProjectRepository()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT kind, entity_id, payload_json
                FROM repository_entity
                ORDER BY kind, entity_id
                """
            ).fetchall()
        grouped: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for row in rows:
            grouped[row["kind"]].append((row["entity_id"], json.loads(row["payload_json"])))

        for entity_id, envelope in _ordered(grouped["project"]):
            repository.projects[entity_id] = ProjectRecord.model_validate(
                envelope["data"],
            )

        for entity_id, envelope in _ordered(grouped["role"]):
            role = _restore_datetime_values(envelope["data"])
            repository.roles[entity_id] = role
            repository.role_ids_by_project[role["project_id"]].append(entity_id)

        for entity_id, envelope in _ordered(grouped["calendar"]):
            calendar = _restore_calendar(envelope["data"])
            repository.calendars[entity_id] = calendar
            repository.calendar_ids_by_project[calendar["project_id"]].append(entity_id)

        for entity_id, envelope in _ordered(grouped["resource"]):
            resource = RecordDict(_restore_resource(envelope["data"]))
            repository.resources[entity_id] = resource
            repository.resource_ids_by_project[resource["project_id"]].append(entity_id)

        for entity_id, envelope in _ordered(grouped["process"]):
            data = envelope["data"]
            process = (
                RetiredProcessRecord.model_validate(data)
                if data.get("is_active") is False
                else ProcessRecord.model_validate(data)
            )
            repository.processes[entity_id] = process
            repository.process_ids_by_project[process.project_id].append(entity_id)

        for _entity_id, envelope in _ordered(grouped["revision"]):
            revision = ProcessRevisionRecord.model_validate(envelope["data"])
            repository.revisions_by_process[revision.process_id].append(revision)

        for entity_id, envelope in _ordered(grouped["role_requirement"]):
            repository.role_requirements[entity_id] = RoleRequirementCommand.model_validate(
                envelope["data"],
            )

        for entity_id, envelope in _ordered(grouped["retired_process"]):
            repository.retired_processes[entity_id] = _restore_datetime_values(
                envelope["data"],
            )

        for _, envelope in _ordered(grouped["process_alias"]):
            data = envelope["data"]
            project_id = data["project_id"]
            alias = data["alias"]
            repository.process_aliases[project_id][alias] = data["process_id"]
            repository.process_alias_sources[project_id][alias] = data["source"]

        for _, envelope in _ordered(grouped["dependency_edge"]):
            data = envelope["data"]
            key = (
                data["project_id"],
                data["predecessor_process_id"],
                data["successor_process_id"],
            )
            repository.dependency_edge_ids[key] = data["edge_id"]

        for entity_id, envelope in _ordered(grouped["blocker"]):
            blocker = BlockerRecord.model_validate(envelope["data"])
            repository.blockers[entity_id] = blocker
            repository.blocker_ids_by_project[blocker.project_id].append(entity_id)

        for _, envelope in _ordered(grouped["schedule_snapshot"]):
            repository.schedule_snapshots.append(
                ScheduleSnapshotRecord.model_validate(envelope["data"]),
            )

        for entity_id, envelope in _ordered(grouped["milestone"]):
            milestone = MilestoneRecord.model_validate(envelope["data"])
            repository.milestones[entity_id] = milestone
            repository.milestone_ids_by_project[milestone.project_id].append(
                entity_id,
            )

        for entity_id, envelope in _ordered(grouped["slack_project_config"]):
            repository.slack_project_configs[entity_id] = (
                SlackProjectConfigRecord.model_validate(envelope["data"])
            )

        for _, envelope in _ordered(grouped["slack_resource_mapping"]):
            mapping = SlackResourceMappingRecord.model_validate(envelope["data"])
            repository.slack_resource_mappings[
                (mapping.project_id, mapping.resource_id)
            ] = mapping

        for _, envelope in _ordered(grouped["slack_collection_cursor"]):
            cursor = SlackCollectionCursorRecord.model_validate(envelope["data"])
            repository.slack_collection_cursors[
                (cursor.project_id, cursor.conversation_id)
            ] = cursor

        for entity_id, envelope in _ordered(grouped["slack_encrypted_token"]):
            repository.slack_encrypted_tokens[entity_id] = (
                SlackEncryptedTokenRecord.model_validate(envelope["data"])
            )

        for entity_id, envelope in _ordered(grouped["slack_run"]):
            run = SlackRunRecord.model_validate(envelope["data"])
            repository.slack_runs[entity_id] = run
            repository.slack_run_ids_by_project[run.project_id].append(entity_id)

        for entity_id, envelope in _ordered(grouped["slack_outbox"]):
            outbox = SlackOutboxRecord.model_validate(envelope["data"])
            repository.slack_outbox[entity_id] = outbox
            repository.slack_outbox_ids_by_project[outbox.project_id].append(entity_id)

        for _, envelope in _ordered(grouped["slack_outbox_dedupe"]):
            data = envelope["data"]
            repository.slack_outbox_dedupe[
                (data["project_id"], data["slack_user_id"], data["content_hash"])
            ] = data["outbox_id"]

        repository.schedule_snapshots.sort(
            key=lambda item: (
                item.project_id,
                item.committed_at,
                tuple(item.terminal_process_symbols),
                item.snapshot_id,
            ),
        )
        return repository


def is_sqlite_path(path: str | Path) -> bool:
    """Return whether a database path should use the SQLite repository."""
    return Path(path).suffix.casefold() in SQLITE_SUFFIXES


def migrate_ladybug_to_sqlite(
    source_path: str | Path,
    target_path: str | Path,
    *,
    force: bool = False,
    overwrite: bool | None = None,
) -> SQLiteMigrationResult:
    """Copy a LadybugDB projection into SQLite without deleting the source."""
    if overwrite is not None:
        force = overwrite
    source = Path(source_path).expanduser().resolve()
    target = Path(target_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Ladybug source database does not exist: {source}")

    backup_path: Path | None = None
    if target.exists() and not force:
        return SQLiteMigrationResult(
            migrated=False,
            source_path=str(source),
            target_path=str(target),
            project_count=0,
            entity_count=0,
            process_count=0,
            command_replay_count=0,
            skipped_reason="target_not_empty",
        )
    elif target.exists() and force:
        backup_path = _backup_sqlite_target(target)

    from projdash.service.ladybug_repository import LadybugProjectRepository

    source_repository = LadybugProjectRepository(source)
    try:
        projection = source_repository.clone()
        replay_cache = source_repository.load_command_replay_cache()
    finally:
        if not source_repository._conn.is_closed():
            source_repository._conn.close()
        if not source_repository._db.is_closed():
            source_repository._db.close()

    write_target = (
        target
        if target.exists()
        else target.with_name(f".{target.name}.migrating-{uuid.uuid4().hex}.tmp")
    )
    target_repository = SQLiteProjectRepository(write_target)
    try:
        target_repository.replace_with(projection)
        target_repository.replace_command_replay_cache(replay_cache)
        entity_count = target_repository.entity_count()
        if write_target != target:
            target_repository.close()
            target_repository = None
            write_target.replace(target)
            _remove_sqlite_sidecars(write_target)
        return SQLiteMigrationResult(
            migrated=True,
            source_path=str(source),
            target_path=str(target),
            project_count=len(projection.projects),
            entity_count=entity_count,
            process_count=len(projection.processes),
            command_replay_count=sum(len(records) for records in replay_cache.values()),
            backup_path=str(backup_path) if backup_path is not None else None,
        )
    except Exception:
        if write_target != target and write_target.exists():
            write_target.unlink()
        raise
    finally:
        if target_repository is not None:
            target_repository.close()


def _backup_sqlite_target(target: Path) -> Path:
    backup_path = target.with_suffix(
        f"{target.suffix}.bak-"
        f"{dt.datetime.now(dt.UTC).strftime('%Y%m%d%H%M%S')}"
    )
    target.replace(backup_path)
    for sidecar_suffix in ("-wal", "-shm"):
        sidecar = Path(f"{target}{sidecar_suffix}")
        if sidecar.exists():
            sidecar.replace(Path(f"{backup_path}{sidecar_suffix}"))
    return backup_path


def _remove_sqlite_sidecars(path: Path) -> None:
    for sidecar_suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{sidecar_suffix}")
        if sidecar.exists():
            sidecar.unlink()


def migrate_default_ladybug_if_needed(
    sqlite_path: str | Path,
) -> SQLiteMigrationResult | None:
    """Migrate sibling ``.lbug`` data for the default SQLite database."""
    target = Path(sqlite_path).expanduser().resolve()
    if target.exists() or target.name != "projdash.sqlite":
        return None
    source = target.with_suffix(".lbug")
    if not source.exists():
        return None
    return migrate_ladybug_to_sqlite(source, target)


def _repository_entity_rows(
    repository: InMemoryProjectRepository,
) -> dict[tuple[str, str], tuple[str | None, str]]:
    rows: dict[tuple[str, str], tuple[str | None, str]] = {}

    def add(
        kind: str,
        entity_id: str,
        project_id: str | None,
        data: Any,
        *,
        order: int | None = None,
    ) -> None:
        envelope: dict[str, Any] = {"data": _normalize_json(data)}
        if order is not None:
            envelope["order"] = order
        rows[(kind, entity_id)] = (project_id, _json_dumps(envelope))

    for project_id, project in sorted(repository.projects.items()):
        add("project", project_id, project_id, project)

    _add_ordered_records(
        rows,
        kind="role",
        id_by_project=repository.role_ids_by_project,
        records=repository.roles,
        project_field="project_id",
    )
    _add_ordered_records(
        rows,
        kind="calendar",
        id_by_project=repository.calendar_ids_by_project,
        records=repository.calendars,
        project_field="project_id",
    )
    _add_ordered_records(
        rows,
        kind="resource",
        id_by_project=repository.resource_ids_by_project,
        records=repository.resources,
        project_field="project_id",
    )
    _add_ordered_records(
        rows,
        kind="process",
        id_by_project=repository.process_ids_by_project,
        records=repository.processes,
        project_field="project_id",
    )

    for _process_id, revisions in sorted(repository.revisions_by_process.items()):
        for index, revision in enumerate(revisions):
            add("revision", revision.revision_id, revision.project_id, revision, order=index)

    for requirement_id, requirement in sorted(repository.role_requirements.items()):
        add("role_requirement", requirement_id, None, requirement)

    for process_id, retirement in sorted(repository.retired_processes.items()):
        project_id = None
        process = repository.processes.get(process_id)
        if process is not None:
            project_id = process.project_id
        add("retired_process", process_id, project_id, retirement)

    for project_id, aliases in sorted(repository.process_aliases.items()):
        sources = repository.process_alias_sources.get(project_id, {})
        for index, (alias, process_id) in enumerate(sorted(aliases.items())):
            add(
                "process_alias",
                _composite_entity_id(project_id, alias),
                project_id,
                {
                    "project_id": project_id,
                    "alias": alias,
                    "process_id": process_id,
                    "source": sources.get(alias, "agent"),
                },
                order=index,
            )

    for index, ((project_id, predecessor_id, successor_id), edge_id) in enumerate(
        sorted(repository.dependency_edge_ids.items())
    ):
        add(
            "dependency_edge",
            _composite_entity_id(project_id, predecessor_id, successor_id),
            project_id,
            {
                "project_id": project_id,
                "predecessor_process_id": predecessor_id,
                "successor_process_id": successor_id,
                "edge_id": edge_id,
            },
            order=index,
        )

    _add_ordered_records(
        rows,
        kind="blocker",
        id_by_project=repository.blocker_ids_by_project,
        records=repository.blockers,
        project_field="project_id",
    )

    for index, snapshot in enumerate(repository.schedule_snapshots):
        add(
            "schedule_snapshot",
            snapshot.snapshot_id,
            snapshot.project_id,
            snapshot,
            order=index,
        )

    _add_ordered_records(
        rows,
        kind="milestone",
        id_by_project=repository.milestone_ids_by_project,
        records=repository.milestones,
        project_field="project_id",
    )

    for project_id, config in sorted(repository.slack_project_configs.items()):
        add("slack_project_config", project_id, project_id, config)

    for index, mapping in enumerate(
        sorted(
            repository.slack_resource_mappings.values(),
            key=lambda item: (item.project_id, item.resource_id),
        )
    ):
        add(
            "slack_resource_mapping",
            _composite_entity_id(mapping.project_id, mapping.resource_id),
            mapping.project_id,
            mapping,
            order=index,
        )

    for index, cursor in enumerate(
        sorted(
            repository.slack_collection_cursors.values(),
            key=lambda item: (item.project_id, item.conversation_id),
        )
    ):
        add(
            "slack_collection_cursor",
            _composite_entity_id(cursor.project_id, cursor.conversation_id),
            cursor.project_id,
            cursor,
            order=index,
        )

    for project_id, token in sorted(repository.slack_encrypted_tokens.items()):
        add("slack_encrypted_token", project_id, project_id, token)

    _add_ordered_records(
        rows,
        kind="slack_run",
        id_by_project=repository.slack_run_ids_by_project,
        records=repository.slack_runs,
        project_field="project_id",
    )
    _add_ordered_records(
        rows,
        kind="slack_outbox",
        id_by_project=repository.slack_outbox_ids_by_project,
        records=repository.slack_outbox,
        project_field="project_id",
    )

    for index, ((project_id, slack_user_id, content_hash), outbox_id) in enumerate(
        sorted(repository.slack_outbox_dedupe.items())
    ):
        add(
            "slack_outbox_dedupe",
            _composite_entity_id(project_id, slack_user_id, content_hash),
            project_id,
            {
                "project_id": project_id,
                "slack_user_id": slack_user_id,
                "content_hash": content_hash,
                "outbox_id": outbox_id,
            },
            order=index,
        )

    return rows


def _add_ordered_records(
    rows: dict[tuple[str, str], tuple[str | None, str]],
    *,
    kind: str,
    id_by_project: dict[str, list[str]],
    records: dict[str, Any],
    project_field: str,
) -> None:
    seen: set[str] = set()

    def add(
        entity_id: str,
        project_id: str | None,
        data: Any,
        order: int | None,
    ) -> None:
        envelope: dict[str, Any] = {"data": _normalize_json(data)}
        if order is not None:
            envelope["order"] = order
        rows[(kind, entity_id)] = (project_id, _json_dumps(envelope))

    for project_id, entity_ids in sorted(id_by_project.items()):
        for index, entity_id in enumerate(entity_ids):
            if entity_id not in records:
                continue
            seen.add(entity_id)
            add(entity_id, project_id, records[entity_id], index)

    for entity_id, record in sorted(records.items()):
        if entity_id in seen:
            continue
        project_id = _record_project_id(record, project_field)
        add(entity_id, project_id, record, None)


def _record_project_id(record: Any, project_field: str) -> str | None:
    if isinstance(record, dict):
        value = record.get(project_field)
    else:
        value = getattr(record, project_field, None)
    return str(value) if value is not None else None


def _ordered(rows: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    return sorted(
        rows,
        key=lambda item: (
            item[1].get("order", 0),
            item[0],
        ),
    )


def _restore_calendar(data: dict[str, Any]) -> dict[str, Any]:
    restored = _restore_datetime_values(data)
    restored["weekly_windows"] = [
        CalendarWeeklyWindowCommand.model_validate(window).model_dump()
        for window in restored.get("weekly_windows", [])
    ]
    return restored


def _restore_resource(data: dict[str, Any]) -> dict[str, Any]:
    restored = _restore_datetime_values(data)
    restored["holidays"] = [
        ResourceHolidayCommand.model_validate(holiday).model_dump()
        for holiday in restored.get("holidays", [])
    ]
    restored["calendar_overrides"] = [
        ResourceCalendarOverrideCommand.model_validate(override).model_dump()
        for override in restored.get("calendar_overrides", [])
    ]
    return restored


def _restore_datetime_values(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            item_key: _restore_datetime_values(item_value, key=item_key)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_restore_datetime_values(item) for item in value]
    if isinstance(value, str) and _is_datetime_key(key):
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def _is_datetime_key(key: str | None) -> bool:
    return key is not None and (
        key.endswith("_at")
        or key in {"starts_at", "ends_at"}
    )


def _normalize_json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _normalize_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_normalize_json(item) for item in value]
    if isinstance(value, list):
        return [_normalize_json(item) for item in value]
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    )


def _composite_entity_id(*parts: str) -> str:
    return _json_dumps(list(parts))

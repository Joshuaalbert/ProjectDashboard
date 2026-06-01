"""SQLite-backed repository adapter."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import uuid
from collections import defaultdict
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
    PMCommunicationEvidenceRecord,
    ProcessEvidenceLineItemRecord,
    ProcessRecord,
    ProcessRevisionRecord,
    ProcessRolePinRecord,
    ProjectRecord,
    ResourceCalendarOverrideCommand,
    ResourceEvidenceLineItemRecord,
    ResourceHolidayCommand,
    RoleRequirementCommand,
    ScheduleSnapshotRecord,
    SlackCollectionCursorRecord,
    SlackEncryptedTokenRecord,
    SlackOutboxRecord,
    SlackProjectConfigRecord,
    SlackResourceMappingRecord,
    SlackRunRecord,
    TeammateWorkPlanRecord,
)
from projdash.service.repository import (
    LEGACY_PROCESS_EVIDENCE_LINE_ITEMS,
    InMemoryProjectRepository,
    RecordDict,
    RetiredProcessRecord,
)
from projdash.service.results import CommandErrorResult, CommandResult

SQLITE_SCHEMA_VERSION = 1
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

REVISION_ROLE_REQUIREMENTS_INSERT_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS repository_revision_role_requirements_one_insert
BEFORE INSERT ON repository_entity
WHEN NEW.kind = 'revision'
 AND (
    COALESCE(json_type(NEW.payload_json, '$.data.role_requirements'), '') != 'array'
    OR json_array_length(json_extract(NEW.payload_json, '$.data.role_requirements')) != 1
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'revision role_requirements must contain exactly one item'
    );
END
"""

REVISION_ROLE_REQUIREMENTS_UPDATE_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS repository_revision_role_requirements_one_update
BEFORE UPDATE OF payload_json, kind ON repository_entity
WHEN NEW.kind = 'revision'
 AND (
    COALESCE(json_type(NEW.payload_json, '$.data.role_requirements'), '') != 'array'
    OR json_array_length(json_extract(NEW.payload_json, '$.data.role_requirements')) != 1
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'revision role_requirements must contain exactly one item'
    );
END
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
        self._repair_projection_invariants()

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
            self._conn.execute(REVISION_ROLE_REQUIREMENTS_INSERT_TRIGGER_SQL)
            self._conn.execute(REVISION_ROLE_REQUIREMENTS_UPDATE_TRIGGER_SQL)
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
                self._repair_projection_invariants()

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
            other._validate_process_role_definition_invariants()
            other._validate_no_orphaned_blocker_processes()
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
                staged._validate_process_role_definition_invariants()
                staged._validate_no_orphaned_blocker_processes()
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
        self._repair_projection_invariants()

    def _repair_projection_invariants(self) -> None:
        previous = self._database_entity_rows()
        repair_at = dt.datetime.now(dt.UTC)
        deleted = self._projection.delete_orphaned_blocker_processes(
            edit_at=repair_at,
        )
        updated = self._projection.ensure_default_process_roles_for_missing_requirements(
            edit_at=repair_at,
        )
        normalized = self._projection.normalize_process_role_requirements_to_single(
            edit_at=repair_at,
        )
        pin_cleanup = self._projection.delete_invalid_process_role_pins(
            edit_at=repair_at,
        )
        evidence_cleanup = self._projection.delete_process_evidence_line_items(
            line_items=LEGACY_PROCESS_EVIDENCE_LINE_ITEMS,
        )
        self._projection._validate_process_role_definition_invariants()
        self._projection._validate_no_orphaned_blocker_processes()
        incoming = _repository_entity_rows(self._projection)
        if (
            not deleted.get("deleted_process_ids")
            and not deleted.get("deleted_blocker_ids")
            and not updated.get("updated_process_ids")
            and not normalized.get("updated_process_ids")
            and not pin_cleanup.get("deleted_pin_ids")
            and not evidence_cleanup.get("deleted_evidence_line_ids")
            and previous == incoming
        ):
            return
        self._loaded_generation = self._persist_entity_delta(
            previous,
            incoming,
            expected_generation=self._loaded_generation,
        )

    def _database_entity_rows(self) -> dict[tuple[str, str], tuple[str | None, str]]:
        rows = self._conn.execute(
            """
            SELECT kind, entity_id, project_id, payload_json
            FROM repository_entity
            """
        ).fetchall()
        return {
            (str(row["kind"]), str(row["entity_id"])): (
                row["project_id"],
                row["payload_json"],
            )
            for row in rows
        }

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
            data = _restore_process(envelope["data"])
            process = (
                RetiredProcessRecord.model_validate(data)
                if data.get("is_active") is False
                else ProcessRecord.model_validate(data)
            )
            repository.processes[entity_id] = process
            repository.process_ids_by_project[process.project_id].append(entity_id)

        for _entity_id, envelope in _ordered(grouped["revision"]):
            revision = ProcessRevisionRecord.model_validate(
                _restore_revision_for_repository(repository, envelope["data"])
            )
            repository.revisions_by_process[revision.process_id].append(revision)

        for entity_id, envelope in _ordered(grouped["process_role_pin"]):
            pin = ProcessRolePinRecord.model_validate(envelope["data"])
            repository.process_role_pins[entity_id] = pin
            if entity_id not in repository.process_role_pin_ids_by_project[pin.project_id]:
                repository.process_role_pin_ids_by_project[pin.project_id].append(
                    entity_id,
                )

        for entity_id, envelope in _ordered(grouped["process_role_stake_window"]):
            if entity_id in repository.process_role_pins:
                continue
            data = _restore_datetime_values(envelope["data"])
            pinned_at = data["starts_at"]
            finished_at = data.get("ends_at")
            status = "pinned_finished" if finished_at is not None else "pinned_started"
            forecast_finish_at = finished_at or _legacy_stake_pin_forecast_finish_at(
                repository,
                data,
                pinned_at,
            )
            pin = ProcessRolePinRecord(
                pin_id=entity_id,
                project_id=data["project_id"],
                process_id=data["process_id"],
                requirement_id=data.get("requirement_id"),
                role_id=data["role_id"],
                resource_id=data["resource_id"],
                pinned_at=pinned_at,
                forecast_finish_at=forecast_finish_at,
                status=status,
                verified_done_at=finished_at,
                created_at=data["created_at"],
                updated_at=data["updated_at"],
                note=data.get("note"),
            )
            repository.process_role_pins[pin.pin_id] = pin
            if pin.pin_id not in repository.process_role_pin_ids_by_project[pin.project_id]:
                repository.process_role_pin_ids_by_project[pin.project_id].append(
                    pin.pin_id,
                )

        for entity_id, envelope in _ordered(grouped["role_requirement"]):
            repository.role_requirements[entity_id] = (
                RoleRequirementCommand.model_validate(
                    _restore_role_requirement(envelope["data"]),
                )
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
            if blocker.process_id not in repository.processes:
                raise ServiceValidationError(
                    code="blocker_process_reference_missing",
                    message="Persisted blocker references a missing process.",
                    entity_id=blocker.blocker_id,
                    details={"process_id": blocker.process_id},
                )
            repository.blockers[entity_id] = blocker
            if entity_id not in repository.blocker_ids_by_project[blocker.project_id]:
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

        for entity_id, envelope in _ordered(grouped["pm_communication_evidence"]):
            evidence = PMCommunicationEvidenceRecord.model_validate(envelope["data"])
            repository.pm_communication_evidence[entity_id] = evidence
            repository.pm_communication_evidence_ids_by_project[
                evidence.project_id
            ].append(entity_id)

        for entity_id, envelope in _ordered(grouped["process_evidence_line_item"]):
            evidence_line = ProcessEvidenceLineItemRecord.model_validate(
                envelope["data"],
            )
            repository.process_evidence_line_items[entity_id] = evidence_line
            repository.process_evidence_line_item_ids_by_project[
                evidence_line.project_id
            ].append(entity_id)

        for entity_id, envelope in _ordered(grouped["resource_evidence_line_item"]):
            evidence_line = ResourceEvidenceLineItemRecord.model_validate(
                envelope["data"],
            )
            repository.resource_evidence_line_items[entity_id] = evidence_line
            repository.resource_evidence_line_item_ids_by_project[
                evidence_line.project_id
            ].append(entity_id)

        for _entity_id, envelope in _ordered(grouped["teammate_work_plan"]):
            work_plan = TeammateWorkPlanRecord.model_validate(envelope["data"])
            if work_plan.status != "accepted":
                continue
            for forecast in work_plan.forecasts:
                if forecast.forecast_id in repository.process_role_pins:
                    continue
                pin = ProcessRolePinRecord(
                    pin_id=forecast.forecast_id,
                    project_id=work_plan.project_id,
                    process_id=forecast.process_id,
                    requirement_id=forecast.requirement_id,
                    role_id=forecast.role_id,
                    resource_id=work_plan.resource_id,
                    pinned_at=work_plan.starts_at,
                    forecast_finish_at=forecast.forecast_finish_at,
                    status="pinned_started",
                    verified_done_at=None,
                    created_at=work_plan.created_at,
                    updated_at=work_plan.updated_at,
                    note=forecast.note or work_plan.note,
                )
                repository.process_role_pins[pin.pin_id] = pin
                if (
                    pin.pin_id
                    not in repository.process_role_pin_ids_by_project[pin.project_id]
                ):
                    repository.process_role_pin_ids_by_project[pin.project_id].append(
                        pin.pin_id,
                    )

        for _, envelope in _ordered(grouped["slack_outbox_dedupe"]):
            data = envelope["data"]
            target_type = data.get("target_type") or "dm"
            target_id = (
                data.get("target_id")
                or (
                    data.get("slack_channel_id")
                    if target_type == "channel"
                    else data.get("slack_user_id")
                )
            )
            repository.slack_outbox_dedupe[
                (
                    data["project_id"],
                    target_type,
                    target_id,
                    data["content_hash"],
                )
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

    _add_ordered_records(
        rows,
        kind="process_role_pin",
        id_by_project=repository.process_role_pin_ids_by_project,
        records=repository.process_role_pins,
        project_field="project_id",
    )

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

    _add_ordered_records(
        rows,
        kind="pm_communication_evidence",
        id_by_project=repository.pm_communication_evidence_ids_by_project,
        records=repository.pm_communication_evidence,
        project_field="project_id",
    )
    _add_ordered_records(
        rows,
        kind="process_evidence_line_item",
        id_by_project=repository.process_evidence_line_item_ids_by_project,
        records=repository.process_evidence_line_items,
        project_field="project_id",
    )
    _add_ordered_records(
        rows,
        kind="resource_evidence_line_item",
        id_by_project=repository.resource_evidence_line_item_ids_by_project,
        records=repository.resource_evidence_line_items,
        project_field="project_id",
    )

    for index, (dedupe_key, outbox_id) in enumerate(
        sorted(repository.slack_outbox_dedupe.items())
    ):
        if len(dedupe_key) == 3:
            project_id, slack_user_id, content_hash = dedupe_key
            target_type = "dm"
            target_id = slack_user_id
            slack_channel_id = None
        else:
            project_id, target_type, target_id, content_hash = dedupe_key
            slack_user_id = target_id if target_type == "dm" else None
            slack_channel_id = target_id if target_type == "channel" else None
        add(
            "slack_outbox_dedupe",
            _composite_entity_id(project_id, target_type, target_id, content_hash),
            project_id,
            {
                "project_id": project_id,
                "target_type": target_type,
                "target_id": target_id,
                "slack_user_id": slack_user_id,
                "slack_channel_id": slack_channel_id,
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


def _restore_process(data: dict[str, Any]) -> dict[str, Any]:
    restored = _restore_datetime_values(data)
    for legacy_field in ("status", "started_at", "finished_at"):
        restored.pop(legacy_field, None)
    return restored


def _restore_revision(data: dict[str, Any]) -> dict[str, Any]:
    restored = _restore_datetime_values(data)
    restored.pop("staked_resource_ids", None)
    restored["role_requirements"] = [
        _restore_role_requirement(requirement)
        for requirement in restored.get("role_requirements", [])
    ]
    return restored


def _restore_revision_for_repository(
    repository: InMemoryProjectRepository,
    data: dict[str, Any],
) -> dict[str, Any]:
    restored = _restore_revision(data)
    role_requirements = restored.get("role_requirements") or []
    if len(role_requirements) == 1:
        return restored
    project_id = str(restored["project_id"])
    process_id = str(restored["process_id"])
    if not role_requirements:
        role_id = repository._ensure_default_missing_process_role(project_id)
        restored["role_requirements"] = [
            RoleRequirementCommand(
                requirement_id=f"{process_id}-{role_id}",
                role_id=role_id,
                effort_hours=1,
            ).model_dump(mode="json")
        ]
        return restored
    restored["role_requirements"] = [
        repository._single_role_requirement_from_legacy_requirements(
            project_id,
            process_id,
            [
                RoleRequirementCommand.model_validate(requirement)
                for requirement in role_requirements
            ],
            requirement_id_prefix=process_id,
        ).model_dump(mode="json")
    ]
    return restored


def _restore_role_requirement(data: dict[str, Any]) -> dict[str, Any]:
    restored = _restore_datetime_values(data)
    restored.pop("staked_resource_ids", None)
    return restored


def _legacy_stake_pin_forecast_finish_at(
    repository: InMemoryProjectRepository,
    data: dict[str, Any],
    pinned_at: dt.datetime,
) -> dt.datetime:
    from projdash.service.queries import QueryEnvelope
    from projdash.service.service import ProjectService

    result = ProjectService(repository).handle_query(
        QueryEnvelope.model_validate(
            {
                "query": {
                    "action": "query_resource_schedule",
                    "project_id": data["project_id"],
                    "as_of": pinned_at,
                    "now": pinned_at,
                    "include_allocation_slices": False,
                    "resource_schedule_backend": "mcts",
                }
            }
        )
    )
    if not result.ok:
        raise ServiceValidationError(
            code="legacy_stake_pin_forecast_unavailable",
            message=(
                "Legacy process-role stake windows cannot be migrated to pins "
                "unless a scheduler forecast finish is available."
            ),
            entity_id=data.get("process_id"),
            details={
                "stake_id": data.get("stake_id"),
                "process_id": data.get("process_id"),
                "scheduler_error": (
                    result.error.model_dump(mode="json")
                    if result.error is not None
                    else None
                ),
            },
        )
    for process in result.data.get("processes", []) if result.data else []:
        if process.get("process_id") != data["process_id"]:
            continue
        forecast_finish_at = _restore_datetime_values(process.get("ends_at"))
        if forecast_finish_at is not None:
            return forecast_finish_at
    raise ServiceValidationError(
        code="legacy_stake_pin_forecast_missing_process",
        message=(
            "Legacy process-role stake windows cannot be migrated to pins "
            "because the scheduler did not return a finish for the process."
        ),
        entity_id=data.get("process_id"),
        details={
            "stake_id": data.get("stake_id"),
            "process_id": data.get("process_id"),
        },
    )


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

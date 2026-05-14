"""LadybugDB adapter boundary.

This first slice initializes the durable graph schema. Command/query persistence
will be implemented behind the same repository protocol after the service
contracts settle under tests.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import uuid
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from projdash.service.errors import ServiceValidationError
from projdash.service.identifiers import new_id
from projdash.service.models import (
    BlockerRecord,
    ProcessRecord,
    ProcessRevisionRecord,
    ProjectRecord,
    RoleRequirementCommand,
    ScheduleSnapshotRecord,
)
from projdash.service.repository import (
    InMemoryProjectRepository,
    RecordDict,
    RetiredProcessRecord,
)
from projdash.service.results import CommandErrorResult, CommandResult

SCHEMA_STATEMENTS = (
    """
    CREATE NODE TABLE IF NOT EXISTS Project(
        project_id STRING PRIMARY KEY,
        name STRING,
        start_at STRING,
        default_currency STRING
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Process(
        process_id STRING PRIMARY KEY,
        project_id STRING,
        symbol STRING,
        status STRING,
        started_at STRING,
        finished_at STRING,
        is_active BOOL,
        retired_at STRING,
        retired_by_command_id STRING,
        retirement_reason STRING
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS ProcessRevision(
        revision_id STRING PRIMARY KEY,
        process_id STRING,
        project_id STRING,
        effective_at STRING,
        name STRING,
        description STRING,
        duration_business_days INT64,
        required_roles_json STRING,
        due_at STRING,
        earliest_start_at STRING,
        start_at_earliest BOOL,
        delay_after_dependencies_business_days INT64,
        assumption_note STRING
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS ProcessRetirementEvent(
        retirement_event_id STRING PRIMARY KEY,
        project_id STRING,
        process_id STRING,
        retired_at STRING,
        retired_by_command_id STRING,
        retirement_reason STRING,
        replacement_process_ids STRING[]
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Role(
        role_id STRING PRIMARY KEY,
        project_id STRING,
        name STRING,
        active BOOL
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Resource(
        resource_id STRING PRIMARY KEY,
        project_id STRING,
        name STRING,
        calendar_id STRING,
        available_from_at STRING,
        available_until_at STRING,
        cost_rate STRING,
        cost_unit STRING,
        cost_currency STRING,
        active BOOL
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS ResourceHoliday(
        holiday_id STRING PRIMARY KEY,
        logical_holiday_id STRING,
        resource_id STRING,
        project_id STRING,
        starts_at STRING,
        ends_at STRING,
        reason STRING
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS ResourceCalendar(
        calendar_id STRING PRIMARY KEY,
        project_id STRING,
        name STRING,
        timezone STRING,
        active BOOL
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS CalendarWeeklyWindow(
        window_id STRING PRIMARY KEY,
        logical_window_id STRING,
        calendar_id STRING,
        project_id STRING,
        weekday INT64,
        start_local_time STRING,
        end_local_time STRING,
        capacity_hours DOUBLE
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS CalendarException(
        exception_id STRING PRIMARY KEY,
        logical_exception_id STRING,
        calendar_id STRING,
        project_id STRING,
        starts_at STRING,
        ends_at STRING,
        capacity_hours DOUBLE,
        reason STRING
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS RoleRequirement(
        requirement_id STRING PRIMARY KEY,
        logical_requirement_id STRING,
        revision_id STRING,
        project_id STRING,
        process_id STRING,
        role_id STRING,
        effort_hours DOUBLE,
        min_allocation_hours_per_day DOUBLE,
        max_allocation_hours_per_day DOUBLE,
        required_resource_count INT64,
        allocation_policy STRING
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS ProcessAlias(
        alias_id STRING PRIMARY KEY,
        project_id STRING,
        process_id STRING,
        alias STRING,
        source STRING,
        created_at STRING
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Blocker(
        blocker_id STRING PRIMARY KEY,
        project_id STRING,
        process_id STRING,
        description STRING,
        opened_at STRING,
        resolved_at STRING,
        summary STRING,
        details STRING,
        severity STRING,
        created_at STRING,
        resolution STRING
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS DueDateHistoryEvent(
        event_id STRING PRIMARY KEY,
        project_id STRING,
        process_id STRING,
        mutation_action STRING,
        edit_at STRING,
        before_due_at STRING,
        after_due_at STRING,
        command_id STRING
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS ScheduleSnapshot(
        snapshot_id STRING PRIMARY KEY,
        project_id STRING,
        committed_at STRING,
        terminal_process_symbols STRING[],
        schedule_basis STRING,
        completion_at STRING,
        derived_due_at STRING,
        horizon_starts_at STRING,
        horizon_ends_at STRING,
        converged BOOL,
        unallocated_count INT64,
        note STRING
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS CommandReplay(
        command_id STRING PRIMARY KEY,
        payload_hash STRING,
        result_json STRING,
        applied_at STRING
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS HAS_PROCESS(FROM Project TO Process)
    """,
    """
    CREATE REL TABLE IF NOT EXISTS HAS_REVISION(FROM Process TO ProcessRevision)
    """,
    """
    CREATE REL TABLE IF NOT EXISTS DEPENDS_ON(
        FROM Process TO Process,
        edge_id STRING,
        project_id STRING,
        revision_id STRING,
        dependency_type STRING,
        retired_at STRING,
        retired_by_command_id STRING,
        retirement_reason STRING
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS BLOCKS(FROM Blocker TO Process)
    """,
    """
    CREATE REL TABLE IF NOT EXISTS HAS_ROLE(FROM Project TO Role)
    """,
    """
    CREATE REL TABLE IF NOT EXISTS HAS_RESOURCE(FROM Project TO Resource)
    """,
    """
    CREATE REL TABLE IF NOT EXISTS HAS_CALENDAR(FROM Project TO ResourceCalendar)
    """,
    """
    CREATE REL TABLE IF NOT EXISTS HAS_WINDOW(
        FROM ResourceCalendar TO CalendarWeeklyWindow
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS HAS_EXCEPTION(
        FROM ResourceCalendar TO CalendarException
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS CAN_FILL(FROM Resource TO Role)
    """,
    """
    CREATE REL TABLE IF NOT EXISTS USES_CALENDAR(FROM Resource TO ResourceCalendar)
    """,
    """
    CREATE REL TABLE IF NOT EXISTS HAS_HOLIDAY(FROM Resource TO ResourceHoliday)
    """,
    """
    CREATE REL TABLE IF NOT EXISTS REQUIRES_ROLE(
        FROM ProcessRevision TO RoleRequirement
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS REQUIREMENT_ROLE(FROM RoleRequirement TO Role)
    """,
    """
    CREATE REL TABLE IF NOT EXISTS HAS_ALIAS(FROM Process TO ProcessAlias)
    """,
    """
    CREATE REL TABLE IF NOT EXISTS HAS_BLOCKER(FROM Process TO Blocker)
    """,
    """
    CREATE REL TABLE IF NOT EXISTS HAS_DUE_DATE_EVENT(
        FROM Process TO DueDateHistoryEvent
    )
    """,
)

SNAPSHOT_NODE_TABLES = (
    "ScheduleSnapshot",
    "DueDateHistoryEvent",
    "Blocker",
    "ProcessAlias",
    "RoleRequirement",
    "CalendarException",
    "CalendarWeeklyWindow",
    "ResourceHoliday",
    "Resource",
    "ResourceCalendar",
    "Role",
    "ProcessRetirementEvent",
    "ProcessRevision",
    "Process",
    "Project",
)

SNAPSHOT_REL_TABLES = (
    "HAS_PROCESS",
    "HAS_REVISION",
    "DEPENDS_ON",
    "BLOCKS",
    "HAS_ROLE",
    "HAS_RESOURCE",
    "HAS_CALENDAR",
    "HAS_WINDOW",
    "HAS_EXCEPTION",
    "HAS_HOLIDAY",
    "CAN_FILL",
    "USES_CALENDAR",
    "REQUIRES_ROLE",
    "REQUIREMENT_ROLE",
    "HAS_ALIAS",
    "HAS_BLOCKER",
    "HAS_DUE_DATE_EVENT",
)

MISSING_REQUIREMENT_ID_PREFIX = "__projdash_missing_requirement_id__"
SCOPED_STORAGE_SEPARATOR = "::"


class LadybugProjectRepository:
    """Thin LadybugDB connection and schema bootstrap wrapper."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._lb = _import_ladybug()
        raw_db = self._lb.Database(self.db_path)
        raw_conn = self._lb.Connection(raw_db)
        self._db = _CloseMethodAdapter(raw_db)
        self._conn = _CloseMethodAdapter(raw_conn)
        self.initialize_schema()
        self._projection = self._load_projection()

    def __getattr__(self, name: str) -> Any:
        projection = self.__dict__.get("_projection")
        if projection is not None and hasattr(projection, name):
            return getattr(projection, name)
        raise AttributeError(name)

    def initialize_schema(self) -> None:
        """Create graph tables and relationships if they do not exist."""
        for statement in SCHEMA_STATEMENTS:
            self._conn.execute(statement)
        if "_projection" in self.__dict__:
            self._projection = self._load_projection()

    def clone(self) -> InMemoryProjectRepository:
        """Return a transactional in-memory snapshot of durable state."""
        return self._projection.clone()

    def replace_with(self, other: Any) -> None:
        """Persist a staged in-memory repository and replace the live projection."""
        if not isinstance(other, InMemoryProjectRepository):
            raise TypeError("LadybugProjectRepository stages in-memory repositories")
        self._persist_snapshot(other)
        self._projection = other.clone()

    def load_command_replay_cache(
        self,
    ) -> dict[uuid.UUID, dict[str, CommandResult | CommandErrorResult]]:
        """Load durable command replay records for service idempotency."""
        cache: dict[uuid.UUID, dict[str, CommandResult | CommandErrorResult]] = {}
        for row in self._rows(
            """
            MATCH (replay:CommandReplay)
            RETURN replay.command_id, replay.payload_hash, replay.result_json
            ORDER BY replay.command_id
            """
        ):
            if row[1] == "records_base64":
                payload_text = base64.b64decode(row[2]).decode("utf-8")
            else:
                payload_text = row[2]
            payload = json.loads(payload_text)
            command_id = uuid.UUID(str(row[0]))
            records = payload.get("records")
            if isinstance(records, dict):
                for payload_hash, result_payload in records.items():
                    cache.setdefault(command_id, {})[payload_hash] = (
                        CommandResult.model_validate(result_payload)
                        if result_payload.get("ok")
                        else CommandErrorResult.model_validate(result_payload)
                    )
                continue
            cache.setdefault(command_id, {})[row[1]] = (
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
        self._conn.execute("MATCH (replay:CommandReplay) DELETE replay")
        for command_id, records in cache.items():
            result_json = json.dumps(
                {
                    "records": {
                        payload_hash: result.model_dump(mode="json")
                        for payload_hash, result in records.items()
                    }
                }
            )
            self._create_node(
                "CommandReplay",
                {
                    "command_id": str(command_id),
                    "payload_hash": "records_base64",
                    "result_json": base64.b64encode(
                        result_json.encode("utf-8"),
                    ).decode("ascii"),
                    "applied_at": dt.datetime.now(dt.UTC).isoformat(),
                },
            )

    def table_names(self) -> set[str]:
        """Return the graph table names currently visible to the connection."""
        rows = self._conn.execute("CALL show_tables() RETURN *").get_all()
        return {row[1] for row in rows}

    def column_names(self, table_name: str) -> set[str]:
        """Return column names for a graph table."""
        rows = self._conn.execute(f"CALL table_info('{table_name}') RETURN *").get_all()
        return {row[1] for row in rows}

    def create_project(
        self,
        name: str,
        start_at: dt.datetime,
        default_currency: str = "USD",
        project_id: str | None = None,
    ) -> SimpleNamespace:
        """Create and persist a project fact."""
        project_id = project_id or new_id()
        self._conn.execute(
            """
            CREATE (:Project {
                project_id: $project_id,
                name: $name,
                start_at: $start_at,
                default_currency: $default_currency
            })
            """,
            {
                "project_id": project_id,
                "name": name,
                "start_at": _isoformat_or_string(start_at),
                "default_currency": default_currency,
            },
        )
        return SimpleNamespace(project_id=project_id, name=name, start_at=start_at)

    def create_role(self, project_id: str, role_id: str, name: str) -> str:
        """Create a project-owned active role if it is absent."""
        if self._node_exists("Role", "role_id", role_id):
            self._ensure_project_role(project_id, role_id)
            return role_id
        self._conn.execute(
            """
            CREATE (:Role {
                role_id: $role_id,
                project_id: $project_id,
                name: $name,
                active: true
            })
            """,
            {"role_id": role_id, "project_id": project_id, "name": name},
        )
        self._ensure_project_role(project_id, role_id)
        return role_id

    def upsert_resource_calendar(
        self,
        project_id: str,
        calendar_id: str | None,
        name: str,
        timezone: str,
        weekly_windows: list[Any],
        active: bool = True,
    ) -> str:
        """Create or replace a resource calendar smoke-test projection."""
        resolved_calendar_id = calendar_id or new_id()
        if self._node_exists("ResourceCalendar", "calendar_id", resolved_calendar_id):
            self._conn.execute(
                """
                MATCH (calendar:ResourceCalendar)
                WHERE calendar.calendar_id = $calendar_id
                SET calendar.project_id = $project_id,
                    calendar.name = $name,
                    calendar.timezone = $timezone,
                    calendar.active = $active
                """,
                {
                    "calendar_id": resolved_calendar_id,
                    "project_id": project_id,
                    "name": name,
                    "timezone": timezone,
                    "active": active,
                },
            )
        else:
            self._conn.execute(
                """
                CREATE (:ResourceCalendar {
                    calendar_id: $calendar_id,
                    project_id: $project_id,
                    name: $name,
                    timezone: $timezone,
                    active: $active
                })
                """,
                {
                    "calendar_id": resolved_calendar_id,
                    "project_id": project_id,
                    "name": name,
                    "timezone": timezone,
                    "active": active,
                },
            )
        self._ensure_project_calendar(project_id, resolved_calendar_id)
        self._replace_calendar_weekly_windows(
            project_id,
            resolved_calendar_id,
            weekly_windows,
        )
        return resolved_calendar_id

    def _replace_calendar_weekly_windows(
        self,
        project_id: str,
        calendar_id: str,
        weekly_windows: list[Any],
    ) -> None:
        self._conn.execute(
            """
            MATCH (:ResourceCalendar)-[relation:HAS_WINDOW]->
                  (window:CalendarWeeklyWindow)
            WHERE window.calendar_id = $calendar_id
            DELETE relation
            """,
            {"calendar_id": calendar_id},
        )
        self._conn.execute(
            """
            MATCH (window:CalendarWeeklyWindow)
            WHERE window.calendar_id = $calendar_id
            DELETE window
            """,
            {"calendar_id": calendar_id},
        )
        for window in weekly_windows:
            window_id = _field_or_generated_id(window, "window_id")
            stored_window_id = _stored_scoped_child_id(calendar_id, window_id)
            window_properties = {
                "window_id": stored_window_id,
                "calendar_id": calendar_id,
                "project_id": project_id,
                "weekday": _field(window, "weekday"),
                "start_local_time": _field(window, "start_local_time"),
                "end_local_time": _field(window, "end_local_time"),
                "capacity_hours": _field(window, "capacity_hours"),
            }
            if self._has_column("CalendarWeeklyWindow", "logical_window_id"):
                window_properties["logical_window_id"] = window_id
            self._create_node("CalendarWeeklyWindow", window_properties)
            self._conn.execute(
                """
                MATCH (calendar:ResourceCalendar), (window:CalendarWeeklyWindow)
                WHERE calendar.calendar_id = $calendar_id
                  AND window.window_id = $window_id
                CREATE (calendar)-[:HAS_WINDOW]->(window)
                """,
                {"calendar_id": calendar_id, "window_id": stored_window_id},
            )

    def upsert_resource(
        self,
        project_id: str,
        resource_id: str | None,
        name: str,
        role_ids: list[str],
        calendar_id: str,
        available_from_at: dt.datetime,
        cost_rate: str,
        cost_unit: str,
        cost_currency: str | None = None,
        available_until_at: dt.datetime | None = None,
        holidays: list[Any] | None = None,
        active: bool = True,
    ) -> str:
        """Create or replace the resource fields needed by storage bootstrap."""
        resolved_resource_id = resource_id or new_id()
        currency = cost_currency or self.project_default_currency(project_id)
        parameters = {
            "resource_id": resolved_resource_id,
            "project_id": project_id,
            "name": name,
            "calendar_id": calendar_id,
            "available_from_at": _isoformat_or_string(available_from_at),
            "available_until_at": _isoformat_or_none(available_until_at),
            "cost_rate": str(cost_rate),
            "cost_unit": _value_or_enum_value(cost_unit),
            "cost_currency": currency,
            "active": active,
        }
        if self._node_exists("Resource", "resource_id", resolved_resource_id):
            self._conn.execute(
                """
                MATCH (resource:Resource)
                WHERE resource.resource_id = $resource_id
                SET resource.project_id = $project_id,
                    resource.name = $name,
                    resource.calendar_id = $calendar_id,
                    resource.available_from_at = $available_from_at,
                    resource.available_until_at = $available_until_at,
                    resource.cost_rate = $cost_rate,
                    resource.cost_unit = $cost_unit,
                    resource.cost_currency = $cost_currency,
                    resource.active = $active
                """,
                parameters,
            )
        else:
            self._conn.execute(
                """
                CREATE (:Resource {
                    resource_id: $resource_id,
                    project_id: $project_id,
                    name: $name,
                    calendar_id: $calendar_id,
                    available_from_at: $available_from_at,
                    available_until_at: $available_until_at,
                    cost_rate: $cost_rate,
                    cost_unit: $cost_unit,
                    cost_currency: $cost_currency,
                    active: $active
                })
                """,
                parameters,
            )
        self._ensure_project_resource(project_id, resolved_resource_id)
        self.set_resource_roles(project_id, resolved_resource_id, role_ids)
        self.set_resource_calendar(project_id, resolved_resource_id, calendar_id)
        self._replace_resource_holidays(
            project_id,
            resolved_resource_id,
            holidays or [],
        )
        return resolved_resource_id

    def _replace_resource_holidays(
        self,
        project_id: str,
        resource_id: str,
        holidays: list[Any],
    ) -> None:
        self._conn.execute(
            """
            MATCH (:Resource)-[relation:HAS_HOLIDAY]->
                  (holiday:ResourceHoliday)
            WHERE holiday.resource_id = $resource_id
            DELETE relation
            """,
            {"resource_id": resource_id},
        )
        self._conn.execute(
            """
            MATCH (holiday:ResourceHoliday)
            WHERE holiday.resource_id = $resource_id
            DELETE holiday
            """,
            {"resource_id": resource_id},
        )
        for holiday in holidays:
            holiday_id = _field(holiday, "holiday_id") or new_id()
            stored_holiday_id = _stored_scoped_child_id(resource_id, holiday_id)
            holiday_properties = {
                "holiday_id": stored_holiday_id,
                "resource_id": resource_id,
                "project_id": project_id,
                "starts_at": _isoformat_or_string(_field(holiday, "starts_at")),
                "ends_at": _isoformat_or_string(_field(holiday, "ends_at")),
                "reason": _field(holiday, "reason"),
            }
            if self._has_column("ResourceHoliday", "logical_holiday_id"):
                holiday_properties["logical_holiday_id"] = holiday_id
            self._create_node("ResourceHoliday", holiday_properties)
            self._conn.execute(
                """
                MATCH (resource:Resource), (holiday:ResourceHoliday)
                WHERE resource.resource_id = $resource_id
                  AND holiday.holiday_id = $holiday_id
                CREATE (resource)-[:HAS_HOLIDAY]->(holiday)
                """,
                {"resource_id": resource_id, "holiday_id": stored_holiday_id},
            )

    def set_resource_active(
        self,
        project_id: str,
        resource_id: str,
        active: bool,
    ) -> None:
        """Set the active flag for a project-owned resource."""
        self._conn.execute(
            """
            MATCH (resource:Resource)
            WHERE resource.project_id = $project_id
              AND resource.resource_id = $resource_id
            SET resource.active = $active
            """,
            {"project_id": project_id, "resource_id": resource_id, "active": active},
        )

    def set_calendar_active(
        self,
        project_id: str,
        calendar_id: str,
        active: bool,
    ) -> None:
        """Set the active flag for a project-owned calendar."""
        self._conn.execute(
            """
            MATCH (calendar:ResourceCalendar)
            WHERE calendar.project_id = $project_id
              AND calendar.calendar_id = $calendar_id
            SET calendar.active = $active
            """,
            {"project_id": project_id, "calendar_id": calendar_id, "active": active},
        )

    def set_resource_roles(
        self,
        project_id: str,
        resource_id: str,
        role_ids: list[str],
    ) -> None:
        """Replace a resource's role capability edges."""
        self._conn.execute(
            """
            MATCH (resource:Resource)-[relation:CAN_FILL]->(:Role)
            WHERE resource.project_id = $project_id
              AND resource.resource_id = $resource_id
            DELETE relation
            """,
            {"project_id": project_id, "resource_id": resource_id},
        )
        for role_id in role_ids:
            self._conn.execute(
                """
                MATCH (resource:Resource), (role:Role)
                WHERE resource.resource_id = $resource_id
                  AND role.role_id = $role_id
                  AND resource.project_id = $project_id
                  AND role.project_id = $project_id
                CREATE (resource)-[:CAN_FILL]->(role)
                """,
                {
                    "project_id": project_id,
                    "resource_id": resource_id,
                    "role_id": role_id,
                },
            )

    def set_resource_calendar(
        self,
        project_id: str,
        resource_id: str,
        calendar_id: str,
    ) -> None:
        """Replace a resource's calendar assignment edge and denormalized id."""
        self._conn.execute(
            """
            MATCH (resource:Resource)
            WHERE resource.project_id = $project_id
              AND resource.resource_id = $resource_id
            SET resource.calendar_id = $calendar_id
            """,
            {
                "project_id": project_id,
                "resource_id": resource_id,
                "calendar_id": calendar_id,
            },
        )
        self._conn.execute(
            """
            MATCH (resource:Resource)-[relation:USES_CALENDAR]->(:ResourceCalendar)
            WHERE resource.project_id = $project_id
              AND resource.resource_id = $resource_id
            DELETE relation
            """,
            {"project_id": project_id, "resource_id": resource_id},
        )
        self._conn.execute(
            """
            MATCH (resource:Resource), (calendar:ResourceCalendar)
            WHERE resource.resource_id = $resource_id
              AND calendar.calendar_id = $calendar_id
              AND resource.project_id = $project_id
              AND calendar.project_id = $project_id
            CREATE (resource)-[:USES_CALENDAR]->(calendar)
            """,
            {
                "project_id": project_id,
                "resource_id": resource_id,
                "calendar_id": calendar_id,
            },
        )

    def project_default_currency(self, project_id: str) -> str:
        """Return a project's default currency, falling back to USD."""
        rows = self._conn.execute(
            """
            MATCH (project:Project)
            WHERE project.project_id = $project_id
            RETURN project.default_currency
            """,
            {"project_id": project_id},
        ).get_all()
        if not rows:
            return "USD"
        return rows[0][0] or "USD"

    def _node_exists(self, label: str, key: str, value: str) -> bool:
        rows = self._conn.execute(
            f"""
            MATCH (node:{label})
            WHERE node.{key} = $value
            RETURN node.{key}
            """,
            {"value": value},
        ).get_all()
        return bool(rows)

    def _ensure_project_role(self, project_id: str, role_id: str) -> None:
        self._conn.execute(
            """
            MATCH (project:Project)-[relation:HAS_ROLE]->(role:Role)
            WHERE project.project_id = $project_id
              AND role.role_id = $role_id
            DELETE relation
            """,
            {"project_id": project_id, "role_id": role_id},
        )
        self._conn.execute(
            """
            MATCH (project:Project), (role:Role)
            WHERE project.project_id = $project_id
              AND role.role_id = $role_id
            CREATE (project)-[:HAS_ROLE]->(role)
            """,
            {"project_id": project_id, "role_id": role_id},
        )

    def _ensure_project_resource(self, project_id: str, resource_id: str) -> None:
        self._conn.execute(
            """
            MATCH (project:Project)-[relation:HAS_RESOURCE]->(resource:Resource)
            WHERE project.project_id = $project_id
              AND resource.resource_id = $resource_id
            DELETE relation
            """,
            {"project_id": project_id, "resource_id": resource_id},
        )
        self._conn.execute(
            """
            MATCH (project:Project), (resource:Resource)
            WHERE project.project_id = $project_id
              AND resource.resource_id = $resource_id
            CREATE (project)-[:HAS_RESOURCE]->(resource)
            """,
            {"project_id": project_id, "resource_id": resource_id},
        )

    def _ensure_project_calendar(self, project_id: str, calendar_id: str) -> None:
        self._conn.execute(
            """
            MATCH (project:Project)-[relation:HAS_CALENDAR]->
                  (calendar:ResourceCalendar)
            WHERE project.project_id = $project_id
              AND calendar.calendar_id = $calendar_id
            DELETE relation
            """,
            {"project_id": project_id, "calendar_id": calendar_id},
        )
        self._conn.execute(
            """
            MATCH (project:Project), (calendar:ResourceCalendar)
            WHERE project.project_id = $project_id
              AND calendar.calendar_id = $calendar_id
            CREATE (project)-[:HAS_CALENDAR]->(calendar)
            """,
            {"project_id": project_id, "calendar_id": calendar_id},
        )

    def _load_projection(self) -> InMemoryProjectRepository:
        """Load persisted graph facts into the service in-memory projection."""
        projection = InMemoryProjectRepository()
        dependency_rows = self._load_dependency_rows()
        dependencies_by_revision: dict[str, list[str]] = defaultdict(list)
        dependencies_by_successor: dict[str, list[str]] = defaultdict(list)
        for row in dependency_rows:
            project_id = row["project_id"]
            predecessor_id = row["predecessor_process_id"]
            successor_id = row["successor_process_id"]
            projection.dependency_edge_ids[
                (project_id, predecessor_id, successor_id)
            ] = row["edge_id"]
            if row.get("revision_id") is not None:
                dependencies_by_revision[row["revision_id"]].append(predecessor_id)
            else:
                dependencies_by_successor[successor_id].append(predecessor_id)

        requirements_by_revision = self._load_role_requirements(projection)
        for row in self._rows(
            """
            MATCH (project:Project)
            RETURN project.project_id, project.name, project.start_at,
                   project.default_currency
            ORDER BY project.project_id
            """
        ):
            project = ProjectRecord(
                project_id=row[0],
                name=row[1],
                start_at=_datetime_from_storage(row[2]),
                default_currency=row[3] or "USD",
            )
            projection.projects[project.project_id] = project

        for row in self._rows(
            """
            MATCH (process:Process)
            RETURN process.process_id, process.project_id, process.symbol,
                   process.status, process.started_at, process.finished_at,
                   process.is_active, process.retired_at
            ORDER BY process.project_id, process.process_id
            """
        ):
            retired_at = _datetime_or_none(row[7])
            started_at = _datetime_or_none(row[4])
            finished_at = _datetime_or_none(row[5])
            if retired_at is not None:
                process = RetiredProcessRecord(
                    process_id=row[0],
                    project_id=row[1],
                    symbol=row[2],
                    status=row[3] or "planned",
                    started_at=started_at,
                    finished_at=finished_at,
                    retired_at=retired_at,
                )
            else:
                process = ProcessRecord(
                    process_id=row[0],
                    project_id=row[1],
                    symbol=row[2],
                    status=row[3] or "planned",
                    started_at=started_at,
                    finished_at=finished_at,
                )
            projection.processes[process.process_id] = process
            projection.process_ids_by_project[process.project_id].append(
                process.process_id,
            )

        for row in self._rows(
            """
            MATCH (revision:ProcessRevision)
            RETURN revision.revision_id, revision.process_id, revision.project_id,
                   revision.effective_at, revision.name,
                   revision.description,
                   revision.duration_business_days, revision.due_at,
                   revision.earliest_start_at, revision.start_at_earliest,
                   revision.delay_after_dependencies_business_days,
                   revision.assumption_note
            ORDER BY revision.process_id, revision.effective_at, revision.revision_id
            """
        ):
            dependencies = dependencies_by_revision.get(row[0])
            if dependencies is None:
                dependencies = dependencies_by_successor.get(row[1], [])
            revision = ProcessRevisionRecord(
                revision_id=row[0],
                process_id=row[1],
                project_id=row[2],
                effective_at=_datetime_from_storage(row[3]),
                name=row[4],
                description=row[5] or "",
                duration_business_days=row[6],
                dependencies=list(dict.fromkeys(dependencies)),
                due_at=_datetime_or_none(row[7]),
                earliest_start_at=_datetime_or_none(row[8]),
                start_at_earliest=bool(row[9]),
                delay_after_dependencies_business_days=row[10] or 0,
                required_roles=self._load_required_roles(row[0]),
                role_requirements=requirements_by_revision.get(row[0], []),
                assumption_note=row[11],
            )
            projection.revisions_by_process[revision.process_id].append(revision)

        self._load_roles(projection)
        self._load_calendars(projection)
        self._load_resources(projection)
        self._load_blockers(projection)
        self._load_due_history(projection)
        self._load_schedule_snapshots(projection)
        self._load_aliases(projection)
        self._load_retirements(projection)
        return projection

    def _persist_snapshot(self, repository: InMemoryProjectRepository) -> None:
        """Rewrite durable graph facts from a staged in-memory projection."""
        self._validate_snapshot_storage_keys(repository)
        previous = self._projection.clone()
        self._clear_snapshot()
        try:
            self._write_snapshot(repository)
        except Exception:
            self._clear_snapshot()
            self._write_snapshot(previous)
            raise

    def _write_snapshot(self, repository: InMemoryProjectRepository) -> None:
        """Write an already validated in-memory projection to empty tables."""
        for project in repository.projects.values():
            self._create_node(
                "Project",
                {
                    "project_id": project.project_id,
                    "name": project.name,
                    "start_at": _isoformat_or_string(project.start_at),
                    "default_currency": project.default_currency,
                },
            )
        self._persist_roles(repository)
        self._persist_calendars(repository)
        for process in repository.processes.values():
            retirement = repository.retired_processes.get(process.process_id, {})
            retired_at = retirement.get("retired_at", getattr(process, "retired_at", None))
            process_properties = {
                "process_id": process.process_id,
                "project_id": process.project_id,
                "symbol": process.symbol,
                "status": _value_or_enum_value(process.status),
                "finished_at": _isoformat_or_none(process.finished_at),
                "is_active": retired_at is None,
                "retired_at": _isoformat_or_none(retired_at),
                "retired_by_command_id": retirement.get("retired_by_command_id"),
                "retirement_reason": retirement.get("retirement_reason"),
            }
            process_properties["started_at"] = _isoformat_or_none(
                process.started_at,
            )
            self._create_node(
                "Process",
                process_properties,
            )
            self._create_relationship(
                "Project",
                "project_id",
                process.project_id,
                "HAS_PROCESS",
                "Process",
                "process_id",
                process.process_id,
            )
        for process_id, revisions in repository.revisions_by_process.items():
            for revision in revisions:
                self._create_revision_node(revision)
                self._create_relationship(
                    "Process",
                    "process_id",
                    process_id,
                    "HAS_REVISION",
                    "ProcessRevision",
                    "revision_id",
                    revision.revision_id,
                )
                for predecessor_id in revision.dependencies:
                    edge_id = repository.dependency_edge_ids.get(
                        (revision.project_id, predecessor_id, revision.process_id),
                        f"edge-{predecessor_id}-{revision.process_id}",
                    )
                    edge_props = {
                        "edge_id": edge_id,
                        "project_id": revision.project_id,
                        "dependency_type": "finish_to_start",
                        "retired_at": None,
                        "retired_by_command_id": None,
                        "retirement_reason": None,
                    }
                    if self._has_column("DEPENDS_ON", "revision_id"):
                        edge_props["revision_id"] = revision.revision_id
                    self._create_relationship(
                        "Process",
                        "process_id",
                        predecessor_id,
                        "DEPENDS_ON",
                        "Process",
                        "process_id",
                        revision.process_id,
                        edge_props,
                    )
        self._persist_retirements(repository)
        self._persist_resources(repository)
        self._persist_aliases(repository)
        self._persist_blockers(repository)
        self._persist_due_history(repository)
        self._persist_schedule_snapshots(repository)

    def _validate_snapshot_storage_keys(
        self,
        repository: InMemoryProjectRepository,
    ) -> None:
        """Fail before clearing durable tables when graph primary keys collide."""
        checks: dict[str, list[str]] = {
            "Project.project_id": list(repository.projects),
            "Process.process_id": list(repository.processes),
            "Role.role_id": list(repository.roles),
            "ResourceCalendar.calendar_id": list(repository.calendars),
            "Resource.resource_id": list(repository.resources),
            "Blocker.blocker_id": list(repository.blockers),
            "ProcessRevision.revision_id": [
                revision.revision_id
                for revisions in repository.revisions_by_process.values()
                for revision in revisions
            ],
            "CalendarWeeklyWindow.window_id": [
                _stored_scoped_child_id(calendar["calendar_id"], window["window_id"])
                for calendar in repository.calendars.values()
                for window in calendar["weekly_windows"]
            ],
            "CalendarException.exception_id": [
                _stored_scoped_child_id(
                    calendar["calendar_id"],
                    exception["exception_id"],
                )
                for calendar in repository.calendars.values()
                for exception in calendar["exceptions"]
            ],
            "ResourceHoliday.holiday_id": [
                _stored_scoped_child_id(resource["resource_id"], holiday["holiday_id"])
                for resource in repository.resources.values()
                for holiday in resource.get("holidays", [])
            ],
            "DueDateHistoryEvent.event_id": [
                event["event_id"] for event in repository.due_history_events
            ],
            "ScheduleSnapshot.snapshot_id": [
                snapshot.snapshot_id for snapshot in repository.schedule_snapshots
            ],
            "ProcessRetirementEvent.retirement_event_id": [
                retirement["retirement_event_id"]
                for retirement in repository.retired_processes.values()
            ],
        }
        role_requirement_ids = []
        for revisions in repository.revisions_by_process.values():
            for revision in revisions:
                for index, requirement in enumerate(revision.role_requirements):
                    role_requirement_ids.append(
                        _stored_role_requirement_id(
                            revision.revision_id,
                            requirement.requirement_id,
                            index,
                        )
                    )
        checks["RoleRequirement.requirement_id"] = role_requirement_ids

        for label, values in checks.items():
            seen: set[str] = set()
            duplicates: set[str] = set()
            for value in values:
                if value in seen:
                    duplicates.add(value)
                seen.add(value)
            if duplicates:
                raise ServiceValidationError(
                    code="storage_key_conflict",
                    message="Durable storage primary keys must be globally unique.",
                    details={
                        "storage_key": label,
                        "duplicates": sorted(duplicates),
                    },
                )

    def _clear_snapshot(self) -> None:
        for table_name in SNAPSHOT_REL_TABLES:
            self._conn.execute(f"MATCH ()-[relation:{table_name}]->() DELETE relation")
        for table_name in SNAPSHOT_NODE_TABLES:
            self._conn.execute(f"MATCH (node:{table_name}) DELETE node")

    def _create_revision_node(self, revision: ProcessRevisionRecord) -> None:
        properties = {
            "revision_id": revision.revision_id,
            "process_id": revision.process_id,
            "project_id": revision.project_id,
            "effective_at": _isoformat_or_string(revision.effective_at),
            "name": revision.name,
            "description": revision.description,
            "duration_business_days": revision.duration_business_days,
            "due_at": _isoformat_or_none(revision.due_at),
            "earliest_start_at": _isoformat_or_none(revision.earliest_start_at),
            "start_at_earliest": revision.start_at_earliest,
            "delay_after_dependencies_business_days": (
                revision.delay_after_dependencies_business_days
            ),
            "assumption_note": revision.assumption_note,
        }
        if self._has_column("ProcessRevision", "required_roles_json"):
            properties["required_roles_json"] = json.dumps(revision.required_roles)
        self._create_node("ProcessRevision", properties)
        for index, requirement in enumerate(revision.role_requirements):
            stored_requirement_id = _stored_role_requirement_id(
                revision.revision_id,
                requirement.requirement_id,
                index,
            )
            requirement_properties = {
                "requirement_id": stored_requirement_id,
                "revision_id": revision.revision_id,
                "project_id": revision.project_id,
                "process_id": revision.process_id,
                "role_id": requirement.role_id,
                "effort_hours": float(requirement.effort_hours),
                "min_allocation_hours_per_day": (
                    requirement.min_allocation_hours_per_day
                ),
                "max_allocation_hours_per_day": (
                    requirement.max_allocation_hours_per_day
                ),
                "required_resource_count": requirement.required_resource_count,
                "allocation_policy": _value_or_enum_value(
                    requirement.allocation_policy,
                ),
            }
            if self._has_column("RoleRequirement", "logical_requirement_id"):
                requirement_properties["logical_requirement_id"] = (
                    requirement.requirement_id
                )
            self._create_node("RoleRequirement", requirement_properties)
            self._create_relationship(
                "ProcessRevision",
                "revision_id",
                revision.revision_id,
                "REQUIRES_ROLE",
                "RoleRequirement",
                "requirement_id",
                stored_requirement_id,
            )
            self._create_relationship(
                "RoleRequirement",
                "requirement_id",
                stored_requirement_id,
                "REQUIREMENT_ROLE",
                "Role",
                "role_id",
                requirement.role_id,
            )

    def _persist_roles(self, repository: InMemoryProjectRepository) -> None:
        for role in repository.roles.values():
            self._create_node("Role", role)
            self._create_relationship(
                "Project",
                "project_id",
                role["project_id"],
                "HAS_ROLE",
                "Role",
                "role_id",
                role["role_id"],
            )

    def _persist_calendars(self, repository: InMemoryProjectRepository) -> None:
        for calendar in repository.calendars.values():
            self._create_node(
                "ResourceCalendar",
                {
                    "calendar_id": calendar["calendar_id"],
                    "project_id": calendar["project_id"],
                    "name": calendar["name"],
                    "timezone": calendar["timezone"],
                    "active": calendar["active"],
                },
            )
            self._create_relationship(
                "Project",
                "project_id",
                calendar["project_id"],
                "HAS_CALENDAR",
                "ResourceCalendar",
                "calendar_id",
                calendar["calendar_id"],
            )
            for window in calendar["weekly_windows"]:
                stored_window_id = _stored_scoped_child_id(
                    calendar["calendar_id"],
                    window["window_id"],
                )
                window_properties = {
                    "window_id": stored_window_id,
                    "calendar_id": calendar["calendar_id"],
                    "project_id": calendar["project_id"],
                    "weekday": window["weekday"],
                    "start_local_time": window["start_local_time"],
                    "end_local_time": window["end_local_time"],
                    "capacity_hours": window["capacity_hours"],
                }
                if self._has_column("CalendarWeeklyWindow", "logical_window_id"):
                    window_properties["logical_window_id"] = window["window_id"]
                self._create_node("CalendarWeeklyWindow", window_properties)
                self._create_relationship(
                    "ResourceCalendar",
                    "calendar_id",
                    calendar["calendar_id"],
                    "HAS_WINDOW",
                    "CalendarWeeklyWindow",
                    "window_id",
                    stored_window_id,
                )
            for exception in calendar["exceptions"]:
                stored_exception_id = _stored_scoped_child_id(
                    calendar["calendar_id"],
                    exception["exception_id"],
                )
                exception_properties = {
                    "exception_id": stored_exception_id,
                    "calendar_id": calendar["calendar_id"],
                    "project_id": calendar["project_id"],
                    "starts_at": _isoformat_or_string(exception["starts_at"]),
                    "ends_at": _isoformat_or_string(exception["ends_at"]),
                    "capacity_hours": exception["capacity_hours"],
                    "reason": exception.get("reason"),
                }
                if self._has_column("CalendarException", "logical_exception_id"):
                    exception_properties["logical_exception_id"] = (
                        exception["exception_id"]
                    )
                self._create_node("CalendarException", exception_properties)
                self._create_relationship(
                    "ResourceCalendar",
                    "calendar_id",
                    calendar["calendar_id"],
                    "HAS_EXCEPTION",
                    "CalendarException",
                    "exception_id",
                    stored_exception_id,
                )

    def _persist_resources(self, repository: InMemoryProjectRepository) -> None:
        for resource in repository.resources.values():
            self._create_node(
                "Resource",
                {
                    "resource_id": resource["resource_id"],
                    "project_id": resource["project_id"],
                    "name": resource["name"],
                    "calendar_id": resource["calendar_id"],
                    "available_from_at": _isoformat_or_string(
                        resource["available_from_at"],
                    ),
                    "available_until_at": _isoformat_or_none(
                        resource.get("available_until_at"),
                    ),
                    "cost_rate": str(resource["cost_rate"]),
                    "cost_unit": _value_or_enum_value(resource["cost_unit"]),
                    "cost_currency": resource["cost_currency"],
                    "active": resource["active"],
                },
            )
            self._create_relationship(
                "Project",
                "project_id",
                resource["project_id"],
                "HAS_RESOURCE",
                "Resource",
                "resource_id",
                resource["resource_id"],
            )
            self._create_relationship(
                "Resource",
                "resource_id",
                resource["resource_id"],
                "USES_CALENDAR",
                "ResourceCalendar",
                "calendar_id",
                resource["calendar_id"],
            )
            for role_id in resource["role_ids"]:
                self._create_relationship(
                    "Resource",
                    "resource_id",
                    resource["resource_id"],
                    "CAN_FILL",
                    "Role",
                    "role_id",
                    role_id,
            )
            for holiday in resource.get("holidays", []):
                stored_holiday_id = _stored_scoped_child_id(
                    resource["resource_id"],
                    holiday["holiday_id"],
                )
                holiday_properties = {
                    "holiday_id": stored_holiday_id,
                    "resource_id": resource["resource_id"],
                    "project_id": resource["project_id"],
                    "starts_at": _isoformat_or_string(holiday["starts_at"]),
                    "ends_at": _isoformat_or_string(holiday["ends_at"]),
                    "reason": holiday.get("reason"),
                }
                if self._has_column("ResourceHoliday", "logical_holiday_id"):
                    holiday_properties["logical_holiday_id"] = holiday["holiday_id"]
                self._create_node("ResourceHoliday", holiday_properties)
                self._create_relationship(
                    "Resource",
                    "resource_id",
                    resource["resource_id"],
                    "HAS_HOLIDAY",
                    "ResourceHoliday",
                    "holiday_id",
                    stored_holiday_id,
                )

    def _persist_aliases(self, repository: InMemoryProjectRepository) -> None:
        for project_id, aliases in repository.process_aliases.items():
            for index, (alias, process_id) in enumerate(sorted(aliases.items())):
                properties = {
                    "alias_id": f"alias-{project_id}-{index}",
                    "project_id": project_id,
                    "process_id": process_id,
                    "alias": alias,
                    "created_at": None,
                }
                if self._has_column("ProcessAlias", "source"):
                    properties["source"] = repository.process_alias_sources.get(
                        project_id,
                        {},
                    ).get(alias, "manual")
                self._create_node("ProcessAlias", properties)
                self._create_relationship(
                    "Process",
                    "process_id",
                    process_id,
                    "HAS_ALIAS",
                    "ProcessAlias",
                    "alias_id",
                    properties["alias_id"],
                )

    def _persist_blockers(self, repository: InMemoryProjectRepository) -> None:
        for blocker in repository.blockers.values():
            self._create_node(
                "Blocker",
                {
                    "blocker_id": blocker.blocker_id,
                    "project_id": blocker.project_id,
                    "process_id": blocker.process_id,
                    "description": blocker.description,
                    "opened_at": _isoformat_or_string(blocker.opened_at),
                    "resolved_at": _isoformat_or_none(blocker.resolved_at),
                    "summary": blocker.summary,
                    "details": blocker.details,
                    "severity": _value_or_enum_value(blocker.severity),
                    "created_at": _isoformat_or_none(
                        blocker.created_at or blocker.opened_at,
                    ),
                    "resolution": blocker.resolution,
                },
            )
            self._create_relationship(
                "Process",
                "process_id",
                blocker.process_id,
                "HAS_BLOCKER",
                "Blocker",
                "blocker_id",
                blocker.blocker_id,
            )
            self._create_relationship(
                "Blocker",
                "blocker_id",
                blocker.blocker_id,
                "BLOCKS",
                "Process",
                "process_id",
                blocker.process_id,
            )

    def _persist_due_history(self, repository: InMemoryProjectRepository) -> None:
        for event in repository.due_history_events:
            self._create_node(
                "DueDateHistoryEvent",
                {
                    "event_id": event["event_id"],
                    "project_id": event["project_id"],
                    "process_id": event["process_id"],
                    "mutation_action": event["mutation_action"],
                    "edit_at": _isoformat_or_string(event["edit_at"]),
                    "before_due_at": _isoformat_or_none(event["before_due_at"]),
                    "after_due_at": _isoformat_or_none(event["after_due_at"]),
                    "command_id": event["command_id"],
                },
            )
            if event["process_id"] is not None:
                self._create_relationship(
                    "Process",
                    "process_id",
                    event["process_id"],
                    "HAS_DUE_DATE_EVENT",
                    "DueDateHistoryEvent",
                    "event_id",
                    event["event_id"],
                )

    def _persist_schedule_snapshots(
        self,
        repository: InMemoryProjectRepository,
    ) -> None:
        for snapshot in repository.schedule_snapshots:
            self._create_node(
                "ScheduleSnapshot",
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "project_id": snapshot.project_id,
                    "committed_at": _isoformat_or_string(snapshot.committed_at),
                    "terminal_process_symbols": snapshot.terminal_process_symbols,
                    "schedule_basis": _value_or_enum_value(snapshot.schedule_basis),
                    "completion_at": _isoformat_or_none(snapshot.completion_at),
                    "derived_due_at": _isoformat_or_none(snapshot.derived_due_at),
                    "horizon_starts_at": _isoformat_or_string(
                        snapshot.horizon_starts_at,
                    ),
                    "horizon_ends_at": _isoformat_or_string(snapshot.horizon_ends_at),
                    "converged": snapshot.converged,
                    "unallocated_count": snapshot.unallocated_count,
                    "note": snapshot.note,
                },
            )

    def _persist_retirements(self, repository: InMemoryProjectRepository) -> None:
        for process_id, retirement in repository.retired_processes.items():
            self._create_node(
                "ProcessRetirementEvent",
                {
                    "retirement_event_id": retirement["retirement_event_id"],
                    "project_id": repository.processes[process_id].project_id,
                    "process_id": process_id,
                    "retired_at": _isoformat_or_string(retirement["retired_at"]),
                    "retired_by_command_id": retirement["retired_by_command_id"],
                    "retirement_reason": retirement["retirement_reason"],
                    "replacement_process_ids": retirement["replacement_process_ids"],
                },
            )

    def _load_required_roles(self, revision_id: str) -> dict[str, float]:
        if not self._has_column("ProcessRevision", "required_roles_json"):
            return {}
        rows = self._rows(
            """
            MATCH (revision:ProcessRevision)
            WHERE revision.revision_id = $revision_id
            RETURN revision.required_roles_json
            """,
            {"revision_id": revision_id},
        )
        if not rows or rows[0][0] is None:
            return {}
        return json.loads(rows[0][0])

    def _load_dependency_rows(self) -> list[dict[str, Any]]:
        if "DEPENDS_ON" not in self.table_names():
            return []
        revision_clause = (
            ", edge.revision_id"
            if self._has_column("DEPENDS_ON", "revision_id")
            else ""
        )
        rows = self._rows(
            f"""
            MATCH (predecessor:Process)-[edge:DEPENDS_ON]->(successor:Process)
            RETURN predecessor.process_id, successor.process_id, edge.edge_id,
                   edge.project_id, predecessor.project_id{revision_clause}
            ORDER BY successor.process_id, predecessor.process_id
            """
        )
        records = []
        for row in rows:
            records.append(
                {
                    "predecessor_process_id": row[0],
                    "successor_process_id": row[1],
                    "edge_id": row[2] or f"edge-{row[0]}-{row[1]}",
                    "project_id": row[3] or row[4],
                    "revision_id": row[5] if len(row) > 5 else None,
                }
            )
        return records

    def _load_role_requirements(
        self,
        projection: InMemoryProjectRepository,
    ) -> dict[str, list[RoleRequirementCommand]]:
        requirements: dict[str, list[RoleRequirementCommand]] = defaultdict(list)
        logical_id_expression = (
            "requirement.logical_requirement_id"
            if self._has_column("RoleRequirement", "logical_requirement_id")
            else "NULL"
        )
        for row in self._rows(
            f"""
            MATCH (requirement:RoleRequirement)
            RETURN requirement.requirement_id, requirement.revision_id,
                   requirement.role_id, requirement.effort_hours,
                   requirement.min_allocation_hours_per_day,
                   requirement.max_allocation_hours_per_day,
                   requirement.required_resource_count,
                   requirement.allocation_policy,
                   {logical_id_expression}
            ORDER BY requirement.revision_id, requirement.requirement_id
            """
        ):
            stored_id = row[0]
            requirement_id = _logical_role_requirement_id(stored_id, row[8])
            requirement = RoleRequirementCommand(
                requirement_id=requirement_id,
                role_id=row[2],
                effort_hours=row[3],
                min_allocation_hours_per_day=row[4],
                max_allocation_hours_per_day=row[5],
                required_resource_count=row[6] or 1,
                allocation_policy=row[7] or "split_allowed",
            )
            requirements[row[1]].append(requirement)
            if requirement.requirement_id is not None:
                projection.role_requirements[requirement.requirement_id] = requirement
        return requirements

    def _load_roles(self, projection: InMemoryProjectRepository) -> None:
        for row in self._rows(
            """
            MATCH (role:Role)
            RETURN role.role_id, role.project_id, role.name, role.active
            ORDER BY role.project_id, role.role_id
            """
        ):
            role = {
                "role_id": row[0],
                "project_id": row[1],
                "name": row[2],
                "active": True if row[3] is None else bool(row[3]),
            }
            projection.roles[role["role_id"]] = role
            projection.role_ids_by_project[role["project_id"]].append(role["role_id"])

    def _load_calendars(self, projection: InMemoryProjectRepository) -> None:
        windows_by_calendar: dict[str, list[dict[str, Any]]] = defaultdict(list)
        logical_window_expression = (
            "window.logical_window_id"
            if self._has_column("CalendarWeeklyWindow", "logical_window_id")
            else "NULL"
        )
        for row in self._rows(
            f"""
            MATCH (window:CalendarWeeklyWindow)
            RETURN window.window_id, window.calendar_id, window.weekday,
                   window.start_local_time, window.end_local_time,
                   window.capacity_hours, {logical_window_expression}
            ORDER BY window.calendar_id, window.weekday, window.start_local_time
            """
        ):
            windows_by_calendar[row[1]].append(
                {
                    "window_id": _logical_scoped_child_id(row[1], row[0], row[6]),
                    "weekday": row[2],
                    "start_local_time": row[3],
                    "end_local_time": row[4],
                    "capacity_hours": row[5],
                },
            )
        exceptions_by_calendar: dict[str, list[dict[str, Any]]] = defaultdict(list)
        logical_exception_expression = (
            "exception.logical_exception_id"
            if self._has_column("CalendarException", "logical_exception_id")
            else "NULL"
        )
        for row in self._rows(
            f"""
            MATCH (exception:CalendarException)
            RETURN exception.exception_id, exception.calendar_id,
                   exception.project_id, exception.starts_at, exception.ends_at,
                   exception.capacity_hours, exception.reason,
                   {logical_exception_expression}
            ORDER BY exception.calendar_id, exception.starts_at
            """
        ):
            exceptions_by_calendar[row[1]].append(
                {
                    "exception_id": _logical_scoped_child_id(row[1], row[0], row[7]),
                    "calendar_id": row[1],
                    "project_id": row[2],
                    "starts_at": _datetime_from_storage(row[3]),
                    "ends_at": _datetime_from_storage(row[4]),
                    "capacity_hours": row[5],
                    "reason": row[6],
                },
            )
        for row in self._rows(
            """
            MATCH (calendar:ResourceCalendar)
            RETURN calendar.calendar_id, calendar.project_id, calendar.name,
                   calendar.timezone, calendar.active
            ORDER BY calendar.project_id, calendar.calendar_id
            """
        ):
            calendar = {
                "calendar_id": row[0],
                "project_id": row[1],
                "name": row[2],
                "timezone": row[3],
                "weekly_windows": windows_by_calendar.get(row[0], []),
                "exceptions": exceptions_by_calendar.get(row[0], []),
                "active": True if row[4] is None else bool(row[4]),
            }
            projection.calendars[calendar["calendar_id"]] = calendar
            projection.calendar_ids_by_project[calendar["project_id"]].append(
                calendar["calendar_id"],
            )

    def _load_resources(self, projection: InMemoryProjectRepository) -> None:
        roles_by_resource: dict[str, list[str]] = defaultdict(list)
        for row in self._rows(
            """
            MATCH (resource:Resource)-[:CAN_FILL]->(role:Role)
            RETURN resource.resource_id, role.role_id
            ORDER BY resource.resource_id, role.role_id
            """
        ):
            roles_by_resource[row[0]].append(row[1])
        holidays_by_resource: dict[str, list[dict[str, Any]]] = defaultdict(list)
        logical_holiday_expression = (
            "holiday.logical_holiday_id"
            if self._has_column("ResourceHoliday", "logical_holiday_id")
            else "NULL"
        )
        for row in self._rows(
            f"""
            MATCH (holiday:ResourceHoliday)
            RETURN holiday.holiday_id, holiday.resource_id, holiday.starts_at,
                   holiday.ends_at, holiday.reason, {logical_holiday_expression}
            ORDER BY holiday.resource_id, holiday.starts_at
            """
        ):
            holidays_by_resource[row[1]].append(
                {
                    "holiday_id": _logical_scoped_child_id(row[1], row[0], row[5]),
                    "starts_at": _datetime_from_storage(row[2]),
                    "ends_at": _datetime_from_storage(row[3]),
                    "reason": row[4],
                },
            )
        for row in self._rows(
            """
            MATCH (resource:Resource)
            RETURN resource.resource_id, resource.project_id, resource.name,
                   resource.calendar_id, resource.available_from_at,
                   resource.available_until_at, resource.cost_rate,
                   resource.cost_unit, resource.cost_currency, resource.active
            ORDER BY resource.project_id, resource.resource_id
            """
        ):
            resource = RecordDict({
                "resource_id": row[0],
                "project_id": row[1],
                "name": row[2],
                "role_ids": roles_by_resource.get(row[0], []),
                "calendar_id": row[3],
                "available_from_at": _datetime_from_storage(row[4]),
                "available_until_at": _datetime_or_none(row[5]),
                "cost_rate": row[6],
                "cost_unit": row[7],
                "cost_currency": row[8],
                "holidays": holidays_by_resource.get(row[0], []),
                "active": True if row[9] is None else bool(row[9]),
            })
            projection.resources[resource["resource_id"]] = resource
            projection.resource_ids_by_project[resource["project_id"]].append(
                resource["resource_id"],
            )

    def _load_blockers(self, projection: InMemoryProjectRepository) -> None:
        for row in self._rows(
            """
            MATCH (blocker:Blocker)
            RETURN blocker.blocker_id, blocker.project_id, blocker.process_id,
                   blocker.description, blocker.opened_at, blocker.resolved_at,
                   blocker.summary, blocker.details, blocker.severity,
                   blocker.created_at, blocker.resolution
            ORDER BY blocker.project_id, blocker.blocker_id
            """
        ):
            opened_at = _datetime_or_none(row[4]) or _datetime_from_storage(row[9])
            blocker = BlockerRecord(
                blocker_id=row[0],
                project_id=row[1],
                process_id=row[2],
                description=row[3] or row[6],
                opened_at=opened_at,
                resolved_at=_datetime_or_none(row[5]),
                summary=row[6] or row[3],
                details=row[7],
                severity=row[8] or "blocking",
                created_at=_datetime_or_none(row[9]) or opened_at,
                resolution=row[10],
            )
            projection.blockers[blocker.blocker_id] = blocker
            projection.blocker_ids_by_project[blocker.project_id].append(
                blocker.blocker_id,
            )

    def _load_due_history(self, projection: InMemoryProjectRepository) -> None:
        for row in self._rows(
            """
            MATCH (event:DueDateHistoryEvent)
            RETURN event.event_id, event.project_id, event.process_id,
                   event.mutation_action, event.edit_at, event.before_due_at,
                   event.after_due_at, event.command_id
            ORDER BY event.edit_at, event.event_id
            """
        ):
            event = {
                "event_id": row[0],
                "project_id": row[1],
                "process_id": row[2],
                "mutation_action": row[3],
                "edit_at": _datetime_from_storage(row[4]),
                "before_due_at": _datetime_or_none(row[5]),
                "after_due_at": _datetime_or_none(row[6]),
                "command_id": row[7],
            }
            projection.due_history_events.append(event)
            if (
                event["process_id"] is None
                and event["mutation_action"]
                in {"set_project_due_at", "clear_project_due_at"}
            ):
                projection.project_due_at[event["project_id"]] = event["after_due_at"]

    def _load_schedule_snapshots(self, projection: InMemoryProjectRepository) -> None:
        if "ScheduleSnapshot" not in self.table_names():
            return
        for row in self._rows(
            """
            MATCH (snapshot:ScheduleSnapshot)
            RETURN snapshot.snapshot_id, snapshot.project_id, snapshot.committed_at,
                   snapshot.terminal_process_symbols, snapshot.schedule_basis,
                   snapshot.completion_at, snapshot.derived_due_at,
                   snapshot.horizon_starts_at, snapshot.horizon_ends_at,
                   snapshot.converged, snapshot.unallocated_count, snapshot.note
            ORDER BY snapshot.project_id, snapshot.committed_at, snapshot.snapshot_id
            """
        ):
            projection.schedule_snapshots.append(
                ScheduleSnapshotRecord(
                    snapshot_id=row[0],
                    project_id=row[1],
                    committed_at=_datetime_from_storage(row[2]),
                    terminal_process_symbols=list(row[3] or []),
                    schedule_basis=row[4] or "resource_aware",
                    completion_at=_datetime_or_none(row[5]),
                    derived_due_at=_datetime_or_none(row[6]),
                    horizon_starts_at=_datetime_from_storage(row[7]),
                    horizon_ends_at=_datetime_from_storage(row[8]),
                    converged=row[9],
                    unallocated_count=row[10] or 0,
                    note=row[11],
                )
            )

    def _load_aliases(self, projection: InMemoryProjectRepository) -> None:
        source_clause = ", alias.source" if self._has_column("ProcessAlias", "source") else ""
        rows = self._rows(
            f"""
            MATCH (alias:ProcessAlias)
            RETURN alias.project_id, alias.process_id, alias.alias{source_clause}
            ORDER BY alias.project_id, alias.alias
            """
        )
        for row in rows:
            projection.process_aliases[row[0]][row[2]] = row[1]
            projection.process_alias_sources[row[0]][row[2]] = (
                row[3] if len(row) > 3 and row[3] is not None else "manual"
            )

    def _load_retirements(self, projection: InMemoryProjectRepository) -> None:
        for row in self._rows(
            """
            MATCH (event:ProcessRetirementEvent)
            RETURN event.retirement_event_id, event.process_id, event.retired_at,
                   event.retired_by_command_id, event.retirement_reason,
                   event.replacement_process_ids
            ORDER BY event.process_id
            """
        ):
            projection.retired_processes[row[1]] = {
                "retirement_event_id": row[0],
                "process_id": row[1],
                "retired_at": _datetime_from_storage(row[2]),
                "retired_by_command_id": row[3],
                "retirement_reason": row[4],
                "replacement_process_ids": list(row[5] or []),
            }
        for process_id, process in projection.processes.items():
            retired_at = getattr(process, "retired_at", None)
            if retired_at is None or process_id in projection.retired_processes:
                continue
            projection.retired_processes[process_id] = {
                "retirement_event_id": f"retirement-{process_id}-loaded",
                "process_id": process_id,
                "retired_at": retired_at,
                "retired_by_command_id": None,
                "retirement_reason": "loaded_retired_process",
                "replacement_process_ids": [],
            }

    def _create_node(self, label: str, properties: dict[str, Any]) -> None:
        assignments = ",\n".join(f"{key}: ${key}" for key in properties)
        self._conn.execute(f"CREATE (:{label} {{{assignments}}})", properties)

    def _create_relationship(
        self,
        source_label: str,
        source_key: str,
        source_value: str,
        relation_label: str,
        target_label: str,
        target_key: str,
        target_value: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        parameters = {"source_value": source_value, "target_value": target_value}
        properties = properties or {}
        rel_properties = ""
        if properties:
            parameters.update(properties)
            rel_properties = " {" + ", ".join(
                f"{key}: ${key}" for key in properties
            ) + "}"
        self._conn.execute(
            f"""
            MATCH (source:{source_label}), (target:{target_label})
            WHERE source.{source_key} = $source_value
              AND target.{target_key} = $target_value
            CREATE (source)-[:{relation_label}{rel_properties}]->(target)
            """,
            parameters,
        )

    def _rows(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
    ) -> list[list[Any]]:
        if parameters is None:
            return self._conn.execute(query).get_all()
        return self._conn.execute(query, parameters).get_all()

    def _has_column(self, table_name: str, column_name: str) -> bool:
        return column_name in self.column_names(table_name)


def _import_ladybug():
    """Import the current LadybugDB Python package name.

    The official docs have shown both `ladybug` and `real_ladybug` imports. The
    installation page currently installs `real_ladybug`, so prefer that while
    retaining the shorter import as a compatibility fallback.
    """
    try:
        import real_ladybug as lb
    except ModuleNotFoundError:
        import ladybug as lb
    return lb


def _isoformat_or_string(value: Any) -> str:
    """Serialize datetime-like values while preserving already-serialized strings."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _isoformat_or_none(value: Any) -> str | None:
    """Serialize optional datetime-like values."""
    if value is None:
        return None
    return _isoformat_or_string(value)


def _datetime_from_storage(value: Any) -> dt.datetime:
    """Parse a stored ISO datetime and require timezone awareness."""
    parsed = _datetime_or_none(value)
    if parsed is None:
        raise ValueError("Stored datetime value cannot be null.")
    return parsed


def _datetime_or_none(value: Any) -> dt.datetime | None:
    """Parse an optional stored ISO datetime while preserving timezone offsets."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError("Stored datetimes must be timezone-aware.")
    return parsed


def _value_or_enum_value(value: Any) -> Any:
    """Return enum values without requiring callers to unwrap Pydantic models."""
    return getattr(value, "value", value)


def _stored_role_requirement_id(
    revision_id: str,
    requirement_id: str | None,
    index: int,
) -> str:
    """Build the durable node id for a revision-scoped role requirement."""
    logical_id = requirement_id or f"{MISSING_REQUIREMENT_ID_PREFIX}{index}"
    return _stored_scoped_child_id(revision_id, logical_id)


def _logical_role_requirement_id(
    stored_requirement_id: str,
    logical_requirement_id: str | None,
) -> str | None:
    """Return the API-visible requirement id from a durable node row."""
    if logical_requirement_id:
        return logical_requirement_id
    tail = _logical_scoped_child_id("", stored_requirement_id, None)
    if tail.startswith(MISSING_REQUIREMENT_ID_PREFIX):
        return None
    if tail != stored_requirement_id:
        return tail
    if stored_requirement_id.startswith(MISSING_REQUIREMENT_ID_PREFIX):
        return None
    return stored_requirement_id


def _stored_scoped_child_id(parent_id: str, logical_id: str) -> str:
    """Build a table-global storage id from a parent-scoped logical id."""
    return f"{parent_id}{SCOPED_STORAGE_SEPARATOR}{logical_id}"


def _logical_scoped_child_id(
    parent_id: str,
    stored_id: str,
    logical_id: str | None,
) -> str:
    """Return a parent-scoped logical id from a storage row."""
    if logical_id:
        return logical_id
    prefix = f"{parent_id}{SCOPED_STORAGE_SEPARATOR}" if parent_id else ""
    if prefix and stored_id.startswith(prefix):
        return stored_id[len(prefix):]
    if not prefix and SCOPED_STORAGE_SEPARATOR in stored_id:
        return stored_id.rsplit(SCOPED_STORAGE_SEPARATOR, 1)[-1]
    return stored_id


def _field(item: Any, name: str) -> Any:
    """Read a field from either a dict payload or a Pydantic command item."""
    if isinstance(item, dict):
        return item[name]
    return getattr(item, name)


def _field_or_generated_id(item: Any, name: str) -> str:
    """Read an optional id field, generating one when the command omitted it."""
    if isinstance(item, dict):
        value = item.get(name)
    else:
        value = getattr(item, name)
    return value or new_id()


class _CloseMethodAdapter:
    """Expose Ladybug handles with method-style ``is_closed`` compatibility."""

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    def is_closed(self) -> bool:
        """Return whether the wrapped Ladybug handle is closed."""
        is_closed = self._wrapped.is_closed
        if callable(is_closed):
            return bool(is_closed())
        return bool(is_closed)

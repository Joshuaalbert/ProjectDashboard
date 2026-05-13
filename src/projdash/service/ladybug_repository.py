"""LadybugDB adapter boundary.

This first slice initializes the durable graph schema. Command/query persistence
will be implemented behind the same repository protocol after the service
contracts settle under tests.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from projdash.service.identifiers import new_id

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
        duration_business_days INT64,
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


class LadybugProjectRepository:
    """Thin LadybugDB connection and schema bootstrap wrapper."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._lb = _import_ladybug()
        raw_db = self._lb.Database(self.db_path)
        raw_conn = self._lb.Connection(raw_db)
        self._db = _CloseMethodAdapter(raw_db)
        self._conn = _CloseMethodAdapter(raw_conn)

    def initialize_schema(self) -> None:
        """Create graph tables and relationships if they do not exist."""
        for statement in SCHEMA_STATEMENTS:
            self._conn.execute(statement)

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
    ) -> SimpleNamespace:
        """Create and persist a project fact."""
        project_id = new_id()
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
            self._conn.execute(
                """
                CREATE (:CalendarWeeklyWindow {
                    window_id: $window_id,
                    calendar_id: $calendar_id,
                    project_id: $project_id,
                    weekday: $weekday,
                    start_local_time: $start_local_time,
                    end_local_time: $end_local_time,
                    capacity_hours: $capacity_hours
                })
                """,
                {
                    "window_id": window_id,
                    "calendar_id": calendar_id,
                    "project_id": project_id,
                    "weekday": _field(window, "weekday"),
                    "start_local_time": _field(window, "start_local_time"),
                    "end_local_time": _field(window, "end_local_time"),
                    "capacity_hours": _field(window, "capacity_hours"),
                },
            )
            self._conn.execute(
                """
                MATCH (calendar:ResourceCalendar), (window:CalendarWeeklyWindow)
                WHERE calendar.calendar_id = $calendar_id
                  AND window.window_id = $window_id
                CREATE (calendar)-[:HAS_WINDOW]->(window)
                """,
                {"calendar_id": calendar_id, "window_id": window_id},
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
        return resolved_resource_id

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


def _value_or_enum_value(value: Any) -> Any:
    """Return enum values without requiring callers to unwrap Pydantic models."""
    return getattr(value, "value", value)


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

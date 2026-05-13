import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from projdash.service.commands import CommandEnvelope
from projdash.service.ladybug_repository import (
    SCHEMA_STATEMENTS,
    LadybugProjectRepository,
)
from projdash.service.service import ProjectService

UTC_MINUS_FOUR = dt.timezone(dt.timedelta(hours=-4))
UTC_PLUS_TWO = dt.timezone(dt.timedelta(hours=2))


def _schema_sql() -> str:
    return " ".join(statement.lower() for statement in SCHEMA_STATEMENTS)


def _table_names(connection: Any) -> set[str]:
    rows = connection.execute("CALL show_tables() RETURN *").get_all()
    return {row[1] for row in rows}


def _columns(connection: Any, table_name: str) -> set[str]:
    rows = connection.execute(f"CALL table_info('{table_name}') RETURN *").get_all()
    return {row[1] for row in rows}


def _one_row(
    connection: Any,
    query: str,
    parameters: dict[str, Any] | None = None,
) -> list[Any]:
    if parameters is None:
        rows = connection.execute(query).get_all()
    else:
        rows = connection.execute(query, parameters).get_all()
    assert len(rows) == 1
    return rows[0]


def _close(repository: LadybugProjectRepository) -> None:
    if not repository._conn.is_closed():
        repository._conn.close()
    if not repository._db.is_closed():
        repository._db.close()


def _aware_iso(day: int, hour: int, tz: dt.tzinfo) -> str:
    return dt.datetime(2026, 5, day, hour, tzinfo=tz).isoformat()


def test_schema_statements_include_resource_graph_and_replay_contracts():
    sql = _schema_sql()

    for expected in [
        "default_currency",
        "processretirementevent",
        "role(",
        "resource(",
        "resourcecalendar",
        "calendarweeklywindow",
        "calendarexception",
        "rolerequirement",
        "processalias",
        "duedatehistoryevent",
        "commandreplay",
        "has_role",
        "has_resource",
        "has_calendar",
        "has_window",
        "has_exception",
        "can_fill",
        "uses_calendar",
        "requires_role",
        "requirement_role",
        "has_alias",
        "has_blocker",
        "has_due_date_event",
    ]:
        assert expected in sql

    for expected_field in [
        "cost_rate",
        "cost_unit",
        "cost_currency",
        "available_from_at",
        "available_until_at",
        "active",
        "effort_hours",
        "required_resource_count",
        "allocation_policy",
        "summary",
        "severity",
        "created_at",
        "edit_at",
        "before_due_at",
        "after_due_at",
        "is_active",
        "retired_at",
        "retired_by_command_id",
        "retirement_reason",
        "replacement_process_ids",
        "command_id",
        "payload_hash",
        "result_json",
    ]:
        assert expected_field in sql


def test_bootstrap_creates_reopenable_schema_metadata(tmp_path: Path):
    pytest.importorskip("real_ladybug")
    db_path = tmp_path / "projdash-contract.lbug"
    repository = LadybugProjectRepository(db_path)
    repository.initialize_schema()
    repository.initialize_schema()
    expected_tables = {
        "Project",
        "Process",
        "ProcessRevision",
        "ProcessRetirementEvent",
        "Role",
        "Resource",
        "ResourceCalendar",
        "CalendarWeeklyWindow",
        "CalendarException",
        "RoleRequirement",
        "ProcessAlias",
        "Blocker",
        "DueDateHistoryEvent",
        "CommandReplay",
    }
    expected_relationship_tables = {
        "HAS_PROCESS",
        "HAS_REVISION",
        "DEPENDS_ON",
        "BLOCKS",
        "HAS_ROLE",
        "HAS_RESOURCE",
        "HAS_CALENDAR",
        "HAS_WINDOW",
        "HAS_EXCEPTION",
        "CAN_FILL",
        "USES_CALENDAR",
        "REQUIRES_ROLE",
        "REQUIREMENT_ROLE",
        "HAS_ALIAS",
        "HAS_BLOCKER",
        "HAS_DUE_DATE_EVENT",
    }
    assert expected_tables <= _table_names(repository._conn)
    assert expected_relationship_tables <= _table_names(repository._conn)
    assert {
        "project_id",
        "name",
        "start_at",
        "default_currency",
    } <= _columns(repository._conn, "Project")
    assert {
        "process_id",
        "project_id",
        "symbol",
        "status",
        "finished_at",
        "is_active",
        "retired_at",
        "retired_by_command_id",
        "retirement_reason",
    } <= _columns(repository._conn, "Process")
    assert {
        "resource_id",
        "project_id",
        "calendar_id",
        "available_from_at",
        "available_until_at",
        "cost_rate",
        "cost_unit",
        "cost_currency",
        "active",
    } <= _columns(repository._conn, "Resource")
    _close(repository)

    reopened = LadybugProjectRepository(db_path)
    assert expected_tables <= _table_names(reopened._conn)
    assert expected_relationship_tables <= _table_names(reopened._conn)
    _close(reopened)


def test_timezone_offsets_and_cost_fields_round_trip(tmp_path: Path):
    pytest.importorskip("real_ladybug")
    db_path = tmp_path / "offset-round-trip.lbug"
    repository = LadybugProjectRepository(db_path)
    repository.initialize_schema()
    conn = repository._conn

    project_start = _aware_iso(13, 9, UTC_MINUS_FOUR)
    resource_start = _aware_iso(13, 9, UTC_PLUS_TWO)
    due_before = _aware_iso(20, 17, UTC_MINUS_FOUR)
    due_after = _aware_iso(21, 17, UTC_MINUS_FOUR)
    retired_at = _aware_iso(14, 12, UTC_MINUS_FOUR)
    conn.execute(
        """
        CREATE (:Project {
            project_id: 'project-alpha',
            name: 'Alpha',
            start_at: $project_start,
            default_currency: 'USD'
        })
        """,
        {"project_start": project_start},
    )
    conn.execute(
        """
        CREATE (:Process {
            process_id: 'process-build',
            project_id: 'project-alpha',
            symbol: 'build',
            status: 'planned',
            finished_at: NULL,
            is_active: true,
            retired_at: NULL,
            retired_by_command_id: NULL,
            retirement_reason: NULL
        })
        """
    )
    conn.execute(
        """
        CREATE (:ProcessRevision {
            revision_id: 'revision-build-1',
            process_id: 'process-build',
            project_id: 'project-alpha',
            effective_at: $project_start,
            name: 'Build',
            duration_business_days: 1,
            due_at: $due_before,
            earliest_start_at: NULL,
            start_at_earliest: false,
            delay_after_dependencies_business_days: 0,
            assumption_note: NULL
        })
        """,
        {"project_start": project_start, "due_before": due_before},
    )
    conn.execute(
        """
        CREATE (:Role {
            role_id: 'role-engineer',
            project_id: 'project-alpha',
            name: 'Engineer',
            active: true
        })
        """
    )
    conn.execute(
        """
        CREATE (:ResourceCalendar {
            calendar_id: 'calendar-nyc',
            project_id: 'project-alpha',
            name: 'NYC',
            timezone: 'America/New_York',
            active: true
        })
        """
    )
    conn.execute(
        """
        CREATE (:Resource {
            resource_id: 'resource-ada',
            project_id: 'project-alpha',
            name: 'Ada',
            calendar_id: 'calendar-nyc',
            available_from_at: $resource_start,
            available_until_at: NULL,
            cost_rate: '125.50',
            cost_unit: 'hour',
            cost_currency: 'USD',
            active: true
        })
        """,
        {"resource_start": resource_start},
    )
    conn.execute(
        """
        CREATE (:RoleRequirement {
            requirement_id: 'req-build-engineer',
            revision_id: 'revision-build-1',
            project_id: 'project-alpha',
            process_id: 'process-build',
            role_id: 'role-engineer',
            effort_hours: 8,
            min_allocation_hours_per_day: NULL,
            max_allocation_hours_per_day: NULL,
            required_resource_count: 1,
            allocation_policy: 'split_allowed'
        })
        """
    )
    conn.execute(
        """
        CREATE (:ProcessAlias {
            alias_id: 'alias-build-api',
            project_id: 'project-alpha',
            process_id: 'process-build',
            alias: 'build-api',
            created_at: $project_start
        })
        """,
        {"project_start": project_start},
    )
    conn.execute(
        """
        CREATE (:Blocker {
            blocker_id: 'blocker-security',
            project_id: 'project-alpha',
            process_id: 'process-build',
            summary: 'Security review',
            details: 'External review pending',
            severity: 'blocking',
            created_at: $project_start,
            resolved_at: $due_after,
            resolution: 'Approved'
        })
        """,
        {"project_start": project_start, "due_after": due_after},
    )
    conn.execute(
        """
        CREATE (:DueDateHistoryEvent {
            event_id: 'due-event-build',
            project_id: 'project-alpha',
            process_id: 'process-build',
            mutation_action: 'set_process_due_at',
            edit_at: $retired_at,
            before_due_at: $due_before,
            after_due_at: $due_after,
            command_id: '00000000-0000-4000-8000-000000000501'
        })
        """,
        {
            "retired_at": retired_at,
            "due_before": due_before,
            "due_after": due_after,
        },
    )
    conn.execute(
        """
        CREATE (:ProcessRetirementEvent {
            retirement_event_id: 'retire-build',
            project_id: 'project-alpha',
            process_id: 'process-build',
            retired_at: $retired_at,
            retired_by_command_id: '00000000-0000-4000-8000-000000000502',
            retirement_reason: 'replace_process_with_subgraph',
            replacement_process_ids: ['process-api', 'process-ui']
        })
        """,
        {"retired_at": retired_at},
    )
    conn.execute(
        """
        CREATE (:CommandReplay {
            command_id: '00000000-0000-4000-8000-000000000503',
            payload_hash: 'sha256:abc',
            result_json: '{"ok": true}',
            applied_at: $project_start
        })
        """,
        {"project_start": project_start},
    )
    conn.execute(
        """
        MATCH (revision:ProcessRevision), (requirement:RoleRequirement)
        WHERE revision.revision_id = 'revision-build-1'
          AND requirement.requirement_id = 'req-build-engineer'
        CREATE (revision)-[:REQUIRES_ROLE]->(requirement)
        """
    )
    conn.execute(
        """
        MATCH (requirement:RoleRequirement), (role:Role)
        WHERE requirement.requirement_id = 'req-build-engineer'
          AND role.role_id = 'role-engineer'
        CREATE (requirement)-[:REQUIREMENT_ROLE]->(role)
        """
    )
    _close(repository)

    reopened = LadybugProjectRepository(db_path)
    conn = reopened._conn
    assert _one_row(
        conn,
        """
        MATCH (project:Project)
        WHERE project.project_id = 'project-alpha'
        RETURN project.start_at, project.default_currency
        """,
    ) == [project_start, "USD"]
    assert _one_row(
        conn,
        """
        MATCH (resource:Resource)
        WHERE resource.resource_id = 'resource-ada'
        RETURN resource.available_from_at, resource.cost_rate,
               resource.cost_unit, resource.cost_currency
        """,
    ) == [resource_start, "125.50", "hour", "USD"]
    assert _one_row(
        conn,
        """
        MATCH (blocker:Blocker)
        WHERE blocker.blocker_id = 'blocker-security'
        RETURN blocker.created_at, blocker.resolved_at, blocker.severity
        """,
    ) == [project_start, due_after, "blocking"]
    assert _one_row(
        conn,
        """
        MATCH (event:DueDateHistoryEvent)
        WHERE event.event_id = 'due-event-build'
        RETURN event.edit_at, event.before_due_at, event.after_due_at
        """,
    ) == [retired_at, due_before, due_after]
    assert _one_row(
        conn,
        """
        MATCH (event:ProcessRetirementEvent)
        WHERE event.retirement_event_id = 'retire-build'
        RETURN event.retired_at, event.retired_by_command_id,
               event.retirement_reason, event.replacement_process_ids
        """,
    ) == [
        retired_at,
        "00000000-0000-4000-8000-000000000502",
        "replace_process_with_subgraph",
        ["process-api", "process-ui"],
    ]
    assert _one_row(
        conn,
        """
        MATCH (revision:ProcessRevision)-[:REQUIRES_ROLE]->
              (requirement:RoleRequirement)-[:REQUIREMENT_ROLE]->(role:Role)
        WHERE revision.revision_id = 'revision-build-1'
        RETURN requirement.requirement_id, requirement.revision_id,
               requirement.process_id, role.role_id
        """,
    ) == [
        "req-build-engineer",
        "revision-build-1",
        "process-build",
        "role-engineer",
    ]
    assert _one_row(
        conn,
        """
        MATCH (replay:CommandReplay)
        WHERE replay.command_id = '00000000-0000-4000-8000-000000000503'
        RETURN replay.payload_hash, replay.result_json, replay.applied_at
        """,
    ) == ["sha256:abc", '{"ok": true}', project_start]
    _close(reopened)


def test_role_requirements_belong_to_revisions_not_processes(tmp_path: Path):
    pytest.importorskip("real_ladybug")
    db_path = tmp_path / "revision-requirements.lbug"
    repository = LadybugProjectRepository(db_path)
    repository.initialize_schema()
    conn = repository._conn
    assert "REQUIRES_ROLE" in _table_names(conn)
    requires_columns = _columns(conn, "REQUIRES_ROLE")
    assert "from" not in {column.lower() for column in requires_columns}
    conn.execute(
        """
        CREATE (:Project {
            project_id: 'project-alpha',
            name: 'Alpha',
            start_at: '2026-05-13T09:00:00-04:00',
            default_currency: 'USD'
        })
        """
    )
    conn.execute(
        """
        CREATE (:Process {
            process_id: 'process-build',
            project_id: 'project-alpha',
            symbol: 'build',
            status: 'planned',
            finished_at: NULL,
            is_active: true,
            retired_at: NULL,
            retired_by_command_id: NULL,
            retirement_reason: NULL
        })
        """
    )
    for revision_id, requirement_id, effort_hours in [
        ("revision-build-1", "req-build-v1", 4),
        ("revision-build-2", "req-build-v2", 8),
    ]:
        conn.execute(
            """
            CREATE (:ProcessRevision {
                revision_id: $revision_id,
                process_id: 'process-build',
                project_id: 'project-alpha',
                effective_at: '2026-05-13T09:00:00-04:00',
                name: 'Build',
                duration_business_days: 1,
                due_at: NULL,
                earliest_start_at: NULL,
                start_at_earliest: false,
                delay_after_dependencies_business_days: 0,
                assumption_note: NULL
            })
            """,
            {"revision_id": revision_id},
        )
        conn.execute(
            """
            CREATE (:RoleRequirement {
                requirement_id: $requirement_id,
                revision_id: $revision_id,
                project_id: 'project-alpha',
                process_id: 'process-build',
                role_id: 'role-engineer',
                effort_hours: $effort_hours,
                min_allocation_hours_per_day: NULL,
                max_allocation_hours_per_day: NULL,
                required_resource_count: 1,
                allocation_policy: 'split_allowed'
            })
            """,
            {
                "revision_id": revision_id,
                "requirement_id": requirement_id,
                "effort_hours": effort_hours,
            },
        )
        conn.execute(
            """
            MATCH (revision:ProcessRevision), (requirement:RoleRequirement)
            WHERE revision.revision_id = $revision_id
              AND requirement.requirement_id = $requirement_id
            CREATE (revision)-[:REQUIRES_ROLE]->(requirement)
            """,
            {"revision_id": revision_id, "requirement_id": requirement_id},
        )

    rows = conn.execute(
        """
        MATCH (revision:ProcessRevision)-[:REQUIRES_ROLE]->
              (requirement:RoleRequirement)
        RETURN revision.revision_id, requirement.requirement_id,
               requirement.effort_hours
        ORDER BY revision.revision_id
        """
    ).get_all()
    assert rows == [
        ["revision-build-1", "req-build-v1", 4],
        ["revision-build-2", "req-build-v2", 8],
    ]
    _close(repository)


def test_soft_retired_processes_and_edges_project_active_as_of(tmp_path: Path):
    pytest.importorskip("real_ladybug")
    db_path = tmp_path / "active-as-of.lbug"
    repository = LadybugProjectRepository(db_path)
    repository.initialize_schema()
    conn = repository._conn
    retired_at = _aware_iso(14, 12, UTC_MINUS_FOUR)
    conn.execute(
        """
        CREATE (:Project {
            project_id: 'project-alpha',
            name: 'Alpha',
            start_at: '2026-05-13T09:00:00-04:00',
            default_currency: 'USD'
        })
        """
    )
    for process_id, symbol, is_active, process_retired_at in [
        ("process-legacy", "legacy-api", False, retired_at),
        ("process-api", "api-implementation", True, None),
        ("process-ship", "ship", True, None),
    ]:
        conn.execute(
            """
            CREATE (:Process {
                process_id: $process_id,
                project_id: 'project-alpha',
                symbol: $symbol,
                status: 'planned',
                finished_at: NULL,
                is_active: $is_active,
                retired_at: $retired_at,
                retired_by_command_id: '00000000-0000-4000-8000-000000000601',
                retirement_reason: 'replace_process_with_subgraph'
            })
            """,
            {
                "process_id": process_id,
                "symbol": symbol,
                "is_active": is_active,
                "retired_at": process_retired_at,
            },
        )
    conn.execute(
        """
        MATCH (legacy:Process), (ship:Process)
        WHERE legacy.process_id = 'process-legacy'
          AND ship.process_id = 'process-ship'
        CREATE (legacy)-[:DEPENDS_ON {
            edge_id: 'edge-legacy-ship',
            project_id: 'project-alpha',
            retired_at: $retired_at,
            retired_by_command_id: '00000000-0000-4000-8000-000000000601',
            retirement_reason: 'replace_process_with_subgraph'
        }]->(ship)
        """,
        {"retired_at": retired_at},
    )
    conn.execute(
        """
        MATCH (api:Process), (ship:Process)
        WHERE api.process_id = 'process-api'
          AND ship.process_id = 'process-ship'
        CREATE (api)-[:DEPENDS_ON {
            edge_id: 'edge-api-ship',
            project_id: 'project-alpha',
            retired_at: NULL,
            retired_by_command_id: NULL,
            retirement_reason: NULL
        }]->(ship)
        """
    )

    active_before_retirement = conn.execute(
        """
        MATCH (process:Process)
        WHERE process.project_id = 'project-alpha'
          AND (process.retired_at IS NULL OR process.retired_at > $as_of)
        RETURN process.symbol
        ORDER BY process.symbol
        """,
        {"as_of": _aware_iso(14, 11, UTC_MINUS_FOUR)},
    ).get_all()
    active_after_retirement = conn.execute(
        """
        MATCH (process:Process)
        WHERE process.project_id = 'project-alpha'
          AND (process.retired_at IS NULL OR process.retired_at > $as_of)
        RETURN process.symbol
        ORDER BY process.symbol
        """,
        {"as_of": _aware_iso(14, 13, UTC_MINUS_FOUR)},
    ).get_all()
    edges_before_retirement = conn.execute(
        """
        MATCH (:Process)-[edge:DEPENDS_ON]->(:Process)
        WHERE edge.project_id = 'project-alpha'
          AND (edge.retired_at IS NULL OR edge.retired_at > $as_of)
        RETURN edge.edge_id
        ORDER BY edge.edge_id
        """,
        {"as_of": _aware_iso(14, 11, UTC_MINUS_FOUR)},
    ).get_all()
    edges_after_retirement = conn.execute(
        """
        MATCH (:Process)-[edge:DEPENDS_ON]->(:Process)
        WHERE edge.project_id = 'project-alpha'
          AND (edge.retired_at IS NULL OR edge.retired_at > $as_of)
        RETURN edge.edge_id
        ORDER BY edge.edge_id
        """,
        {"as_of": _aware_iso(14, 13, UTC_MINUS_FOUR)},
    ).get_all()

    assert ["legacy-api"] in active_before_retirement
    assert ["legacy-api"] not in active_after_retirement
    assert ["edge-legacy-ship"] in edges_before_retirement
    assert ["edge-legacy-ship"] not in edges_after_retirement
    assert ["edge-api-ship"] in edges_after_retirement
    _close(repository)


def test_process_aliases_resolve_only_unique_active_as_of_target(tmp_path: Path):
    pytest.importorskip("real_ladybug")
    db_path = tmp_path / "alias-resolution.lbug"
    repository = LadybugProjectRepository(db_path)
    repository.initialize_schema()
    conn = repository._conn
    retired_at = _aware_iso(14, 12, UTC_MINUS_FOUR)
    conn.execute(
        """
        CREATE (:Project {
            project_id: 'project-alpha',
            name: 'Alpha',
            start_at: '2026-05-13T09:00:00-04:00',
            default_currency: 'USD'
        })
        """
    )
    for process_id, symbol, is_active, process_retired_at in [
        ("process-legacy", "legacy-api", False, retired_at),
        ("process-api", "api-implementation", True, None),
        ("process-ship", "ship", True, None),
    ]:
        conn.execute(
            """
            CREATE (:Process {
                process_id: $process_id,
                project_id: 'project-alpha',
                symbol: $symbol,
                status: 'planned',
                finished_at: NULL,
                is_active: $is_active,
                retired_at: $retired_at,
                retired_by_command_id: '00000000-0000-4000-8000-000000000602',
                retirement_reason: 'replace_process_with_subgraph'
            })
            """,
            {
                "process_id": process_id,
                "symbol": symbol,
                "is_active": is_active,
                "retired_at": process_retired_at,
            },
        )
    for alias_id, process_id, alias in [
        ("alias-legacy-api", "process-legacy", "api"),
        ("alias-active-api", "process-api", "api"),
        ("alias-service-api", "process-api", "service-api"),
    ]:
        conn.execute(
            """
            CREATE (:ProcessAlias {
                alias_id: $alias_id,
                project_id: 'project-alpha',
                process_id: $process_id,
                alias: $alias,
                created_at: '2026-05-13T09:00:00-04:00'
            })
            """,
            {"alias_id": alias_id, "process_id": process_id, "alias": alias},
        )

    duplicate_active_aliases = conn.execute(
        """
        MATCH (process:Process), (alias:ProcessAlias)
        WHERE process.project_id = 'project-alpha'
          AND process.process_id = alias.process_id
          AND (process.retired_at IS NULL OR process.retired_at > $as_of)
        WITH alias.alias AS alias, count(process.process_id) AS active_count
        WHERE active_count > 1
        RETURN alias
        """,
        {"as_of": _aware_iso(14, 13, UTC_MINUS_FOUR)},
    ).get_all()
    resolved_api = conn.execute(
        """
        MATCH (process:Process), (alias:ProcessAlias)
        WHERE alias.project_id = 'project-alpha'
          AND process.process_id = alias.process_id
          AND alias.alias = 'api'
          AND (process.retired_at IS NULL OR process.retired_at > $as_of)
        RETURN process.process_id, process.symbol
        """,
        {"as_of": _aware_iso(14, 13, UTC_MINUS_FOUR)},
    ).get_all()
    resolved_service_api = conn.execute(
        """
        MATCH (process:Process), (alias:ProcessAlias)
        WHERE alias.project_id = 'project-alpha'
          AND process.process_id = alias.process_id
          AND alias.alias = 'service-api'
          AND (process.retired_at IS NULL OR process.retired_at > $as_of)
        RETURN process.process_id, process.symbol
        """,
        {"as_of": _aware_iso(14, 13, UTC_MINUS_FOUR)},
    ).get_all()

    assert duplicate_active_aliases == []
    assert resolved_api == [["process-api", "api-implementation"]]
    assert resolved_service_api == [["process-api", "api-implementation"]]
    _close(repository)


def test_ladybug_service_command_fails_closed_without_transactional_staging(
    tmp_path: Path,
):
    pytest.importorskip("real_ladybug")
    db_path = tmp_path / "fail-closed-command.lbug"
    repository = LadybugProjectRepository(db_path)
    repository.initialize_schema()
    service = ProjectService(repository)

    result = service.handle_command(
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "create_project",
                    "name": "Fail Closed",
                    "start_at": _aware_iso(13, 9, UTC_MINUS_FOUR),
                }
            }
        )
    )

    assert result.ok is False
    assert result.error.code == "transaction_required"
    assert repository._conn.execute(
        """
        MATCH (project:Project)
        RETURN count(project.project_id)
        """
    ).get_all() == [[0]]
    _close(repository)

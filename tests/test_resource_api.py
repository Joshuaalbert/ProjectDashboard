import copy
import datetime as dt
import uuid
from collections.abc import Mapping

import pytest
from pydantic import ValidationError

from projdash.service.commands import CommandEnvelope, CreateProject
from projdash.service.queries import QueryEnvelope
from projdash.service.repository import InMemoryProjectRepository
from projdash.service.service import ProjectService

UTC = dt.UTC


def _at(day: int, hour: int = 9) -> dt.datetime:
    return dt.datetime(2026, 5, day, hour, tzinfo=UTC)


def _iso(day: int, hour: int = 9) -> str:
    return _at(day, hour).isoformat()


def _handle(
    service: ProjectService,
    command: Mapping[str, object],
    *,
    command_id: str | None = None,
):
    payload: dict[str, object] = {"command": command}
    if command_id is not None:
        payload["command_id"] = command_id
    return service.handle_command(CommandEnvelope.model_validate(payload))


def _query(service: ProjectService, query: Mapping[str, object]):
    return service.handle_query(QueryEnvelope.model_validate({"query": query}))


def _create_project(
    service: ProjectService,
    name: str = "Resource Project",
) -> str:
    return service.handle_command(
        CommandEnvelope(command=CreateProject(name=name, start_at=_at(13)))
    ).entity_ids["project_id"]


def _weekday_windows() -> list[dict[str, object]]:
    return [
        {
            "window_id": f"weekday-{weekday}",
            "weekday": weekday,
            "start_local_time": "09:00",
            "end_local_time": "17:00",
            "capacity_hours": 8,
        }
        for weekday in range(5)
    ]


def _seed_allocatable_project(
    service: ProjectService,
) -> tuple[str, str, str, str, str]:
    project_id = _create_project(service)
    role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    ).entity_ids["role_id"]
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-nyc",
            "name": "New York Weekdays",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    resource_id = _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": "resource-ada",
            "name": "Ada",
            "role_ids": [role_id],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13, 13),
            "cost_rate": "125.00",
            "cost_unit": "hour",
        },
    ).entity_ids["resource_id"]
    process_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-api",
            "name": "Build API",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-api-eng",
                    "role_id": role_id,
                    "effort_hours": 8,
                }
            ],
        },
    ).entity_ids["process_id"]
    return project_id, role_id, calendar_id, resource_id, process_id


def _resource_schedule_query(
    project_id: str,
    *,
    include_allocation_slices: bool = True,
) -> dict[str, object]:
    return {
        "action": "query_resource_schedule",
        "project_id": project_id,
        "as_of": _iso(13, 12),
        "now": _iso(13, 12),
        "include_allocation_slices": include_allocation_slices,
    }


def _assert_no_nested_warnings(data: Mapping[str, object]) -> None:
    assert "warnings" not in data
    assert "cost_warnings" not in data
    assert "resource_warnings" not in data


def _assert_command_error_result(result, code: str) -> None:
    assert result.ok is False
    assert result.error.code == code
    assert result.warnings == []
    dumped = result.model_dump(mode="json")
    assert set(dumped) == {"command_id", "ok", "error", "warnings"}
    assert dumped["error"]["code"] == code


def _assert_failed_command_result(result) -> None:
    assert result.ok is False
    assert result.error.code
    assert result.error.message
    assert result.warnings == []
    dumped = result.model_dump(mode="json")
    assert set(dumped) == {"command_id", "ok", "error", "warnings"}
    assert dumped["ok"] is False
    assert dumped["error"]["code"] == result.error.code


def _repository_snapshot(repository: InMemoryProjectRepository):
    return copy.deepcopy(repository.__dict__)


def _dump_record(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _dump_record(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_dump_record(item) for item in value]
    return copy.deepcopy(value)


def _find_record_by_id(
    repository: InMemoryProjectRepository,
    id_field: str,
    entity_id: str,
) -> dict[str, object]:
    pending = list(repository.__dict__.values())
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        value_id = id(value)
        if value_id in seen:
            continue
        seen.add(value_id)
        dumped = _dump_record(value)
        if isinstance(dumped, dict) and dumped.get(id_field) == entity_id:
            return dumped
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list | tuple | set):
            pending.extend(value)
    raise AssertionError(f"Could not find record with {id_field}={entity_id!r}.")


def _calendar_windows(
    repository: InMemoryProjectRepository,
    calendar_id: str,
) -> list[dict[str, object]]:
    calendar = _find_record_by_id(repository, "calendar_id", calendar_id)
    return calendar["weekly_windows"]


def _calendar_exceptions(
    repository: InMemoryProjectRepository,
    calendar_id: str,
) -> list[dict[str, object]]:
    calendar = _find_record_by_id(repository, "calendar_id", calendar_id)
    return calendar.get("exceptions", [])


def test_agent_context_query_returns_concise_project_management_json():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    ship_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-ship",
            "name": "Ship API",
            "description": "Release the API deliverable.",
            "effective_at": _iso(13),
            "dependencies": [process_id],
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-ship-eng",
                    "role_id": role_id,
                    "effort_hours": 2,
                }
            ],
        },
    ).entity_ids["process_id"]
    _handle(
        service,
        {
            "action": "add_blocker",
            "project_id": project_id,
            "process_id": ship_id,
            "summary": "Awaiting release approval",
            "severity": "blocking",
            "resolution_owner_resource_id": resource_id,
            "created_at": _iso(13, 12),
        },
    )
    _handle(
        service,
        {
            "action": "commit_project_state",
            "project_id": project_id,
            "committed_at": _iso(13, 12),
            "note": "Initial context baseline",
        },
    )

    result = _query(
        service,
        {
            "action": "query_agent_context",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "now": _iso(13, 14),
        },
    )

    assert result.ok is True
    assert result.warnings == []
    data = result.data
    assert data["context_version"] == 1
    assert data["project"]["project_id"] == project_id
    assert data["summary"]["process_count"] == 3
    assert data["summary"]["edge_count"] == 2
    assert data["summary"]["blocked_process_count"] == 1
    assert data["summary"]["total_role_effort_hours"] == 11
    assert data["schedule"]["basis"] == "resource_aware"
    assert "critical_path" not in data["schedule"]
    assert data["schedule"]["processes"]
    assert data["slippage"]["snapshot_count"] == 1
    assert data["slippage"]["latest"]["note"] == "Initial context baseline"
    assert data["slippage"]["timeline"] == [
        {
            "commit_datetime": _iso(13, 12),
            "estimated_done_datetime": data["slippage"]["latest"]["completion_at"],
        }
    ]
    assert data["blockers"][0]["summary"] == "Awaiting release approval"
    assert data["blockers"][0]["resolution_owner_resource_id"] == resource_id
    assert data["blockers"][0]["needed_by_role_ids"] == [role_id]
    assert data["blockers"][0]["needed_by_resource_ids"] == [resource_id]
    assert data["blockers"][0]["immediate_blocked_processes"][0][
        "process_symbol"
    ] == "process-ship"
    assert "query_resource_schedule" in data["available_queries"]

    nodes = {node["symbol"]: node for node in data["graph"]["nodes"]}
    assert nodes["process-api"]["successors"] == ["process-ship"]
    assert nodes["process-ship"]["predecessors"] == [
        "process-api",
        "resolve-awaiting-release-approval",
    ]
    assert nodes["resolve-awaiting-release-approval"]["process_type"] == "blocker"
    assert nodes["resolve-awaiting-release-approval"]["successors"] == ["process-ship"]
    assert nodes["process-ship"]["role_requirements"] == [
            {
                "requirement_id": "req-ship-eng",
                "role_id": role_id,
                "effort_hours": 2,
                "pin_status": "planned",
                "active_pinned_resource_ids": [],
                "recent_pinned_resource_ids": [],
                "pins": [],
                "required_resource_count": 1,
                "allocation_policy": "split_allowed",
                "min_allocation_hours_per_day": None,
                "max_allocation_hours_per_day": None,
            }
    ]

    priority_by_role = {
        row["role_id"]: row["processes"]
        for row in data["prioritized_work"]["by_role"]
    }
    assert priority_by_role[role_id][0]["process_symbol"] == "process-api"
    assert priority_by_role[role_id][0]["priority"] == "P0"
    assert priority_by_role[role_id][0]["effort_hours"] == 8
    assert "hours_until_planned_start" in priority_by_role[role_id][0]

    priority_by_resource = {
        row["resource_id"]: row["processes"]
        for row in data["prioritized_work"]["by_resource"]
    }
    assert priority_by_resource[resource_id][0]["process_symbol"] == "process-api"
    assert priority_by_resource[resource_id][0]["effort_hours"] == 8
    assert priority_by_resource[resource_id][0]["role_ids"] == [role_id]


def test_agent_context_marks_process_starting_within_seven_days_as_p1():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, _resource_id, _process_id = (
        _seed_allocatable_project(service)
    )
    _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-five-days-out",
            "name": "Five days out",
            "effective_at": _iso(13),
            "earliest_start_at": _iso(18, 12),
            "start_at_earliest": True,
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-five-days-out-eng",
                    "role_id": role_id,
                    "effort_hours": 1,
                }
            ],
        },
    )

    result = _query(
        service,
        {
            "action": "query_agent_context",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
        },
    )

    assert result.ok is True
    role_rows = {
        process["process_symbol"]: process
        for group in result.data["prioritized_work"]["by_role"]
        if group["role_id"] == role_id
        for process in group["processes"]
    }
    assert role_rows["process-five-days-out"]["priority"] == "P1"


def test_pm_markdown_context_includes_process_evidence_line_items():
    service = ProjectService(InMemoryProjectRepository())
    project_id, _role_id, _calendar_id, resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    _handle(
        service,
        {
            "action": "add_blocker",
            "project_id": project_id,
            "process_id": process_id,
            "blocker_id": "blocker-api-token",
            "summary": "Awaiting API token",
            "severity": "blocking",
            "resolution_owner_resource_id": resource_id,
            "created_at": _iso(13, 20),
        },
    )
    evidence_result = _handle(
        service,
        {
            "action": "upsert_process_evidence_line_item",
            "project_id": project_id,
            "process_symbol": "process-api",
            "line_item": "blockers",
            "last_evidence_at": _iso(13, 21),
            "evidence_note": "Ada confirmed the token is still blocked.",
            "evidence_source": "slack:D1:123",
            "updated_at": _iso(13, 21),
        },
    )

    line_items = _query(
        service,
        {
            "action": "query_process_evidence_line_items",
            "project_id": project_id,
            "process_id": process_id,
        },
    ).data["line_items"]
    assert line_items == [
        {
            "evidence_line_id": evidence_result.entity_ids["evidence_line_id"],
            "project_id": project_id,
                "process_id": process_id,
                "process_symbol": "process-api",
                "line_item": "blockers",
                "last_modified_at": _iso(13, 20),
                "last_evidence_at": _iso(13, 21),
                "evidence_note": "Ada confirmed the token is still blocked.",
                "evidence_source": "slack:D1:123",
                "created_at": _iso(13, 21),
                "updated_at": _iso(13, 21),
            }
        ]

    result = _query(
        service,
        {
            "action": "query_pm_markdown_context",
            "project_id": project_id,
            "as_of": _iso(13, 22),
            "now": _iso(13, 22),
        },
    )

    assert result.ok is True
    data = result.data
    evidence_by_item = {
        item["line_item"]: item
        for item in data["evidence_line_items"]
        if item.get("process_symbol") == "process-api"
    }
    assert evidence_by_item["blockers"]["verification_state"] == "current"
    assert evidence_by_item["blockers"]["evidence_source"] == "slack:D1:123"
    assert evidence_by_item["blockers"]["process_priority"] == "P1"
    assert evidence_by_item["blockers"]["target_evidence_age_days"] == 3
    assert evidence_by_item["blockers"]["target_evidence_age_label"] == (
        "< 3 days old"
    )
    assert evidence_by_item["blockers"]["evidence_age_exceeds_target"] is False
    assert evidence_by_item["blockers"]["evidence_is_stale"] is False
    assert evidence_by_item["done_criteria"]["verification_state"] == "needs_evidence"
    assert evidence_by_item["done_criteria"]["evidence_is_stale"] is True
    assert evidence_by_item["done_criteria"]["last_modified_at"] != (
        evidence_by_item["blockers"]["last_modified_at"]
    )
    markdown = data["markdown"]
    assert "# Dependencies" not in markdown
    assert "# Milestones" in markdown
    assert "# Processes" in markdown
    assert "# Continuity note" in markdown
    assert "# Evidence" in markdown
    assert "## Process Evidence" in markdown
    assert "## Resource Evidence" in markdown
    assert "## P" in markdown
    assert "| process-api | Build API" in markdown
    assert "Type: normal" in markdown
    assert "Mode: planned" in markdown
    assert "Planned Start: - | Planned Finish: -" not in markdown
    assert "Planned " + "assignments:" not in markdown
    assert "Role requirement: Engineer (role-engineer) | req-api-eng" in markdown
    assert "Assigned to: Ada (resource-ada)" in markdown
    assert "Planned start:" in markdown and "(in " in markdown
    assert "| resolve-api-token | Resolve: Awaiting API token" in markdown
    assert "Type: blocker" in markdown
    assert "Blockers:\n" not in markdown
    assert "Effort hours:" in markdown
    assert "Remaining " + "planning effort" not in markdown
    assert "Sp" + "ent" not in markdown
    assert "`pin_data`" in markdown
    assert (
        "Line items with '*' are considered to have stale evidence and should "
        "be prioritised."
    ) in markdown
    assert (
        "Staleness targets: P0 < 1 day, P1 < 3 days, P2 < 7 days, P3 < 14 days"
    ) in markdown
    assert "where P0=planned with planned start in the past or pinned" in markdown
    assert (
        "process-api.blockers last modified 0.08 days ago, "
        "last evidence that it's correct 0.04 days ago."
    ) in markdown
    assert (
        "*process-api.done_criteria last modified "
    ) in markdown


def test_blocker_update_does_not_refresh_other_process_evidence_attributes():
    service = ProjectService(InMemoryProjectRepository())
    project_id, _role_id, _calendar_id, resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    for line_item in (
        "done_criteria",
        "plan_data",
        "pin_data",
    ):
        result = _handle(
            service,
            {
                "action": "upsert_process_evidence_line_item",
                "project_id": project_id,
                "process_id": process_id,
                "line_item": line_item,
                "last_evidence_at": _iso(13, 10),
                "evidence_note": f"{line_item} confirmed before blocker update.",
                "updated_at": _iso(13, 10),
            },
        )
        assert result.ok is True

    _handle(
        service,
        {
            "action": "add_blocker",
            "project_id": project_id,
            "process_id": process_id,
            "blocker_id": "blocker-api-token",
            "summary": "Awaiting API token",
            "severity": "blocking",
            "resolution_owner_resource_id": resource_id,
            "created_at": _iso(13, 20),
        },
    )

    result = _query(
        service,
        {
            "action": "query_pm_markdown_context",
            "project_id": project_id,
            "as_of": _iso(13, 22),
            "now": _iso(13, 22),
        },
    )

    evidence_by_item = {
        item["line_item"]: item
        for item in result.data["evidence_line_items"]
        if item.get("process_symbol") == "process-api"
    }
    assert evidence_by_item["blockers"]["last_modified_at"] == _iso(13, 20)
    assert evidence_by_item["blockers"]["evidence_is_stale"] is True
    assert evidence_by_item["plan_data"]["last_modified_at"] == _iso(13, 20)
    assert evidence_by_item["plan_data"]["evidence_is_stale"] is True
    for line_item in (
        "done_criteria",
        "pin_data",
    ):
        assert evidence_by_item[line_item]["last_modified_at"] < _iso(13, 20)
        assert evidence_by_item[line_item]["verification_state"] == "current"
        assert evidence_by_item[line_item]["evidence_is_stale"] is False


def test_pm_evidence_freshness_targets_follow_process_priority():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, _resource_id, _process_id = (
        _seed_allocatable_project(service)
    )
    for process_id, earliest_start_at, effort_hours in (
        ("process-past", _iso(13, 9), 0),
        ("process-two-days-out", _iso(15, 14), 1),
        ("process-five-days-out", _iso(18, 12), 1),
        ("process-later", _iso(30, 12), 1),
    ):
        _handle(
            service,
            {
                "action": "upsert_process_revision",
                "project_id": project_id,
                "process_id": process_id,
                "name": process_id.replace("-", " ").title(),
                "effective_at": _iso(13),
                "earliest_start_at": earliest_start_at,
                "start_at_earliest": True,
                "duration_business_days": 1,
                "role_requirements": (
                    []
                    if effort_hours == 0
                    else [
                        {
                            "requirement_id": f"req-{process_id}-eng",
                            "role_id": role_id,
                            "effort_hours": effort_hours,
                        }
                    ]
                ),
            },
        )
    _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-api-eng",
            "process_id": "process-api",
            "requirement_id": "req-api-eng",
            "role_id": role_id,
            "resource_id": "resource-ada",
            "pinned_at": _iso(13, 13),
            "forecast_finish_at": _iso(15, 13),
            "updated_at": _iso(13, 13),
        },
    )

    result = _query(
        service,
        {
            "action": "query_pm_markdown_context",
            "project_id": project_id,
            "as_of": _iso(14, 10),
            "now": _iso(14, 10),
        },
    )

    plan_data_rows = {
        item["process_symbol"]: item
        for item in result.data["evidence_line_items"]
        if item["line_item"] == "plan_data"
    }
    assert plan_data_rows["process-past"]["process_priority"] == "P0"
    assert plan_data_rows["process-past"]["target_evidence_age_days"] == 1
    assert plan_data_rows["process-api"]["process_priority"] == "P0"
    assert plan_data_rows["process-api"]["target_evidence_age_days"] == 1
    assert plan_data_rows["process-two-days-out"]["process_priority"] == "P1"
    assert plan_data_rows["process-two-days-out"]["target_evidence_age_days"] == 3
    assert plan_data_rows["process-five-days-out"]["process_priority"] == "P2"
    assert plan_data_rows["process-five-days-out"][
        "target_evidence_age_days"
    ] == 7
    assert plan_data_rows["process-later"]["process_priority"] == "P3"
    assert plan_data_rows["process-later"]["target_evidence_age_days"] == 14


def test_process_role_pin_forecast_controls_finish_and_resource_capacity():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    child_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-child",
            "name": "Child",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "dependencies": [process_id],
            "role_requirements": [
                {
                    "requirement_id": "req-child-eng",
                    "role_id": role_id,
                    "effort_hours": 1,
                }
            ],
        },
    ).entity_ids["process_id"]

    result = _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-ada-api",
            "process_id": process_id,
            "requirement_id": "req-api-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 13),
            "forecast_finish_at": _iso(13, 16),
            "updated_at": _iso(13, 13),
            "note": "Ada expects her API contribution done by noon.",
        },
    )
    pins = _query(
        service,
        {
            "action": "query_process_role_pins",
            "project_id": project_id,
            "as_of": _iso(13, 13),
            "process_id": process_id,
        },
    ).data["pins"]
    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(13, 13),
            "now": _iso(13, 13),
            "include_resource_fields": True,
            "include_allocation_slices": True,
        },
    ).data

    nodes = {node["process_id"]: node for node in graph["nodes"]}
    requirement = nodes[process_id]["role_requirements"][0]

    assert result.ok is True
    assert result.entity_ids["pin_id"] == "pin-ada-api"
    assert pins[0]["pin_id"] == "pin-ada-api"
    assert pins[0]["project_id"] == project_id
    assert pins[0]["process_id"] == process_id
    assert pins[0]["requirement_id"] == "req-api-eng"
    assert pins[0]["role_id"] == role_id
    assert pins[0]["resource_id"] == resource_id
    assert pins[0]["pinned_at"] == _iso(13, 13).replace("+00:00", "Z")
    assert pins[0]["pinned_started_at"] == _iso(13, 13).replace("+00:00", "Z")
    assert pins[0]["forecast_finish_at"] == _iso(13, 16).replace("+00:00", "Z")
    assert pins[0]["status"] == "pinned_started"
    assert pins[0]["verified_done_at"] is None
    assert pins[0]["verified_finished_at"] is None
    assert pins[0]["note"] == "Ada expects her API contribution done by noon."
    assert requirement["pin_status"] == "due"
    assert requirement["active_pinned_resource_ids"] == [resource_id]
    assert requirement["pins"][0]["pin_id"] == "pin-ada-api"
    assert requirement["pins"][0]["due"] is True
    assert nodes[process_id]["computed_status"] == "due"
    assert nodes[process_id]["resource_aware"]["ends_at"] == _iso(13, 16)
    assert nodes[child_id]["resource_aware"]["starts_at"] >= _iso(13, 16)
    assert all(
        row["resource_id"] != resource_id or row["starts_at"] >= _iso(13, 16)
        for row in graph["allocation_slices"]
        if row["process_id"] == child_id
    )


def test_pm_markdown_context_includes_resource_pin_evidence_targets():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-ada-api",
            "process_id": process_id,
            "requirement_id": "req-api-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 13),
            "forecast_finish_at": _iso(17, 13),
            "updated_at": _iso(13, 13),
        },
    )
    _handle(
        service,
        {
            "action": "upsert_resource_evidence_line_item",
            "project_id": project_id,
            "resource_id": resource_id,
            "line_item": "complete_pin_communication",
            "last_modified_at": _iso(13, 13),
            "last_evidence_at": _iso(13, 13),
            "evidence_note": "Ada said the API pin is her complete pinned plate.",
            "updated_at": _iso(13, 13),
        },
    )

    result = _query(
        service,
        {
            "action": "query_pm_markdown_context",
            "project_id": project_id,
            "as_of": _iso(16, 13),
            "now": _iso(16, 13),
        },
    )

    assert result.ok is True
    resource_lines = [
        item
        for item in result.data["evidence_line_items"]
        if item.get("resource_id") == resource_id
    ]
    lines_by_item = {item["line_item"]: item for item in resource_lines}
    assert set(lines_by_item) == {
        "understands_plan",
        "complete_pin_communication",
        "complete_planning_communication",
        "slippage_risk",
    }
    assert lines_by_item["complete_pin_communication"] == {
        "evidence_line_id": (
            lines_by_item["complete_pin_communication"]["evidence_line_id"]
        ),
        "project_id": project_id,
        "entity_type": "resource",
        "resource_id": resource_id,
        "resource_name": "Ada",
        "line_item": "complete_pin_communication",
        "last_modified_at": _iso(13, 13),
        "last_evidence_at": _iso(13, 13),
        "target_evidence_age_days": 7,
        "target_evidence_age_label": "< 7 days old",
        "evidence_age_exceeds_target": False,
        "evidence_is_stale": False,
        "evidence_note": "Ada said the API pin is her complete pinned plate.",
        "evidence_source": None,
        "created_at": _iso(13, 13),
        "updated_at": _iso(13, 13),
        "verification_state": "current",
        "is_current": True,
        "question": (
            "Has Ada completely communicated pin data for things they worked on "
            "previously as well as presently?"
        ),
    }
    assert lines_by_item["understands_plan"]["target_evidence_age_days"] == 7
    assert lines_by_item["understands_plan"]["last_evidence_at"] is None
    assert lines_by_item["understands_plan"]["evidence_is_stale"] is True
    markdown = result.data["markdown"]
    assert "## Resource Evidence" in markdown
    assert "`complete_pin_communication`: have they communicated complete pin data" in markdown
    assert (
        "Ada.complete_pin_communication last modified 3 days ago, "
        "last evidence that it's correct 3 days ago. Target evidence age: "
        "< 7 days old."
    ) in markdown
    assert (
        "*Ada.understands_plan last modified 3 days ago, "
        "last evidence that it's correct never. Target evidence age: < 7 days old."
    ) in markdown


def test_pm_markdown_context_follows_specified_project_context_shape():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, _resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    auth_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-auth",
            "name": "Build Auth",
            "description": "Done means auth works.",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-auth-eng",
                    "role_id": role_id,
                    "effort_hours": 2,
                }
            ],
        },
    ).entity_ids["process_id"]
    _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-ship",
            "name": "Ship API",
            "description": "Release the API deliverable.",
            "effective_at": _iso(13),
            "dependencies": [process_id, auth_id],
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-ship-eng",
                    "role_id": role_id,
                    "effort_hours": 2,
                }
            ],
        },
    )
    _handle(
        service,
        {
            "action": "upsert_milestone",
            "project_id": project_id,
            "milestone_id": "milestone-api-release",
            "name": "API release",
            "description": "API release checkpoint.",
            "process_symbols": ["process-ship"],
            "edit_at": _iso(13, 10),
        },
    )
    _handle(
        service,
        {
            "action": "commit_project_state",
            "project_id": project_id,
            "milestone_id": "milestone-api-release",
            "committed_at": _iso(13, 12),
            "note": "Milestone baseline",
            "resource_schedule_backend": "mcts",
            "include_resource_sensitivity": True,
            "resource_schedule_sensitivity_backend": "mcts",
            "resource_schedule_sensitivity_workers": 1,
            "resource_schedule_sensitivity_process_pool": False,
        },
    )

    result = _query(
        service,
        {
            "action": "query_pm_markdown_context",
            "project_id": project_id,
            "as_of": _iso(13, 22),
            "now": _iso(13, 22),
        },
    )

    assert result.ok is True
    query = QueryEnvelope.model_validate(
        {
            "query": {
                "action": "query_pm_markdown_context",
                "project_id": project_id,
                "as_of": _iso(13, 22),
                "now": _iso(13, 22),
            }
        }
    ).query
    assert query.resource_schedule_backend == "mcts"
    assert query.include_resource_sensitivity is True
    assert query.resource_schedule_sensitivity_backend == "mcts"
    assert query.resource_schedule_sensitivity_workers == 1
    assert query.resource_schedule_sensitivity_process_pool is False

    markdown = result.data["markdown"]
    assert "# Dependencies" not in markdown
    assert "Parents: {process-api, process-auth}" in markdown
    assert "Parents: {}" in markdown
    assert "- process-api -> process-ship" not in markdown
    assert "- process-auth -> process-ship" not in markdown
    assert "## milestone-api-release" in markdown
    assert "- Terminal processes: process-ship" in markdown
    assert "- Optimal make span:" in markdown
    assert "| process-ship | Ship API" in markdown
    assert "Planned start:" in markdown
    assert "Planned finish:" in markdown
    assert "Planned finish:" in markdown and "(in " in markdown
    assert "Planned " + "assignments:" not in markdown
    assert "Priority: P0 planned start in the past or pinned" in markdown
    assert "P1 planned start > 0 days and < 3 days" in markdown
    assert "P2 planned start > 3 days and < 7 days" in markdown
    assert "Sensitivity: optimal makespan change from adding 1 hour" in markdown
    assert "Sensitivity:" in markdown
    assert "Status: ready" in markdown
    assert "- blockers:" not in markdown
    assert "- allocation:" not in markdown
    assert "Definition: Release the API deliverable." in markdown
    assert "Role requirement: Engineer (role-engineer) | req-ship-eng" in markdown
    assert "Mode: planned" in markdown
    assert "process-ship.plan_data last modified" in markdown
    assert "last evidence that it's correct" in markdown
    assert 'Yes, Josh confirmed no new blockers' in markdown


def test_milestones_are_exposed_in_context_and_snapshot_queries():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, _resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-ship",
            "name": "Ship API",
            "effective_at": _iso(13),
            "dependencies": [process_id],
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-ship-eng",
                    "role_id": role_id,
                    "effort_hours": 2,
                }
            ],
        },
    )
    milestone_result = _handle(
        service,
        {
            "action": "upsert_milestone",
            "project_id": project_id,
            "milestone_id": "milestone-api-release",
            "name": "API release",
            "description": "API release checkpoint.",
            "process_symbols": ["process-ship"],
            "edit_at": _iso(13, 10),
        },
    )
    assert milestone_result.ok is True

    commit_result = _handle(
        service,
        {
            "action": "commit_project_state",
            "project_id": project_id,
            "milestone_id": "milestone-api-release",
            "committed_at": _iso(13, 12),
            "note": "Milestone baseline",
        },
    )
    assert commit_result.ok is True

    catalog = _query(
        service,
        {
            "action": "query_project_catalog",
            "project_id": project_id,
        },
    ).data
    assert catalog["milestones"][0]["milestone_id"] == "milestone-api-release"
    assert catalog["milestones"][0]["process_symbols"] == ["process-ship"]

    snapshot_data = _query(
        service,
        {
            "action": "query_schedule_snapshots",
            "project_id": project_id,
            "milestone_id": "milestone-api-release",
            "as_of": _iso(13, 12),
        },
    ).data
    assert snapshot_data["terminal_process_symbols"] == ["process-ship"]
    assert snapshot_data["snapshots"][0]["note"] == "Milestone baseline"

    context = _query(
        service,
        {
            "action": "query_agent_context",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
        },
    ).data
    assert context["milestones"][0]["name"] == "API release"
    assert context["milestones"][0]["slippage"]["snapshot_count"] == 1
    assert context["milestones"][0]["slippage"]["timeline"] == [
        {
            "commit_datetime": _iso(13, 12),
            "estimated_done_datetime": context["milestones"][0]["slippage"]["latest"][
                "completion_at"
            ],
        }
    ]


def test_agent_context_terminal_scope_filters_blockers_and_accepts_aliases():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, _resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    ship_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-ship",
            "name": "Ship API",
            "effective_at": _iso(13),
            "dependencies": [process_id],
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-ship-eng",
                    "role_id": role_id,
                    "effort_hours": 2,
                }
            ],
        },
    ).entity_ids["process_id"]
    docs_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-docs",
            "name": "Write Docs",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-docs-eng",
                    "role_id": role_id,
                    "effort_hours": 1,
                }
            ],
        },
    ).entity_ids["process_id"]
    _handle(
        service,
        {
            "action": "add_process_aliases",
            "project_id": project_id,
            "process_id": ship_id,
            "aliases": ["ship-target"],
            "edit_at": _iso(13, 12),
        },
    )
    _handle(
        service,
        {
            "action": "add_blocker",
            "project_id": project_id,
            "process_id": ship_id,
            "summary": "Ship approval",
            "severity": "blocking",
            "created_at": _iso(13, 12),
        },
    )
    _handle(
        service,
        {
            "action": "add_blocker",
            "project_id": project_id,
            "process_id": docs_id,
            "summary": "Docs review",
            "severity": "blocking",
            "created_at": _iso(13, 12),
        },
    )
    _handle(
        service,
        {
            "action": "commit_project_state",
            "project_id": project_id,
            "committed_at": _iso(13, 12),
            "terminal_process_symbols": ["process-ship"],
            "note": "Canonical ship scope",
        },
    )

    result = _query(
        service,
        {
            "action": "query_agent_context",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "now": _iso(13, 14),
            "terminal_process_symbols": ["ship-target"],
        },
    )

    data = result.data
    assert result.ok is True
    assert data["terminal_process_symbols"] == ["ship-target"]
    assert data["canonical_terminal_process_symbols"] == ["process-ship"]
    assert {node["symbol"] for node in data["graph"]["nodes"]} == {
        "process-api",
        "process-ship",
        "resolve-ship-approval",
    }
    assert [blocker["summary"] for blocker in data["blockers"]] == [
        "Ship approval"
    ]
    assert data["slippage"]["snapshot_count"] == 1
    assert data["slippage"]["latest"]["terminal_process_symbols"] == [
        "process-ship"
    ]
    assert data["slippage"]["latest"]["note"] == "Canonical ship scope"
    priority_symbols = {
        process["process_symbol"]
        for role in data["prioritized_work"]["by_role"]
        for process in role["processes"]
    }
    assert priority_symbols == {
        "process-api",
        "process-ship",
        "resolve-ship-approval",
    }

    explicit_scope_result = _query(
        service,
        {
            "action": "query_agent_context",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "now": _iso(13, 14),
            "scope": {"type": "project"},
            "terminal_process_symbols": ["ship-target"],
        },
    )
    explicit_scope_data = explicit_scope_result.data
    explicit_priority_symbols = {
        process["process_symbol"]
        for role in explicit_scope_data["prioritized_work"]["by_role"]
        for process in role["processes"]
    }
    assert {node["symbol"] for node in explicit_scope_data["graph"]["nodes"]} == {
        "process-api",
        "process-docs",
        "process-ship",
        "resolve-docs-review",
        "resolve-ship-approval",
    }
    assert explicit_priority_symbols == {
        "process-api",
        "process-docs",
        "process-ship",
        "resolve-docs-review",
        "resolve-ship-approval",
    }


def test_agent_context_propagates_resource_schedule_warnings():
    service = ProjectService(InMemoryProjectRepository())
    project_id, _role_id, _calendar_id, _resource_id, _process_id = (
        _seed_allocatable_project(service)
    )

    result = _query(
        service,
        {
            "action": "query_agent_context",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
            "max_iterations": 1,
        },
    )

    assert result.ok is True
    assert result.data["summary"]["converged"] is False
    assert result.warnings == [
        {
            "code": "max_iterations_reached",
            "message": "Resource schedule did not converge.",
            "severity": "warning",
            "details": {"max_iterations": 1},
        }
    ]


def test_upsert_resource_stores_timezone_aware_holidays():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id, role_id, calendar_id, resource_id, _ = _seed_allocatable_project(
        service,
    )

    _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": resource_id,
            "name": "Ada",
            "role_ids": [role_id],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13, 13),
            "cost_rate": "125.00",
            "cost_unit": "hour",
            "holidays": [
                {
                    "holiday_id": "ada-vacation",
                    "starts_at": _iso(18, 0),
                    "ends_at": _iso(19, 0),
                    "reason": "Vacation",
                }
            ],
        },
    )

    resource = _find_record_by_id(repository, "resource_id", resource_id)
    assert resource["holidays"] == [
        {
            "holiday_id": "ada-vacation",
            "starts_at": _at(18, 0),
            "ends_at": _at(19, 0),
            "reason": "Vacation",
        }
    ]


def test_cross_project_resource_references_are_rejected():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    first_project_id = _create_project(service, "First")
    second_project_id = _create_project(service, "Second")
    role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": first_project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    ).entity_ids["role_id"]
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": second_project_id,
            "calendar_id": "calendar-other",
            "name": "Other Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    before_rejected_command = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": first_project_id,
            "resource_id": "resource-cross-project",
            "name": "Cross Project Resource",
            "role_ids": [role_id],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13, 13),
            "cost_rate": "125.00",
            "cost_unit": "hour",
        },
    )

    _assert_command_error_result(result, "cross_project_calendar")
    assert _repository_snapshot(repository) == before_rejected_command


def test_cross_project_role_references_are_rejected_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    first_project_id = _create_project(service, "First")
    second_project_id = _create_project(service, "Second")
    role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": second_project_id,
            "role_id": "role-other-project",
            "name": "Other Project Role",
        },
    ).entity_ids["role_id"]
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": first_project_id,
            "calendar_id": "calendar-first",
            "name": "First Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    before_rejected_command = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": first_project_id,
            "resource_id": "resource-cross-project-role",
            "name": "Cross Project Role Resource",
            "role_ids": [role_id],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13, 13),
            "cost_rate": "125.00",
            "cost_unit": "hour",
        },
    )

    _assert_failed_command_result(result)
    assert _repository_snapshot(repository) == before_rejected_command


def test_cross_project_process_references_are_rejected_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    first_project_id = _create_project(service, "First")
    second_project_id = _create_project(service, "Second")
    role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": first_project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    ).entity_ids["role_id"]
    process_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": second_project_id,
            "process_id": "process-other-project",
            "name": "Other Project Work",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "role_requirements": [],
        },
    ).entity_ids["process_id"]
    before_rejected_command = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": first_project_id,
            "edit_at": _iso(14),
            "operations": [
                {
                    "action": "add_role_requirement",
                    "process_id": process_id,
                    "requirement": {
                        "requirement_id": "req-cross-project-process",
                        "role_id": role_id,
                        "effort_hours": 4,
                    },
                }
            ],
        },
    )

    _assert_failed_command_result(result)
    assert _repository_snapshot(repository) == before_rejected_command


def test_cross_project_requirement_references_are_rejected_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    first_project_id = _create_project(service, "First")
    second_project_id = _create_project(service, "Second")
    role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": second_project_id,
            "role_id": "role-other-project",
            "name": "Other Project Role",
        },
    ).entity_ids["role_id"]
    before_rejected_command = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": first_project_id,
            "process_id": "process-cross-project-requirement",
            "name": "Cross Project Requirement",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-cross-project-role",
                    "role_id": role_id,
                    "effort_hours": 4,
                }
            ],
        },
    )

    _assert_failed_command_result(result)
    assert _repository_snapshot(repository) == before_rejected_command


def test_cross_project_set_resource_roles_rejects_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    first_project_id, _, _, resource_id, _ = _seed_allocatable_project(service)
    second_project_id = _create_project(service, "Second")
    role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": second_project_id,
            "role_id": "role-other-project",
            "name": "Other Project Role",
        },
    ).entity_ids["role_id"]
    before_rejected_command = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "set_resource_roles",
            "project_id": first_project_id,
            "resource_id": resource_id,
            "role_ids": [role_id],
        },
    )

    _assert_failed_command_result(result)
    assert _repository_snapshot(repository) == before_rejected_command


def test_cross_project_set_resource_roles_resource_id_rejects_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    first_project_id, role_id, _, _, _ = _seed_allocatable_project(service)
    second_project_id = _create_project(service, "Second")
    other_role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": second_project_id,
            "role_id": "role-other-project-resource-owner",
            "name": "Other Project Resource Owner",
        },
    ).entity_ids["role_id"]
    other_calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": second_project_id,
            "calendar_id": "calendar-other-project-resource-owner",
            "name": "Other Project Resource Owner Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    other_resource_id = _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": second_project_id,
            "resource_id": "resource-other-project-owner",
            "name": "Other Project Owner",
            "role_ids": [other_role_id],
            "calendar_id": other_calendar_id,
            "available_from_at": _iso(13, 13),
            "cost_rate": "125.00",
            "cost_unit": "hour",
        },
    ).entity_ids["resource_id"]
    before_rejected_command = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "set_resource_roles",
            "project_id": first_project_id,
            "resource_id": other_resource_id,
            "role_ids": [role_id],
        },
    )

    _assert_failed_command_result(result)
    assert _repository_snapshot(repository) == before_rejected_command
    assert _find_record_by_id(repository, "resource_id", other_resource_id)[
        "project_id"
    ] == second_project_id


def test_cross_project_set_resource_calendar_resource_id_rejects_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    first_project_id, _, calendar_id, _, _ = _seed_allocatable_project(service)
    second_project_id = _create_project(service, "Second")
    other_role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": second_project_id,
            "role_id": "role-other-project-calendar-owner",
            "name": "Other Project Calendar Owner",
        },
    ).entity_ids["role_id"]
    other_calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": second_project_id,
            "calendar_id": "calendar-other-project-calendar-owner",
            "name": "Other Project Calendar Owner Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    other_resource_id = _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": second_project_id,
            "resource_id": "resource-other-project-calendar-owner",
            "name": "Other Project Calendar Owner",
            "role_ids": [other_role_id],
            "calendar_id": other_calendar_id,
            "available_from_at": _iso(13, 13),
            "cost_rate": "125.00",
            "cost_unit": "hour",
        },
    ).entity_ids["resource_id"]
    before_rejected_command = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "set_resource_calendar",
            "project_id": first_project_id,
            "resource_id": other_resource_id,
            "calendar_id": calendar_id,
        },
    )

    _assert_failed_command_result(result)
    assert _repository_snapshot(repository) == before_rejected_command
    assert _find_record_by_id(repository, "resource_id", other_resource_id)[
        "project_id"
    ] == second_project_id


def test_cross_project_set_resource_calendar_calendar_id_rejects_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    first_project_id, _, active_calendar_id, resource_id, _ = (
        _seed_allocatable_project(service)
    )
    second_project_id = _create_project(service, "Second")
    other_calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": second_project_id,
            "calendar_id": "calendar-other-project-target",
            "name": "Other Project Target Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    before_rejected_command = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "set_resource_calendar",
            "project_id": first_project_id,
            "resource_id": resource_id,
            "calendar_id": other_calendar_id,
        },
    )

    _assert_failed_command_result(result)
    assert _find_record_by_id(repository, "resource_id", resource_id)[
        "calendar_id"
    ] == active_calendar_id
    assert _find_record_by_id(repository, "calendar_id", other_calendar_id)[
        "project_id"
    ] == second_project_id
    assert _repository_snapshot(repository) == before_rejected_command


def test_direct_process_mutations_reject_cross_project_process_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    first_project_id = _create_project(service, "First")
    second_project_id = _create_project(service, "Second")
    process_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": second_project_id,
            "process_id": "process-other-project-direct",
            "name": "Other Project Direct",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "role_requirements": [],
        },
    ).entity_ids["process_id"]

    commands = [
        {
            "action": "set_process_status",
            "project_id": first_project_id,
            "process_id": process_id,
            "status": "in_progress",
            "edit_at": _iso(14),
        },
        {
            "action": "rename_process",
            "project_id": first_project_id,
            "process_id": process_id,
            "new_symbol": "renamed-cross-project",
            "edit_at": _iso(14),
        },
        {
            "action": "add_process_aliases",
            "project_id": first_project_id,
            "process_id": process_id,
            "aliases": ["cross-project-alias"],
            "edit_at": _iso(14),
        },
        {
            "action": "add_blocker",
            "project_id": first_project_id,
            "process_id": process_id,
            "blocker_id": "blocker-cross-project-process",
            "summary": "Cross-project blocker",
            "created_at": _iso(14),
        },
    ]

    for command in commands:
        before_rejected_command = _repository_snapshot(repository)

        result = _handle(service, command)

        _assert_failed_command_result(result)
        assert _repository_snapshot(repository) == before_rejected_command


def test_resolve_blocker_rejects_cross_project_blocker_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    first_project_id = _create_project(service, "First")
    second_project_id = _create_project(service, "Second")
    process_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": second_project_id,
            "process_id": "process-other-project-blocker-owner",
            "name": "Other Project Blocker Owner",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "role_requirements": [],
        },
    ).entity_ids["process_id"]
    blocker_id = _handle(
        service,
        {
            "action": "add_blocker",
            "project_id": second_project_id,
            "process_id": process_id,
            "blocker_id": "blocker-other-project-owner",
            "summary": "Owned by other project",
            "created_at": _iso(14),
        },
    ).entity_ids["blocker_id"]
    before_rejected_command = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "resolve_blocker",
            "project_id": first_project_id,
            "blocker_id": blocker_id,
            "resolved_at": _iso(15),
            "resolution": "Attempted from wrong project.",
        },
    )

    _assert_failed_command_result(result)
    assert _find_record_by_id(repository, "blocker_id", blocker_id)[
        "project_id"
    ] == second_project_id
    assert _find_record_by_id(repository, "blocker_id", blocker_id)[
        "resolved_at"
    ] is None
    assert _repository_snapshot(repository) == before_rejected_command


def test_inactive_roles_and_calendars_cannot_be_used_by_active_resources():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-inactive",
            "name": "Inactive Role",
        },
    ).entity_ids["role_id"]
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-active",
            "name": "Active Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": "resource-active-role-user",
            "name": "Active Role User",
            "role_ids": [role_id],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13, 13),
            "cost_rate": "125.00",
            "cost_unit": "hour",
        },
    )
    before_force_false = _repository_snapshot(repository)

    active_resource_result = _handle(
        service,
        {
            "action": "deactivate_role",
            "project_id": project_id,
            "role_id": role_id,
            "force": False,
        },
    )

    _assert_command_error_result(active_resource_result, "role_in_use")
    assert _repository_snapshot(repository) == before_force_false
    _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": "resource-role-still-active",
            "name": "Role Still Active",
            "role_ids": [role_id],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13, 13),
            "cost_rate": "125.00",
            "cost_unit": "hour",
        },
    )
    _handle(
        service,
        {
            "action": "deactivate_role",
            "project_id": project_id,
            "role_id": role_id,
            "force": True,
        },
    )
    before_inactive_role_upsert = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "name": "Ada",
            "role_ids": [role_id],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13, 13),
            "cost_rate": "125.00",
            "cost_unit": "hour",
        },
    )

    _assert_command_error_result(result, "inactive_role")
    assert _repository_snapshot(repository) == before_inactive_role_upsert


def test_inactive_calendar_cannot_be_assigned_to_active_resource_without_write():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id, _, active_calendar_id, resource_id, _ = _seed_allocatable_project(
        service
    )
    inactive_calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-inactive",
            "name": "Inactive Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
            "active": False,
        },
    ).entity_ids["calendar_id"]
    before_rejected_command = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "set_resource_calendar",
            "project_id": project_id,
            "resource_id": resource_id,
            "calendar_id": inactive_calendar_id,
        },
    )

    _assert_failed_command_result(result)
    assert _find_record_by_id(repository, "resource_id", resource_id)[
        "calendar_id"
    ] == active_calendar_id
    assert _repository_snapshot(repository) == before_rejected_command


def test_active_resource_empty_role_ids_rejects_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id, _, calendar_id, resource_id, _ = _seed_allocatable_project(service)
    before_upsert = _repository_snapshot(repository)

    upsert_result = _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": "resource-empty-roles",
            "name": "Empty Roles",
            "role_ids": [],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13, 13),
            "cost_rate": "125.00",
            "cost_unit": "hour",
            "active": True,
        },
    )
    before_set = _repository_snapshot(repository)
    set_result = _handle(
        service,
        {
            "action": "set_resource_roles",
            "project_id": project_id,
            "resource_id": resource_id,
            "role_ids": [],
        },
    )

    _assert_failed_command_result(upsert_result)
    assert _repository_snapshot(repository) == before_upsert
    _assert_failed_command_result(set_result)
    assert _repository_snapshot(repository) == before_set


def test_new_process_requirements_reject_inactive_roles_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-inactive-requirement",
            "name": "Inactive Requirement Role",
        },
    ).entity_ids["role_id"]
    _handle(
        service,
        {
            "action": "deactivate_role",
            "project_id": project_id,
            "role_id": role_id,
            "force": True,
        },
    )
    before_rejected_command = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-inactive-role-requirement",
            "name": "Inactive Role Requirement",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-inactive-role",
                    "role_id": role_id,
                    "effort_hours": 4,
                }
            ],
        },
    )

    _assert_failed_command_result(result)
    assert _repository_snapshot(repository) == before_rejected_command


def test_deactivate_role_force_false_rejects_current_revision_reference_no_write():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-revision-reference",
            "name": "Revision Reference",
        },
    ).entity_ids["role_id"]
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-revision-reference",
            "name": "Revision Reference Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-revision-reference",
            "name": "Revision Reference Work",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-revision-reference",
                    "role_id": role_id,
                    "effort_hours": 4,
                }
            ],
        },
    )
    before_rejected_deactivation = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "deactivate_role",
            "project_id": project_id,
            "role_id": role_id,
            "force": False,
        },
    )

    _assert_command_error_result(result, "role_in_use")
    assert _repository_snapshot(repository) == before_rejected_deactivation
    _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": "resource-after-rejected-deactivation",
            "name": "After Rejected Deactivation",
            "role_ids": [role_id],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13, 13),
            "cost_rate": "125.00",
            "cost_unit": "hour",
        },
    )


def test_deactivate_role_force_true_preserves_requirements_and_reports_missing_role():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id, role_id, _, _, process_id = _seed_allocatable_project(service)
    before_deactivation_requirement = _find_record_by_id(
        repository,
        "requirement_id",
        "req-api-eng",
    )

    result = _handle(
        service,
        {
            "action": "deactivate_role",
            "project_id": project_id,
            "role_id": role_id,
            "force": True,
        },
    )

    assert result.ok is True
    assert result.entity_ids["role_id"] == role_id
    assert _find_record_by_id(repository, "role_id", role_id)["active"] is False
    assert _find_record_by_id(repository, "requirement_id", "req-api-eng") == (
        before_deactivation_requirement
    )

    schedule = _query(
        service,
        _resource_schedule_query(project_id, include_allocation_slices=False),
    )

    assert process_id == "process-api"
    assert schedule.ok is False
    assert schedule.error.code == "resource_schedule_unsatisfiable"
    assert "missing_role" in schedule.error.message
    assert "req-api-eng" in schedule.error.message


def test_calendar_deactivation_requires_force_and_cannot_be_bypassed_by_upsert():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id, _, calendar_id, _, _ = _seed_allocatable_project(service)
    before_set_inactive = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "set_calendar_active",
            "project_id": project_id,
            "calendar_id": calendar_id,
            "active": False,
        },
    )
    _assert_command_error_result(result, "calendar_in_use")
    assert _repository_snapshot(repository) == before_set_inactive
    before_upsert_inactive = _repository_snapshot(repository)

    upsert_result = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": calendar_id,
            "name": "New York Weekdays",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
            "active": False,
        },
    )
    _assert_command_error_result(upsert_result, "calendar_in_use")
    assert _repository_snapshot(repository) == before_upsert_inactive


def test_set_calendar_active_force_true_preserves_references_and_removes_capacity():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id, role_id, calendar_id, resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    before_deactivation_resource = _find_record_by_id(
        repository,
        "resource_id",
        resource_id,
    )

    result = _handle(
        service,
        {
            "action": "set_calendar_active",
            "project_id": project_id,
            "calendar_id": calendar_id,
            "active": False,
            "force": True,
        },
    )

    assert result.ok is True
    assert result.entity_ids["calendar_id"] == calendar_id
    assert _find_record_by_id(repository, "calendar_id", calendar_id)["active"] is False
    assert _find_record_by_id(repository, "resource_id", resource_id) == (
        before_deactivation_resource
    )

    schedule = _query(
        service,
        _resource_schedule_query(project_id, include_allocation_slices=False),
    )
    capacity = _query(
        service,
        {
            "action": "query_resource_capacity",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "horizon_starts_at": _iso(13, 13),
            "horizon_ends_at": _iso(13, 15),
            "resource_ids": [resource_id],
            "role_ids": [role_id],
            "planning_granularity": "hour",
        },
    )

    assert process_id == "process-api"
    assert schedule.ok is False
    assert schedule.error.code == "resource_schedule_unsatisfiable"
    assert "no_calendar_capacity" in schedule.error.message
    assert "req-api-eng" in schedule.error.message
    assert resource_id == "resource-ada"
    assert capacity.data["buckets"] == []


def test_resource_commands_are_idempotent_for_supplied_ids():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    create_role = {
        "action": "create_role",
        "project_id": project_id,
        "role_id": "role-engineer",
        "name": "Engineer",
    }
    create_calendar = {
        "action": "upsert_resource_calendar",
        "project_id": project_id,
        "calendar_id": "calendar-nyc",
        "name": "New York Weekdays",
        "timezone": "America/New_York",
        "weekly_windows": _weekday_windows(),
    }
    add_exception = {
        "action": "add_calendar_exception",
        "project_id": project_id,
        "calendar_id": "calendar-nyc",
        "exception_id": "exception-holiday",
        "starts_at": _iso(18, 13),
        "ends_at": _iso(18, 17),
        "capacity_hours": 0,
        "reason": "Holiday",
    }
    upsert_resource = {
        "action": "upsert_resource",
        "project_id": project_id,
        "resource_id": "resource-ada",
        "name": "Ada",
        "role_ids": ["role-engineer"],
        "calendar_id": "calendar-nyc",
        "available_from_at": _iso(13, 13),
        "cost_rate": "125.00",
        "cost_unit": "hour",
    }

    first_role = _handle(service, create_role)
    second_role = _handle(service, create_role)
    _handle(service, create_calendar)
    first_exception = _handle(service, add_exception)
    second_exception = _handle(service, add_exception)
    first_resource = _handle(service, upsert_resource)
    second_resource = _handle(service, upsert_resource)

    assert second_role.entity_ids == first_role.entity_ids
    assert second_exception.entity_ids == first_exception.entity_ids
    assert second_resource.entity_ids == first_resource.entity_ids


def test_active_duplicate_role_names_are_rejected_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    )
    before_duplicate = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-reviewer",
            "name": "Engineer",
        },
    )

    _assert_failed_command_result(result)
    assert _repository_snapshot(repository) == before_duplicate


def test_rename_role_updates_catalog_and_rejects_duplicate_names():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    )
    _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-reviewer",
            "name": "Reviewer",
        },
    )

    rename_result = _handle(
        service,
        {
            "action": "rename_role",
            "project_id": project_id,
            "role_id": "role-reviewer",
            "name": "QA Reviewer",
        },
    )
    before_duplicate = _repository_snapshot(repository)
    duplicate_result = _handle(
        service,
        {
            "action": "rename_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "QA Reviewer",
        },
    )
    catalog_result = _query(
        service,
        {
            "action": "query_project_catalog",
            "project_id": project_id,
        },
    )

    assert rename_result.ok is True
    assert rename_result.entity_ids == {"role_id": "role-reviewer"}
    _assert_failed_command_result(duplicate_result)
    assert duplicate_result.error.code == "duplicate_role_name"
    assert _repository_snapshot(repository) == before_duplicate
    roles_by_id = {
        role["role_id"]: role["name"]
        for role in catalog_result.data["roles"]
    }
    assert roles_by_id == {
        "role-engineer": "Engineer",
        "role-reviewer": "QA Reviewer",
    }


def test_project_catalog_query_lists_roles_calendars_and_resources():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, calendar_id, resource_id, _ = _seed_allocatable_project(
        service,
    )

    result = _query(
        service,
        {
            "action": "query_project_catalog",
            "project_id": project_id,
        },
    )

    assert result.ok is True
    assert [role["role_id"] for role in result.data["roles"]] == [role_id]
    assert [calendar["calendar_id"] for calendar in result.data["calendars"]] == [
        calendar_id,
    ]
    assert [resource["resource_id"] for resource in result.data["resources"]] == [
        resource_id,
    ]


def test_role_id_reuse_with_conflicting_fields_rejects_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
        command_id="00000000-0000-4000-8000-000000000401",
    )
    original_role = _find_record_by_id(repository, "role_id", "role-engineer")
    before_conflict = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineering Lead",
        },
        command_id="00000000-0000-4000-8000-000000000402",
    )

    _assert_failed_command_result(result)
    assert _find_record_by_id(repository, "role_id", "role-engineer") == original_role
    assert _repository_snapshot(repository) == before_conflict


def test_active_duplicate_resource_names_are_rejected_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id, role_id, calendar_id, _, _ = _seed_allocatable_project(service)
    before_duplicate = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": "resource-grace",
            "name": "Ada",
            "role_ids": [role_id],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13, 13),
            "cost_rate": "125.00",
            "cost_unit": "hour",
        },
    )

    _assert_failed_command_result(result)
    assert _repository_snapshot(repository) == before_duplicate


def test_active_duplicate_calendar_names_are_rejected_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-nyc",
            "name": "New York Weekdays",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    )
    before_duplicate = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-copy",
            "name": "New York Weekdays",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    )

    _assert_failed_command_result(result)
    assert _repository_snapshot(repository) == before_duplicate


def test_duplicate_weekly_window_ids_within_calendar_reject_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-existing",
            "name": "Existing Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    existing_windows = _calendar_windows(repository, calendar_id)
    before_replacement = _repository_snapshot(repository)

    duplicate_windows = [
        {
            "window_id": "weekday-monday",
            "weekday": 0,
            "start_local_time": "09:00",
            "end_local_time": "12:00",
            "capacity_hours": 3,
        },
        {
            "window_id": "weekday-monday",
            "weekday": 0,
            "start_local_time": "13:00",
            "end_local_time": "17:00",
            "capacity_hours": 4,
        },
    ]
    replacement_result = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": calendar_id,
            "name": "Existing Calendar",
            "timezone": "America/New_York",
            "weekly_windows": duplicate_windows,
        },
    )
    before_add = _repository_snapshot(repository)

    add_result = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-duplicate-window",
            "name": "Duplicate Window Calendar",
            "timezone": "America/New_York",
            "weekly_windows": duplicate_windows,
        },
    )

    _assert_failed_command_result(replacement_result)
    assert _calendar_windows(repository, calendar_id) == existing_windows
    assert _repository_snapshot(repository) == before_replacement
    _assert_failed_command_result(add_result)
    assert _calendar_windows(repository, calendar_id) == existing_windows
    assert _repository_snapshot(repository) == before_add


def test_upsert_resource_calendar_rejects_overlapping_weekly_windows_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-overlap-existing",
            "name": "Overlap Existing Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    existing_windows = _calendar_windows(repository, calendar_id)
    before_rejected_upsert = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": calendar_id,
            "name": "Overlap Existing Calendar",
            "timezone": "America/New_York",
            "weekly_windows": [
                {
                    "window_id": "monday-morning",
                    "weekday": 0,
                    "start_local_time": "09:00",
                    "end_local_time": "12:00",
                    "capacity_hours": 3,
                },
                {
                    "window_id": "monday-overlap",
                    "weekday": 0,
                    "start_local_time": "11:00",
                    "end_local_time": "17:00",
                    "capacity_hours": 6,
                },
            ],
        },
    )

    _assert_failed_command_result(result)
    assert _calendar_windows(repository, calendar_id) == existing_windows
    assert _repository_snapshot(repository) == before_rejected_upsert


def test_add_calendar_exception_rejects_conflicting_overlap_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-exception-conflict-existing",
            "name": "Exception Conflict Existing Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    _handle(
        service,
        {
            "action": "add_calendar_exception",
            "project_id": project_id,
            "calendar_id": calendar_id,
            "exception_id": "exception-closed",
            "starts_at": _iso(18, 13),
            "ends_at": _iso(18, 16),
            "capacity_hours": 0,
            "reason": "Closed",
        },
    )
    existing_windows = _calendar_windows(repository, calendar_id)
    existing_exceptions = _calendar_exceptions(repository, calendar_id)
    before_rejected_add = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "add_calendar_exception",
            "project_id": project_id,
            "calendar_id": calendar_id,
            "exception_id": "exception-partial",
            "starts_at": _iso(18, 15),
            "ends_at": _iso(18, 17),
            "capacity_hours": 2,
            "reason": "Partial coverage",
        },
    )

    _assert_failed_command_result(result)
    assert _calendar_windows(repository, calendar_id) == existing_windows
    assert _calendar_exceptions(repository, calendar_id) == existing_exceptions
    assert _repository_snapshot(repository) == before_rejected_add


def test_duplicate_exception_ids_within_calendar_reject_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-existing",
            "name": "Existing Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    _handle(
        service,
        {
            "action": "add_calendar_exception",
            "project_id": project_id,
            "calendar_id": calendar_id,
            "exception_id": "exception-holiday",
            "starts_at": _iso(18, 13),
            "ends_at": _iso(18, 17),
            "capacity_hours": 0,
            "reason": "Holiday",
        },
    )
    existing_exceptions = _calendar_exceptions(repository, calendar_id)
    before_add = _repository_snapshot(repository)

    add_result = _handle(
        service,
        {
            "action": "add_calendar_exception",
            "project_id": project_id,
            "calendar_id": calendar_id,
            "exception_id": "exception-holiday",
            "starts_at": _iso(20, 13),
            "ends_at": _iso(20, 17),
            "capacity_hours": 0,
            "reason": "Duplicate add",
        },
    )

    _assert_failed_command_result(add_result)
    assert _calendar_exceptions(repository, calendar_id) == existing_exceptions
    assert _repository_snapshot(repository) == before_add


def test_remove_calendar_exception_is_idempotent_when_absent():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-existing",
            "name": "Existing Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    before_remove = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "remove_calendar_exception",
            "project_id": project_id,
            "calendar_id": calendar_id,
            "exception_id": "exception-already-absent",
        },
    )

    assert result.ok is True
    assert result.entity_ids["exception_id"] == "exception-already-absent"
    assert _repository_snapshot(repository) == before_remove


def test_remove_calendar_exception_removes_seeded_exception_idempotently():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-remove-exception",
            "name": "Remove Exception Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    add_result = _handle(
        service,
        {
            "action": "add_calendar_exception",
            "project_id": project_id,
            "calendar_id": calendar_id,
            "exception_id": "exception-remove-me",
            "starts_at": _iso(18, 13),
            "ends_at": _iso(18, 17),
            "capacity_hours": 0,
            "reason": "Remove me",
        },
    )
    assert add_result.ok is True
    assert [
        exception["exception_id"]
        for exception in _calendar_exceptions(repository, calendar_id)
    ] == ["exception-remove-me"]

    remove_command = {
        "action": "remove_calendar_exception",
        "project_id": project_id,
        "calendar_id": calendar_id,
        "exception_id": "exception-remove-me",
    }
    remove_result = _handle(service, remove_command)
    after_remove = _repository_snapshot(repository)
    replay_result = _handle(service, remove_command)
    second_remove_result = _handle(
        service,
        {
            **remove_command,
            "exception_id": "exception-remove-me",
        },
        command_id="00000000-0000-4000-8000-000000000501",
    )

    assert remove_result.ok is True
    assert _calendar_exceptions(repository, calendar_id) == []
    assert replay_result.ok is True
    assert second_remove_result.ok is True
    assert _repository_snapshot(repository) == after_remove


def test_calendar_upsert_preserves_exceptions_until_explicit_removal():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-preserve-exception",
            "name": "Preserve Exception Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    _handle(
        service,
        {
            "action": "add_calendar_exception",
            "project_id": project_id,
            "calendar_id": calendar_id,
            "exception_id": "exception-preserved",
            "starts_at": _iso(18, 13),
            "ends_at": _iso(18, 17),
            "capacity_hours": 0,
            "reason": "Preserved",
        },
    )
    original_exceptions = _calendar_exceptions(repository, calendar_id)

    _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": calendar_id,
            "name": "Preserve Exception Calendar",
            "timezone": "America/New_York",
            "weekly_windows": [
                {
                    "window_id": "weekday-short",
                    "weekday": 0,
                    "start_local_time": "10:00",
                    "end_local_time": "12:00",
                    "capacity_hours": 2,
                }
            ],
        },
    )

    assert _calendar_exceptions(repository, calendar_id) == original_exceptions
    _handle(
        service,
        {
            "action": "remove_calendar_exception",
            "project_id": project_id,
            "calendar_id": calendar_id,
            "exception_id": "exception-preserved",
        },
    )
    assert _calendar_exceptions(repository, calendar_id) == []


def test_set_resource_roles_duplicate_role_ids_reject_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id, role_id, _, resource_id, _ = _seed_allocatable_project(service)
    before_rejected_command = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "set_resource_roles",
            "project_id": project_id,
            "resource_id": resource_id,
            "role_ids": [role_id, role_id],
        },
    )

    _assert_failed_command_result(result)
    assert _repository_snapshot(repository) == before_rejected_command


def test_batch_set_resource_roles_duplicate_role_ids_rejects_atomically():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id, role_id, active_calendar_id, resource_id, _ = (
        _seed_allocatable_project(service)
    )
    new_calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-new-active",
            "name": "New Active Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    before_rejected_command = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14),
            "operations": [
                {
                    "action": "set_resource_calendar",
                    "resource_id": resource_id,
                    "calendar_id": new_calendar_id,
                },
                {
                    "action": "set_resource_roles",
                    "resource_id": resource_id,
                    "role_ids": [role_id, role_id],
                },
            ],
        },
    )

    _assert_failed_command_result(result)
    assert _find_record_by_id(repository, "resource_id", resource_id)[
        "calendar_id"
    ] == active_calendar_id
    assert _repository_snapshot(repository) == before_rejected_command


def test_batch_set_resource_calendar_inactive_calendar_rejects_atomically():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id, role_id, active_calendar_id, resource_id, _ = (
        _seed_allocatable_project(service)
    )
    reviewer_role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-reviewer",
            "name": "Reviewer",
        },
    ).entity_ids["role_id"]
    inactive_calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-batch-inactive",
            "name": "Batch Inactive Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
            "active": False,
        },
    ).entity_ids["calendar_id"]
    before_rejected_command = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14),
            "operations": [
                {
                    "action": "set_resource_roles",
                    "resource_id": resource_id,
                    "role_ids": [role_id, reviewer_role_id],
                },
                {
                    "action": "set_resource_calendar",
                    "resource_id": resource_id,
                    "calendar_id": inactive_calendar_id,
                },
            ],
        },
    )

    _assert_failed_command_result(result)
    resource = _find_record_by_id(repository, "resource_id", resource_id)
    assert resource["role_ids"] == [role_id]
    assert resource["calendar_id"] == active_calendar_id
    assert _repository_snapshot(repository) == before_rejected_command


def test_batch_set_resource_roles_empty_active_role_ids_rejects_atomically():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id, _, active_calendar_id, resource_id, _ = _seed_allocatable_project(
        service
    )
    new_calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-empty-roles-new",
            "name": "Empty Roles New Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    before_rejected_command = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14),
            "operations": [
                {
                    "action": "set_resource_calendar",
                    "resource_id": resource_id,
                    "calendar_id": new_calendar_id,
                },
                {
                    "action": "set_resource_roles",
                    "resource_id": resource_id,
                    "role_ids": [],
                },
            ],
        },
    )

    _assert_failed_command_result(result)
    assert _find_record_by_id(repository, "resource_id", resource_id)[
        "calendar_id"
    ] == active_calendar_id
    assert _repository_snapshot(repository) == before_rejected_command


def test_batch_upsert_resource_empty_active_role_ids_rejects_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id, _, calendar_id, _, _ = _seed_allocatable_project(service)
    before_rejected_command = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14),
            "operations": [
                {
                    "action": "upsert_resource",
                    "resource": {
                        "resource_id": "resource-empty-batch",
                        "name": "Empty Batch Resource",
                        "role_ids": [],
                        "calendar_id": calendar_id,
                        "available_from_at": _iso(13, 13),
                        "cost_rate": "125.00",
                        "cost_unit": "hour",
                        "active": True,
                    },
                }
            ],
        },
    )

    _assert_failed_command_result(result)
    assert _repository_snapshot(repository) == before_rejected_command


def test_batch_upsert_resource_inactive_role_rejects_without_writes():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id, _, calendar_id, _, _ = _seed_allocatable_project(service)
    inactive_role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-batch-inactive",
            "name": "Batch Inactive Role",
        },
    ).entity_ids["role_id"]
    _handle(
        service,
        {
            "action": "deactivate_role",
            "project_id": project_id,
            "role_id": inactive_role_id,
            "force": True,
        },
    )
    before_rejected_command = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14),
            "operations": [
                {
                    "action": "upsert_resource",
                    "resource": {
                        "resource_id": "resource-inactive-role-batch",
                        "name": "Inactive Role Batch Resource",
                        "role_ids": [inactive_role_id],
                        "calendar_id": calendar_id,
                        "available_from_at": _iso(13, 13),
                        "cost_rate": "125.00",
                        "cost_unit": "hour",
                        "active": True,
                    },
                }
            ],
        },
    )

    _assert_failed_command_result(result)
    assert _repository_snapshot(repository) == before_rejected_command


def test_batch_add_role_requirement_inactive_role_rejects_atomically():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id, _, active_calendar_id, resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    inactive_role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-inactive-batch-requirement",
            "name": "Inactive Batch Requirement",
        },
    ).entity_ids["role_id"]
    _handle(
        service,
        {
            "action": "deactivate_role",
            "project_id": project_id,
            "role_id": inactive_role_id,
            "force": True,
        },
    )
    new_calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-inactive-requirement-new",
            "name": "Inactive Requirement New Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    before_rejected_command = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14),
            "operations": [
                {
                    "action": "set_resource_calendar",
                    "resource_id": resource_id,
                    "calendar_id": new_calendar_id,
                },
                {
                    "action": "add_role_requirement",
                    "process_id": process_id,
                    "requirement": {
                        "requirement_id": "req-inactive-batch-role",
                        "role_id": inactive_role_id,
                        "effort_hours": 4,
                    },
                },
            ],
        },
    )

    _assert_failed_command_result(result)
    assert _find_record_by_id(repository, "resource_id", resource_id)[
        "calendar_id"
    ] == active_calendar_id
    assert _repository_snapshot(repository) == before_rejected_command


def test_command_id_replay_rejects_different_payload():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    command_id = uuid.UUID("00000000-0000-4000-8000-000000000001")

    service.handle_command(
        CommandEnvelope.model_validate(
            {
                "command_id": str(command_id),
                "command": {
                    "action": "create_role",
                    "project_id": project_id,
                    "role_id": "role-engineer",
                    "name": "Engineer",
                },
            }
        )
    )

    before_replay_conflict = _repository_snapshot(repository)
    result = service.handle_command(
        CommandEnvelope.model_validate(
            {
                "command_id": str(command_id),
                "command": {
                    "action": "create_role",
                    "project_id": project_id,
                    "role_id": "role-designer",
                    "name": "Designer",
                },
            }
        )
    )

    _assert_command_error_result(result, "idempotency_conflict")
    assert _repository_snapshot(repository) == before_replay_conflict


def test_historical_as_of_projection_uses_revision_owned_requirements():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id, role_id, _, _, process_id = _seed_allocatable_project(service)
    first_revision_state = {
        key: [revision.model_dump(mode="json") for revision in revisions]
        for key, revisions in repository.revisions_by_process.items()
    }
    _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": process_id,
            "name": "Build API v2",
            "effective_at": _iso(15),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-api-review",
                    "role_id": role_id,
                    "effort_hours": 4,
                }
            ],
        },
    )

    early = _query(
        service,
        {
            **_resource_schedule_query(project_id, include_allocation_slices=False),
            "as_of": _iso(14, 12),
            "now": _iso(14, 12),
        },
    )
    later = _query(
        service,
        {
            **_resource_schedule_query(project_id, include_allocation_slices=False),
            "as_of": _iso(16, 12),
            "now": _iso(16, 12),
        },
    )

    assert early.data["processes"][0]["requirement_ids"] == ["req-api-eng"]
    assert later.data["processes"][0]["requirement_ids"] == ["req-api-review"]
    assert first_revision_state[process_id][0]["required_roles"] == {}


def test_resource_schedule_query_returns_documented_output_contract():
    service = ProjectService(InMemoryProjectRepository())
    project_id, _, _, _, process_id = _seed_allocatable_project(service)

    result = _query(service, _resource_schedule_query(project_id))
    data = result.data

    _assert_no_nested_warnings(data)
    assert set(data) == {
        "project_id",
        "as_of",
        "now",
        "planning_granularity",
        "processes",
        "allocation_slices",
        "critical_path_process_ids",
        "converged",
        "iteration_count",
        "convergence",
    }
    assert data["project_id"] == project_id
    assert data["planning_granularity"] == "hour"
    assert data["critical_path_process_ids"] == [process_id]
    assert data["converged"] is True
    assert data["iteration_count"] >= 1
    assert data["convergence"] == {
        "converged": data["converged"],
        "iteration_count": data["iteration_count"],
        "max_iterations": 20,
        "tolerance_hours": 0,
        "changed_process_ids": [],
        "reason_changes": [],
        "allocation_fingerprint_changed": False,
    }

    row = data["processes"][0]
    assert set(row) == {
        "process_id",
        "name",
        "description",
        "ready_at",
        "starts_at",
        "ends_at",
        "dependency_only_starts_at",
        "dependency_only_ends_at",
        "resource_es_at",
        "resource_ef_at",
        "resource_ls_at",
        "resource_lf_at",
        "resource_slack_hours",
        "schedule_window_starts_at",
        "schedule_window_ends_at",
        "schedule_buffer_hours",
        "schedule_elapsed_hours",
        "role_sensitivity",
        "max_makespan_sensitivity_hours",
        "sensitivity_label",
        "inferred_duration_hours",
        "resource_delay_hours",
        "allocation_state",
        "allocation_diagnostic",
        "status",
        "started_at",
        "finished_at",
        "requirement_ids",
    }
    assert row["process_id"] == process_id
    assert row["description"] == ""
    assert row["allocation_state"] == "complete"
    assert row["starts_at"] is not None
    assert row["ends_at"] is not None
    assert row["finished_at"] is None
    assert row["requirement_ids"] == ["req-api-eng"]

    allocation_slice = data["allocation_slices"][0]
    assert set(allocation_slice) == {
        "slice_id",
        "project_id",
        "process_id",
        "requirement_id",
        "role_id",
        "resource_id",
        "starts_at",
        "ends_at",
        "effort_hours",
        "capacity_hours",
        "cost_amount",
        "cost_currency",
        "iteration",
    }
    assert allocation_slice["project_id"] == project_id
    assert allocation_slice["process_id"] == process_id
    assert allocation_slice["requirement_id"] == "req-api-eng"
    assert allocation_slice["cost_amount"] is None
    assert allocation_slice["cost_currency"] == "USD"


def test_resource_schedule_query_can_include_makespan_sensitivity():
    service = ProjectService(InMemoryProjectRepository())
    project_id, _, _, _, process_id = _seed_allocatable_project(service)
    query = _resource_schedule_query(project_id, include_allocation_slices=False)
    query.update(
        {
            "resource_schedule_backend": "mcts",
            "include_resource_sensitivity": True,
            "resource_schedule_sensitivity_backend": "mcts",
            "resource_schedule_sensitivity_workers": 1,
        }
    )

    result = _query(service, query)
    data = result.data

    sensitivity = data["resource_sensitivity"]
    assert len(sensitivity) == 1
    assert sensitivity[0]["process_id"] == process_id
    assert sensitivity[0]["requirement_id"] == "req-api-eng"
    assert sensitivity[0]["status"] == "ok"
    assert sensitivity[0]["makespan_delta_hours"] > 0
    row = data["processes"][0]
    assert row["role_sensitivity"] == sensitivity
    assert row["max_makespan_sensitivity_hours"] == sensitivity[0][
        "makespan_delta_hours"
    ]
    assert row["sensitivity_label"] == "makespan_sensitive"


def test_schedule_without_allocation_slices_preserves_timing_contract():
    service = ProjectService(InMemoryProjectRepository())
    project_id, _, _, _, _ = _seed_allocatable_project(service)

    with_slices = _query(
        service,
        _resource_schedule_query(project_id, include_allocation_slices=True),
    ).data
    without_slices = _query(
        service,
        _resource_schedule_query(project_id, include_allocation_slices=False),
    ).data

    assert without_slices["allocation_slices"] == []
    assert without_slices["processes"] == with_slices["processes"]
    assert without_slices["critical_path_process_ids"] == (
        with_slices["critical_path_process_ids"]
    )


def test_resource_capacity_query_returns_bucket_contract():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, calendar_id, resource_id, _ = _seed_allocatable_project(service)

    result = _query(
        service,
        {
            "action": "query_resource_capacity",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "horizon_starts_at": _iso(13, 13),
            "horizon_ends_at": _iso(13, 15),
            "resource_ids": [resource_id],
            "role_ids": [role_id],
        },
    )
    data = result.data

    _assert_no_nested_warnings(data)
    assert set(data) == {
        "project_id",
        "as_of",
        "horizon_starts_at",
        "horizon_ends_at",
        "planning_granularity",
        "buckets",
    }
    bucket = data["buckets"][0]
    assert set(bucket) == {
        "resource_id",
        "calendar_id",
        "starts_at",
        "ends_at",
        "capacity_hours",
        "available_hours",
        "allocated_hours",
        "remaining_hours",
        "role_ids",
        "local_date",
        "local_week",
    }
    assert bucket["resource_id"] == resource_id
    assert bucket["calendar_id"] == calendar_id
    assert bucket["role_ids"] == [role_id]


def test_unallocated_requirements_query_is_not_a_public_action():
    with pytest.raises(ValueError):
        QueryEnvelope.model_validate(
            {
                "query": {
                    "action": "query_unallocated_requirements",
                    "project_id": "project-api",
                    "as_of": _iso(13, 12),
                    "now": _iso(13, 12),
                }
            }
        )


def test_utilization_query_returns_aggregate_contract():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _, resource_id, _ = _seed_allocatable_project(service)

    result = _query(
        service,
        {
            "action": "query_utilization",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
        },
    )
    data = result.data

    _assert_no_nested_warnings(data)
    assert set(data) == {
        "project_id",
        "as_of",
        "planning_granularity",
        "by_resource",
        "by_role",
        "time_series",
        "overallocated_buckets",
    }
    by_resource = data["by_resource"][0]
    assert set(by_resource) == {
        "resource_id",
        "capacity_hours",
        "available_hours",
        "allocated_hours",
        "remaining_hours",
        "utilization_ratio",
    }
    assert by_resource["resource_id"] == resource_id
    assert by_resource["allocated_hours"] == 8

    by_role = data["by_role"][0]
    assert set(by_role) == {
        "role_id",
        "demanded_effort_hours",
        "fulfilled_effort_hours",
    }
    assert by_role["role_id"] == role_id
    assert by_role["demanded_effort_hours"] == 8
    assert by_role["fulfilled_effort_hours"] == 8
    assert data["overallocated_buckets"] == []


def test_cost_query_returns_decimal_string_contract_and_default_currency():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _, resource_id, process_id = _seed_allocatable_project(service)

    result = _query(
        service,
        {
            "action": "query_costs",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
        },
    )
    data = result.data

    _assert_no_nested_warnings(data)
    assert set(data) == {
        "project_id",
        "as_of",
        "currency",
        "total_cost",
        "by_resource",
        "by_process",
        "by_role",
        "time_series",
    }
    assert data["currency"] == "USD"
    assert data["total_cost"] == "1000.00"

    by_resource = data["by_resource"][0]
    assert set(by_resource) == {
        "resource_id",
        "cost_unit",
        "allocated_hours",
        "currency",
        "cost_amount",
    }
    assert by_resource == {
        "resource_id": resource_id,
        "cost_unit": "hour",
        "allocated_hours": 8,
        "currency": "USD",
        "cost_amount": "1000.00",
    }
    assert data["by_process"][0]["process_id"] == process_id
    assert data["by_process"][0]["currency"] == "USD"
    assert data["by_role"][0]["role_id"] == role_id
    assert data["by_role"][0]["currency"] == "USD"
    assert isinstance(data["time_series"][0]["cost_amount"], str)


def test_cost_query_group_by_time_serializes_omitted_dimensions_as_null():
    service = ProjectService(InMemoryProjectRepository())
    project_id, _, _, _, _ = _seed_allocatable_project(service)

    data = _query(
        service,
        {
            "action": "query_costs",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
            "currency": "USD",
            "group_by": ["time"],
        },
    ).data

    _assert_no_nested_warnings(data)
    assert data["by_resource"] == []
    assert data["by_process"] == []
    assert data["by_role"] == []

    bucket = data["time_series"][0]
    assert set(bucket) == {
        "starts_at",
        "ends_at",
        "resource_id",
        "process_id",
        "role_id",
        "allocated_hours",
        "currency",
        "cost_amount",
    }
    assert bucket["starts_at"] == _iso(13, 13)
    assert bucket["ends_at"] <= _iso(13, 21)
    assert bucket["resource_id"] is None
    assert bucket["process_id"] is None
    assert bucket["role_id"] is None
    assert bucket["currency"] == "USD"
    assert isinstance(bucket["cost_amount"], str)


def test_cost_query_group_by_resource_process_time_has_stable_bucket_shape():
    service = ProjectService(InMemoryProjectRepository())
    project_id, _, _, resource_id, process_id = _seed_allocatable_project(service)

    data = _query(
        service,
        {
            "action": "query_costs",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
            "currency": "USD",
            "group_by": ["resource", "process", "time"],
        },
    ).data

    _assert_no_nested_warnings(data)
    assert data["by_role"] == []

    bucket = data["time_series"][0]
    assert set(bucket) == {
        "starts_at",
        "ends_at",
        "resource_id",
        "process_id",
        "role_id",
        "allocated_hours",
        "currency",
        "cost_amount",
    }
    assert bucket["resource_id"] == resource_id
    assert bucket["process_id"] == process_id
    assert bucket["role_id"] is None
    assert bucket["starts_at"] == _iso(13, 13)
    assert bucket["ends_at"] <= _iso(13, 21)
    assert bucket["currency"] == "USD"
    assert isinstance(bucket["cost_amount"], str)


def test_process_graph_query_returns_lifecycle_windows_and_process_only_edges():
    service = ProjectService(InMemoryProjectRepository())
    project_id, _, _, _, process_id = _seed_allocatable_project(service)

    data = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
        },
    ).data

    _assert_no_nested_warnings(data)
    assert set(data) == {
        "project_id",
        "as_of",
        "now",
        "schedule_basis",
        "converged",
        "nodes",
        "edges",
        "critical_path_process_ids",
        "allocation_slices",
    }
    assert data["schedule_basis"] == "dependency_only"
    assert data["converged"] is None
    assert data["allocation_slices"] == []
    assert data["critical_path_process_ids"] == [process_id]

    node = data["nodes"][0]
    assert {
        "process_id",
        "process_symbol",
        "aliases",
        "name",
        "duration_hours",
        "earliest_start_at",
        "status",
        "finished_at",
        "computed_status",
        "blocker_summary",
        "dependency_only",
        "resource_aware",
        "work_now_window",
        "late_risk_window",
    } <= set(node)
    assert node["process_id"] == process_id
    assert node["resource_aware"] is None
    assert set(node["blocker_summary"]) == {
        "unresolved_count",
        "blocking_count",
        "blocker_ids",
    }
    assert set(node["work_now_window"]) == {"starts_at", "ends_at", "active"}
    assert set(node["late_risk_window"]) == {"starts_at", "ends_at", "active"}
    assert all("resource_id" not in edge for edge in data["edges"])


def test_target_history_query_is_removed_from_resource_api_contract():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-api",
            "name": "Build API",
            "effective_at": _iso(13),
            "duration_business_days": 1,
        },
    )

    with pytest.raises(ValueError):
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "set_project_target_at",
                    "project_id": project_id,
                    "target_at": _iso(20, 17),
                    "edit_at": _iso(13, 12),
                }
            }
        )
    with pytest.raises(ValueError):
        QueryEnvelope.model_validate(
            {
                "query": {
                    "action": "query_target_history",
                    "project_id": project_id,
                    "as_of": _iso(21),
                }
            }
        )


def test_process_role_pin_drives_start_progress_and_done_time():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, resource_id, process_id = (
        _seed_allocatable_project(service)
    )

    created = _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-api-eng",
            "process_id": process_id,
            "requirement_id": "req-api-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 13),
            "forecast_finish_at": _iso(13, 15),
            "status": "pinned_finished",
            "verified_done_at": _iso(13, 15),
            "updated_at": _iso(13, 15),
        },
    )
    assert created.ok is True
    pin_id = created.entity_ids["pin_id"]

    pins = _query(
        service,
        {
            "action": "query_process_role_pins",
            "project_id": project_id,
            "as_of": _iso(13, 16),
        },
    ).data["pins"]
    assert pins[0]["pin_id"] == pin_id
    assert pins[0]["pinned_at"] == _iso(13, 13).replace("+00:00", "Z")
    assert pins[0]["pinned_started_at"] == _iso(13, 13).replace("+00:00", "Z")
    assert pins[0]["forecast_finish_at"] == _iso(13, 15).replace("+00:00", "Z")
    assert pins[0]["verified_done_at"] == _iso(13, 15).replace("+00:00", "Z")
    assert pins[0]["verified_finished_at"] == _iso(13, 15).replace("+00:00", "Z")
    assert pins[0]["status"] == "pinned_finished"

    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(13, 16),
            "now": _iso(13, 16),
            "include_resource_fields": True,
            "resource_schedule_backend": "greedy",
        },
    ).data
    node = graph["nodes"][0]
    assert node["started_at"] == _iso(13, 13)
    assert node["finished_at"] == _iso(13, 15)
    assert node["computed_status"] == "finished"
    assert node["role_requirements"][0]["pin_status"] == "pinned_finished"
    assert "completed_" + "effort_hours" not in node["role_requirements"][0]
    assert "remaining_" + "effort_hours" not in node["role_requirements"][0]
    assert node["role_requirements"][0]["active_pinned_resource_ids"] == []
    assert node["role_requirements"][0]["recent_pinned_resource_ids"] == [resource_id]
    assert node["role_requirements"][0]["pins"][0]["pin_id"] == pin_id

    schedule = _query(
        service,
        {
            **_resource_schedule_query(project_id),
            "as_of": _iso(13, 16),
            "now": _iso(13, 16),
            "resource_schedule_backend": "greedy",
        },
    ).data
    planned = next(row for row in schedule["processes"] if row["process_id"] == process_id)
    assert planned["starts_at"] == _iso(13, 13)
    assert planned["started_at"] == _iso(13, 13)
    assert planned["ends_at"] == _iso(13, 15)
    planned_slices = [
        row
        for row in schedule["allocation_slices"]
        if row["process_id"] == process_id
    ]
    assert planned_slices == []

    _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "done",
            "edit_at": _iso(13, 16),
        },
    )
    done_graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(13, 16),
            "now": _iso(13, 16),
        },
    ).data
    done_node = done_graph["nodes"][0]
    assert done_node["status"] == "finished"
    assert done_node["finished_at"] == _iso(13, 15)


def test_process_role_pin_invariants_require_start_and_normalize_final_forecast():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    full_command = {
        "action": "upsert_process_role_pin",
        "project_id": project_id,
        "pin_id": "pin-api-eng",
        "process_id": process_id,
        "requirement_id": "req-api-eng",
        "role_id": role_id,
        "resource_id": resource_id,
        "pinned_at": _iso(13, 13),
        "forecast_finish_at": _iso(13, 14),
        "updated_at": _iso(13, 15),
    }

    for missing_key in ("resource_id", "pinned_at", "forecast_finish_at"):
        invalid_command = {**full_command}
        invalid_command.pop(missing_key)
        with pytest.raises(ValidationError):
            CommandEnvelope.model_validate({"command": invalid_command})
    with pytest.raises(ValidationError):
        CommandEnvelope.model_validate(
            {
                "command": {
                    **full_command,
                    "status": "pinned_finished",
                }
            }
        )

    created = _handle(
        service,
        {
            **full_command,
            "status": "pinned_finished",
            "verified_done_at": _iso(13, 15),
        },
    )
    pins = _query(
        service,
        {
            "action": "query_process_role_pins",
            "project_id": project_id,
            "as_of": _iso(13, 15),
        },
    ).data["pins"]

    assert created.ok is True
    assert pins[0]["forecast_finish_at"] == _iso(13, 15).replace("+00:00", "Z")
    assert pins[0]["verified_done_at"] == _iso(13, 15).replace("+00:00", "Z")


def test_process_role_pin_cannot_start_in_future():
    service = ProjectService(
        InMemoryProjectRepository(),
        now_provider=lambda: _at(13, 12),
    )
    project_id, role_id, _calendar_id, resource_id, process_id = (
        _seed_allocatable_project(service)
    )

    result = _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-api-eng",
            "process_id": process_id,
            "requirement_id": "req-api-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 13),
            "forecast_finish_at": _iso(13, 15),
            "updated_at": _iso(13, 13),
        },
    )

    assert result.ok is False
    assert result.error.code == "pin_start_in_future"
    assert result.error.details["field_path"] == "pinned_at"


def test_process_role_revision_change_deletes_obsolete_pin():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    replacement_role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-josh-exact",
            "name": "Josh Exact",
        },
    ).entity_ids["role_id"]
    pinned = _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-api-eng",
            "process_id": process_id,
            "requirement_id": "req-api-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 13),
            "forecast_finish_at": _iso(13, 15),
            "updated_at": _iso(13, 13),
        },
    )
    changed = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": process_id,
            "name": "Build API",
            "effective_at": _iso(14),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-api-josh",
                    "role_id": replacement_role_id,
                    "effort_hours": 4,
                }
            ],
        },
    )
    pins = _query(
        service,
        {
            "action": "query_process_role_pins",
            "project_id": project_id,
            "as_of": _iso(14, 12),
            "process_id": process_id,
            "include_done": True,
        },
    ).data["pins"]
    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(14, 12),
            "now": _iso(14, 12),
        },
    ).data
    node = next(node for node in graph["nodes"] if node["process_id"] == process_id)

    assert pinned.ok is True
    assert changed.ok is True
    assert pins == []
    assert node["computed_status"] == "ready"
    assert node["started_at"] is None
    assert node["role_requirements"] == [
        {
            "requirement_id": "req-api-josh",
            "role_id": replacement_role_id,
            "effort_hours": 4,
            "pin_status": "planned",
            "active_pinned_resource_ids": [],
            "recent_pinned_resource_ids": [],
            "pins": [],
            "required_resource_count": 1,
            "allocation_policy": "split_allowed",
            "min_allocation_hours_per_day": None,
            "max_allocation_hours_per_day": None,
        }
    ]


def test_process_role_cannot_be_verified_done_before_parent_processes_finish():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, resource_id, parent_id = (
        _seed_allocatable_project(service)
    )
    child_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-child",
            "name": "Child",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "dependencies": [parent_id],
            "role_requirements": [
                {
                    "requirement_id": "req-child-eng",
                    "role_id": role_id,
                    "effort_hours": 2,
                }
            ],
        },
    ).entity_ids["process_id"]

    started = _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-child-eng",
            "process_id": child_id,
            "requirement_id": "req-child-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 13),
            "forecast_finish_at": _iso(13, 15),
            "updated_at": _iso(13, 13),
        },
    )
    early_graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(13, 14),
            "now": _iso(13, 14),
        },
    ).data
    early_nodes = {node["process_id"]: node for node in early_graph["nodes"]}

    rejected = _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-child-eng",
            "process_id": child_id,
            "requirement_id": "req-child-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 13),
            "forecast_finish_at": _iso(13, 15),
            "status": "pinned_finished",
            "verified_done_at": _iso(13, 15),
            "updated_at": _iso(13, 15),
        },
    )

    assert started.ok is True
    assert early_nodes[child_id]["computed_status"] == "early_start"
    assert rejected.ok is False
    assert rejected.error.code == "pin_finish_requires_finished_parent_processes"
    assert rejected.error.details["unfinished_parent_processes"] == ["process-api"]

    parent_done = _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-api-eng",
            "process_id": parent_id,
            "requirement_id": "req-api-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 9),
            "forecast_finish_at": _iso(13, 16),
            "status": "pinned_finished",
            "verified_done_at": _iso(13, 16),
            "updated_at": _iso(13, 16),
        },
    )
    child_done = _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-child-eng",
            "process_id": child_id,
            "requirement_id": "req-child-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 13),
            "forecast_finish_at": _iso(13, 17),
            "status": "pinned_finished",
            "verified_done_at": _iso(13, 17),
            "updated_at": _iso(13, 17),
        },
    )

    assert parent_done.ok is True
    assert child_done.ok is True


def test_late_pin_pushes_downstream_dependency_schedule():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    child_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-child",
            "name": "Child",
            "effective_at": _iso(13),
            "dependencies": [process_id],
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-child-eng",
                    "role_id": role_id,
                    "effort_hours": 2,
                }
            ],
        },
    ).entity_ids["process_id"]

    before = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(14, 10),
            "now": _iso(14, 10),
        },
    ).data
    before_by_id = {node["process_id"]: node for node in before["nodes"]}
    assert before_by_id[process_id]["computed_status"] == "ready"
    assert before_by_id[child_id]["dependency_only"]["es_at"] == _iso(15, 10)

    _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-api-eng",
            "process_id": process_id,
            "requirement_id": "req-api-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(14),
            "forecast_finish_at": _iso(15),
            "updated_at": _iso(14, 10),
        },
    )

    after = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(14, 10),
            "now": _iso(14, 10),
        },
    ).data
    after_by_id = {node["process_id"]: node for node in after["nodes"]}

    assert after_by_id[process_id]["started_at"] == _iso(14)
    assert after_by_id[process_id]["dependency_only"]["ef_at"] == _iso(15)
    assert after_by_id[child_id]["dependency_only"]["es_at"] == _iso(15)


def test_process_role_pin_reserves_resource_capacity_until_forecast_finish():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    other_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-docs",
            "name": "Docs",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-docs-eng",
                    "role_id": role_id,
                    "effort_hours": 2,
                }
            ],
        },
    ).entity_ids["process_id"]
    _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-api-eng",
            "process_id": process_id,
            "requirement_id": "req-api-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 13),
            "forecast_finish_at": _iso(13, 15),
            "updated_at": _iso(13, 13),
        },
    )

    schedule = _query(
        service,
        {
            **_resource_schedule_query(project_id),
            "as_of": _iso(13, 16),
            "now": _iso(13, 16),
            "resource_schedule_backend": "greedy",
        },
    ).data
    overlapping_other = [
        row
        for row in schedule["allocation_slices"]
        if row["resource_id"] == resource_id
        and row["process_id"] == other_id
        and row["starts_at"] < _iso(13, 15)
        and row["ends_at"] > _iso(13, 13)
    ]

    assert overlapping_other == []


def test_unpinned_same_role_resource_can_plan_from_now_while_pin_reserves_resource():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, calendar_id, pinned_resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    available_resource_id = _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": "resource-grace",
            "name": "Grace",
            "role_ids": [role_id],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13, 13),
            "cost_rate": "100.00",
            "cost_unit": "hour",
        },
    ).entity_ids["resource_id"]
    planned_process_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-docs",
            "name": "Docs",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-docs-eng",
                    "role_id": role_id,
                    "effort_hours": 2,
                }
            ],
        },
    ).entity_ids["process_id"]
    _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-api-eng",
            "process_id": process_id,
            "requirement_id": "req-api-eng",
            "role_id": role_id,
            "resource_id": pinned_resource_id,
            "pinned_at": _iso(13, 13),
            "forecast_finish_at": _iso(13, 15),
            "updated_at": _iso(13, 13),
        },
    )

    schedule = _query(
        service,
        {
            **_resource_schedule_query(project_id),
            "as_of": _iso(13, 13),
            "now": _iso(13, 13),
            "resource_schedule_backend": "greedy",
        },
    ).data
    planned_slices = [
        row
        for row in schedule["allocation_slices"]
        if row["process_id"] == planned_process_id
    ]
    pinned_resource_slices_before_forecast = [
        row
        for row in schedule["allocation_slices"]
        if row["resource_id"] == pinned_resource_id
        and row["starts_at"] < _iso(13, 15)
    ]

    assert planned_slices[0]["resource_id"] == available_resource_id
    assert planned_slices[0]["starts_at"] == _iso(13, 13)
    assert pinned_resource_slices_before_forecast == []


def test_overdue_pin_uses_now_as_planning_lower_bound():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    child_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-child",
            "name": "Child",
            "effective_at": _iso(13),
            "dependencies": [process_id],
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-child-eng",
                    "role_id": role_id,
                    "effort_hours": 2,
                }
            ],
        },
    ).entity_ids["process_id"]
    _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-api-eng",
            "process_id": process_id,
            "requirement_id": "req-api-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 13),
            "forecast_finish_at": _iso(13, 16),
            "updated_at": _iso(13, 14),
        },
    )

    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(14, 10),
            "now": _iso(14, 10),
            "include_resource_fields": True,
            "resource_schedule_backend": "greedy",
        },
    ).data
    nodes = {node["process_id"]: node for node in graph["nodes"]}

    assert nodes[process_id]["computed_status"] == "started"
    assert nodes[process_id]["role_requirements"][0]["pin_status"] == "pinned_started"
    assert nodes[process_id]["role_requirements"][0]["pins"][0]["overdue"] is True
    assert nodes[process_id]["role_requirements"][0]["pins"][0]["due"] is False
    assert nodes[process_id]["resource_aware"]["ends_at"] >= _iso(14, 10)
    assert nodes[child_id]["resource_aware"]["starts_at"] >= _iso(14, 10)


def test_pre_ready_pin_derives_early_start_until_parent_is_finished():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, resource_id, parent_id = (
        _seed_allocatable_project(service)
    )
    child_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-child",
            "name": "Child",
            "effective_at": _iso(13),
            "dependencies": [parent_id],
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-child-eng",
                    "role_id": role_id,
                    "effort_hours": 2,
                }
            ],
        },
    ).entity_ids["process_id"]
    child_started = _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-child-eng",
            "process_id": child_id,
            "requirement_id": "req-child-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 13),
            "forecast_finish_at": _iso(13, 15),
            "updated_at": _iso(13, 13),
        },
    )
    assert child_started.ok is True

    premature_done = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": child_id,
            "status": "done",
            "edit_at": _iso(13, 15),
        },
    )
    assert premature_done.ok is False
    assert premature_done.error.code == "unfinished_parent_processes"

    before_parent_done = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(13, 15),
            "now": _iso(13, 15),
            "include_resource_fields": True,
            "resource_schedule_backend": "greedy",
        },
    ).data
    child = next(
        node for node in before_parent_done["nodes"] if node["process_id"] == child_id
    )
    assert child["started_at"] == _iso(13, 13)
    assert child["finished_at"] is None
    assert child["computed_status"] == "early_start"
    assert "completed_" + "effort_hours" not in child["role_requirements"][0]
    assert "remaining_" + "effort_hours" not in child["role_requirements"][0]
    assert child["role_requirements"][0]["pin_status"] == "due"
    assert child["resource_aware"]["starts_at"] == _iso(13, 13)

    _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-api-eng",
            "process_id": parent_id,
            "requirement_id": "req-api-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 13),
            "forecast_finish_at": _iso(13, 16),
            "status": "pinned_finished",
            "verified_done_at": _iso(13, 16),
            "updated_at": _iso(13, 16),
        },
    )
    _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": parent_id,
            "status": "done",
            "edit_at": _iso(14),
        },
    )
    _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-child-eng",
            "process_id": child_id,
            "requirement_id": "req-child-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 13),
            "forecast_finish_at": _iso(14, 9),
            "status": "pinned_finished",
            "verified_done_at": _iso(14, 9),
            "updated_at": _iso(14, 10),
        },
    )
    child_done = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": child_id,
            "status": "done",
            "edit_at": _iso(14, 10),
        },
    )
    assert child_done.ok is True

    after_parent_done = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(14, 10),
            "now": _iso(14, 10),
            "include_resource_fields": True,
            "resource_schedule_backend": "greedy",
        },
    ).data
    child = next(node for node in after_parent_done["nodes"] if node["process_id"] == child_id)
    assert child["started_at"] == _iso(13, 13)
    assert child["finished_at"] == _iso(14, 9)


def test_started_projection_is_derived_only_from_process_role_pins():
    service = ProjectService(InMemoryProjectRepository())
    project_id, _role_id, _calendar_id, _resource_id, process_id = (
        _seed_allocatable_project(service)
    )

    started = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "in_progress",
            "edit_at": _iso(13, 12),
        },
    )

    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(13, 13),
            "now": _iso(13, 13),
            "include_resource_fields": True,
            "resource_schedule_backend": "greedy",
        },
    ).data
    node = graph["nodes"][0]

    assert started.ok is False
    assert started.error.code == "started_requires_process_role_pin"
    assert node["status"] == "ready"
    assert node["started_at"] is None


def test_done_process_requires_verified_process_role_pins():
    service = ProjectService(InMemoryProjectRepository())
    project_id, _role_id, _calendar_id, _resource_id, process_id = (
        _seed_allocatable_project(service)
    )

    result = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "done",
            "edit_at": _iso(13, 16),
        },
    )

    assert result.ok is False
    assert result.error.code == "done_requires_verified_process_role_pins"
    assert result.error.details["missing_verified_pins"] == [
        {
            "requirement_id": "req-api-eng",
            "role_id": "role-engineer",
        }
    ]


def test_done_process_with_default_role_requires_verified_pin():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    process_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-admin-resolution",
            "name": "Resolve design question",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "role_requirements": [],
        },
    ).entity_ids["process_id"]

    result = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "done",
            "edit_at": _iso(13, 16),
        },
    )
    assert result.ok is False
    assert result.error.code == "done_requires_verified_process_role_pins"
    assert result.error.details["missing_verified_pins"] == [
        {
            "requirement_id": "process-admin-resolution-role_res_josh",
            "role_id": "role_res_josh",
        }
    ]

    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(13, 16),
            "now": _iso(13, 16),
            "include_resource_fields": True,
            "resource_schedule_backend": "greedy",
        },
    ).data
    node = graph["nodes"][0]

    assert node["status"] == "ready"
    assert node["computed_status"] == "ready"
    assert node["finished_at"] is None
    assert node.get("role_requirements", [])[0]["role_id"] == "role_res_josh"


def test_resource_can_be_pinned_to_multiple_process_roles():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    second_process_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-ui",
            "name": "Build UI",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-ui-eng",
                    "role_id": role_id,
                    "effort_hours": 4,
                }
            ],
        },
    ).entity_ids["process_id"]

    first = _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-api-eng",
            "process_id": process_id,
            "requirement_id": "req-api-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 13),
            "forecast_finish_at": _iso(13, 17),
            "updated_at": _iso(13, 13),
        },
    ).entity_ids["pin_id"]
    second = _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-ui-eng",
            "process_id": second_process_id,
            "requirement_id": "req-ui-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 15),
            "forecast_finish_at": _iso(13, 18),
            "updated_at": _iso(13, 15),
        },
    ).entity_ids["pin_id"]

    pins = _query(
        service,
        {
            "action": "query_process_role_pins",
            "project_id": project_id,
            "as_of": _iso(13, 16),
            "resource_id": resource_id,
        },
    ).data["pins"]
    by_id = {row["pin_id"]: row for row in pins}
    assert set(by_id) == {first, second}
    assert by_id[first]["status"] == "pinned_started"
    assert by_id[second]["status"] == "pinned_started"
    assert by_id[first]["forecast_finish_at"] == _iso(13, 17).replace("+00:00", "Z")
    assert by_id[second]["forecast_finish_at"] == _iso(13, 18).replace("+00:00", "Z")


def test_started_state_cannot_be_cleared_while_process_role_pin_exists():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-api-eng",
            "process_id": process_id,
            "requirement_id": "req-api-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 13),
            "forecast_finish_at": _iso(13, 15),
            "status": "pinned_finished",
            "verified_done_at": _iso(13, 15),
            "updated_at": _iso(13, 15),
        },
    )

    result = service.handle_command(
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "set_process_status",
                    "project_id": project_id,
                    "process_id": process_id,
                    "status": "planned",
                    "edit_at": _iso(13, 16),
                },
            },
        ),
    )

    assert result.ok is False
    assert result.error.code == "started_state_derived_from_pins"


def test_unpinned_unstarted_process_planned_start_is_lower_bounded_by_now():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    process_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-no-roles",
            "name": "Clarify roles",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "role_requirements": [],
        },
    ).entity_ids["process_id"]

    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(20, 12),
            "now": _iso(20, 12),
            "include_resource_fields": True,
            "resource_schedule_backend": "mcts",
        },
    ).data
    node = next(node for node in graph["nodes"] if node["process_id"] == process_id)

    assert node["computed_status"] == "ready"
    assert node["dependency_only"]["es_at"] == _iso(20, 12)
    assert node["resource_aware"]["starts_at"] == _iso(20, 12)


def test_child_planned_start_uses_max_of_parent_verified_finish_and_now():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    ).entity_ids["role_id"]
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-utc",
            "name": "UTC Weekdays",
            "timezone": "UTC",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    resource_id = _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": "resource-ada",
            "name": "Ada",
            "role_ids": [role_id],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13),
            "cost_rate": "100",
            "cost_unit": "hour",
        },
    ).entity_ids["resource_id"]
    parent_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-parent",
            "name": "Parent",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-parent-eng",
                    "role_id": role_id,
                    "effort_hours": 2,
                }
            ],
        },
    ).entity_ids["process_id"]
    child_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-child",
            "name": "Child",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "dependencies": [parent_id],
            "role_requirements": [
                {
                    "requirement_id": "req-child-eng",
                    "role_id": role_id,
                    "effort_hours": 2,
                }
            ],
        },
    ).entity_ids["process_id"]

    _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-parent-eng",
            "process_id": parent_id,
            "requirement_id": "req-parent-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 9),
            "forecast_finish_at": _iso(13, 11),
            "status": "pinned_finished",
            "verified_done_at": _iso(13, 11),
            "updated_at": _iso(13, 11),
        },
    )

    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(14, 10),
            "now": _iso(14, 10),
            "include_resource_fields": True,
        },
    ).data
    nodes = {node["process_id"]: node for node in graph["nodes"]}

    assert nodes[parent_id]["computed_status"] == "finished"
    assert nodes[parent_id]["finished_at"] == _iso(13, 11)
    assert nodes[child_id]["computed_status"] == "ready"
    assert nodes[child_id]["resource_aware"]["starts_at"] == _iso(14, 10)


def test_deleting_finished_pin_removes_forecast_verified_work_and_done_state():
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, resource_id, process_id = (
        _seed_allocatable_project(service)
    )
    pin_id = _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "pin_id": "pin-api-eng",
            "process_id": process_id,
            "requirement_id": "req-api-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 13),
            "forecast_finish_at": _iso(13, 15),
            "status": "pinned_finished",
            "verified_done_at": _iso(13, 15),
            "updated_at": _iso(13, 15),
        },
    ).entity_ids["pin_id"]
    done = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "done",
            "edit_at": _iso(13, 16),
        },
    )
    assert done.ok is True

    deleted = _handle(
        service,
        {
            "action": "delete_process_role_pin",
            "project_id": project_id,
            "pin_id": pin_id,
        },
    )
    pins = _query(
        service,
        {
            "action": "query_process_role_pins",
            "project_id": project_id,
            "as_of": _iso(13, 16),
            "process_id": process_id,
            "include_done": True,
        },
    ).data["pins"]
    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(13, 16),
            "now": _iso(13, 16),
            "include_resource_fields": True,
            "resource_schedule_backend": "greedy",
        },
    ).data
    node = next(node for node in graph["nodes"] if node["process_id"] == process_id)

    assert deleted.ok is True
    assert pins == []
    assert node["status"] == "ready"
    assert node["computed_status"] == "ready"
    assert node["started_at"] is None
    assert node["finished_at"] is None
    assert node["role_requirements"][0]["effort_hours"] == 8

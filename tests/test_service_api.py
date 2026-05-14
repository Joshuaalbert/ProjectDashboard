import datetime as dt
from typing import NamedTuple

import pytest
from pydantic import ValidationError

from projdash.service import results as result_models
from projdash.service.commands import (
    BatchCommandEnvelope,
    CommandEnvelope,
    CreateProject,
    UpsertProcessRevision,
)
from projdash.service.queries import (
    GetProject,
    QueryEnvelope,
    QueryProcessGraph,
    QueryProjects,
    QuerySchedule,
)
from projdash.service.repository import InMemoryProjectRepository
from projdash.service.service import ProjectService

UTC = dt.UTC


class ApiCase(NamedTuple):
    name: str
    payload: dict[str, object]


def _at(day: int, hour: int = 9) -> dt.datetime:
    return dt.datetime(2026, 5, day, hour, tzinfo=UTC)


def _resource_horizon() -> tuple[str, str, str]:
    return (
        _at(13, 0).isoformat(),
        _at(20, 0).isoformat(),
        _at(13, 12).isoformat(),
    )


def _result_model(name: str):
    model = getattr(result_models, name, None)
    assert model is not None, f"{name} result model is required by the DSL"
    return model


def _command_payload(command: dict[str, object]) -> dict[str, object]:
    return {"command_id": "00000000-0000-4000-8000-000000000101", "command": command}


def _query_payload(query: dict[str, object]) -> dict[str, object]:
    return {"query_id": "00000000-0000-4000-8000-000000000201", "query": query}


def _assert_validation_error_locates(
    payload: dict[str, object],
    *,
    envelope: type[CommandEnvelope] | type[QueryEnvelope],
    field_name: str,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        envelope.model_validate(payload)

    assert any(field_name in error["loc"] for error in exc_info.value.errors())


def test_command_envelope_json_round_trip():
    envelope = CommandEnvelope(
        command=CreateProject(
            name="Launch Plan",
            start_at=_at(13),
        )
    )

    decoded = CommandEnvelope.model_validate_json(envelope.model_dump_json())

    assert decoded.command.action == "create_project"
    assert decoded.command.name == "Launch Plan"
    assert decoded.command.start_at.tzinfo is not None


def test_naive_datetime_payload_reports_validation_error():
    with pytest.raises(ValidationError):
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "create_project",
                    "name": "Bad project",
                    "start_at": "2026-05-13T09:00:00",
                }
            }
        )


def test_invalid_command_payload_reports_validation_error():
    with pytest.raises(ValidationError):
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "upsert_process_revision",
                    "project_id": "project",
                    "name": "Bad process",
                    "effective_at": _at(13).isoformat(),
                    "duration_business_days": -1,
                }
            }
        )


def test_service_creates_project_and_process_revision():
    service = ProjectService(InMemoryProjectRepository())
    project_result = service.handle_command(
        CommandEnvelope(
            command=CreateProject(
                name="Agentic Rewrite",
                start_at=_at(13),
            )
        )
    )
    project_id = project_result.entity_ids["project_id"]

    process_result = service.handle_command(
        CommandEnvelope(
            command=UpsertProcessRevision(
                project_id=project_id,
                name="Define service API",
                effective_at=_at(13),
                duration_business_days=3,
                due_at=_at(20, 17),
            )
        )
    )

    project_query_result = service.handle_query(
        QueryEnvelope(
            query=GetProject(
                project_id=project_id,
            )
        )
    )
    graph_query_result = service.handle_query(
        QueryEnvelope(
            query=QueryProcessGraph(
                project_id=project_id,
                as_of=_at(13),
                now=_at(13),
            )
        )
    )

    assert process_result.entity_ids["process_id"]
    assert project_query_result.data["project"]["name"] == "Agentic Rewrite"
    assert graph_query_result.data["project_id"] == project_id
    assert graph_query_result.data["nodes"][0]["name"] == "Define service API"


def test_service_create_project_accepts_explicit_id_and_rejects_reuse():
    service = ProjectService(InMemoryProjectRepository())

    first = service.handle_command(
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "create_project",
                    "project_id": "project-stable",
                    "name": "Stable Project",
                    "start_at": _at(13).isoformat(),
                }
            }
        )
    )
    second = service.handle_command(
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "create_project",
                    "project_id": "project-stable",
                    "name": "Duplicate Stable Project",
                    "start_at": _at(13).isoformat(),
                }
            }
        )
    )

    assert first.ok is True
    assert first.entity_ids == {"project_id": "project-stable"}
    assert second.ok is False
    assert second.error.code == "project_conflict"


def test_service_lists_updates_and_deletes_projects_with_confirmation():
    service = ProjectService(InMemoryProjectRepository())
    service.handle_command(
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "create_project",
                    "project_id": "project-alpha",
                    "name": "Alpha",
                    "start_at": _at(13).isoformat(),
                }
            }
        )
    )
    service.handle_command(
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "create_project",
                    "project_id": "project-beta",
                    "name": "Beta",
                    "start_at": _at(14).isoformat(),
                }
            }
        )
    )

    update = service.handle_command(
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "update_project",
                    "project_id": "project-beta",
                    "name": "Beta Updated",
                    "default_currency": "gbp",
                }
            }
        )
    )
    with pytest.raises(ValidationError):
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "delete_project",
                    "project_id": "project-alpha",
                    "confirm_project_id": "not-alpha",
                }
            }
        )
    deleted = service.handle_command(
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "delete_project",
                    "project_id": "project-alpha",
                    "confirm_project_id": "project-alpha",
                }
            }
        )
    )
    listed = service.handle_query(QueryEnvelope(query=QueryProjects()))

    assert update.ok is True
    assert deleted.ok is True
    assert [project["project_id"] for project in listed.data["projects"]] == [
        "project-beta",
    ]
    assert listed.data["projects"][0]["name"] == "Beta Updated"
    assert listed.data["projects"][0]["default_currency"] == "GBP"


def test_batch_commands_are_applied_in_order():
    service = ProjectService(InMemoryProjectRepository())
    project_result = service.handle_command(
        CommandEnvelope(
            command=CreateProject(
                name="Batch Project",
                start_at=_at(13),
            )
        )
    )
    project_id = project_result.entity_ids["project_id"]

    results = service.handle_batch(
        BatchCommandEnvelope(
            commands=[
                CommandEnvelope(
                    command=UpsertProcessRevision(
                        project_id=project_id,
                        name="Write tests",
                        effective_at=_at(13),
                        duration_business_days=1,
                    )
                ),
                CommandEnvelope(
                    command=UpsertProcessRevision(
                        project_id=project_id,
                        name="Implement service",
                        effective_at=_at(13),
                        duration_business_days=2,
                    )
                ),
            ]
        )
    )

    assert [result.ok for result in results] == [True, True]


def test_unresolved_blocker_derives_blocked_state_until_resolved():
    service = ProjectService(InMemoryProjectRepository())
    project_id = service.handle_command(
        CommandEnvelope(
            command=CreateProject(
                name="Blocked Project",
                start_at=_at(13),
            )
        )
    ).entity_ids["project_id"]
    process_id = service.handle_command(
        CommandEnvelope(
            command=UpsertProcessRevision(
                project_id=project_id,
                name="Wait for review",
                effective_at=_at(13),
                duration_business_days=2,
            )
        )
    ).entity_ids["process_id"]

    blocker_id = service.handle_command(
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "add_blocker",
                    "project_id": project_id,
                    "process_id": process_id,
                    "summary": "Reviewer unavailable",
                    "severity": "blocking",
                    "created_at": _at(14).isoformat(),
                }
            }
        )
    ).entity_ids["blocker_id"]

    blockers = service.handle_query(
        QueryEnvelope.model_validate(
            {
                "query": {
                    "action": "query_blockers",
                    "project_id": project_id,
                    "as_of": _at(14, 12).isoformat(),
                }
            }
        )
    ).data
    graph = service.handle_query(
        QueryEnvelope.model_validate(
            {
                "query": {
                    "action": "query_process_graph",
                    "project_id": project_id,
                    "as_of": _at(14, 12).isoformat(),
                    "now": _at(14, 12).isoformat(),
                }
            }
        )
    ).data

    assert blockers["blocked_process_ids"] == [process_id]
    assert blockers["blockers"][0]["summary"] == "Reviewer unavailable"
    assert graph["nodes"][0]["status"] == "planned"
    assert graph["nodes"][0]["computed_status"] == "blocked"

    service.handle_command(
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "resolve_blocker",
                    "project_id": project_id,
                    "blocker_id": blocker_id,
                    "resolved_at": _at(15).isoformat(),
                    "resolution": "Reviewer returned.",
                }
            }
        )
    )

    resolved = service.handle_query(
        QueryEnvelope.model_validate(
            {
                "query": {
                    "action": "query_blockers",
                    "project_id": project_id,
                    "as_of": _at(15, 12).isoformat(),
                }
            }
        )
    ).data
    assert resolved["blocked_process_ids"] == []
    assert resolved["blockers"] == []


@pytest.mark.parametrize(
    "payload, expected_action",
    [
        (
            {
                "command": {
                    "action": "create_project",
                    "name": "Resource Project",
                    "start_at": _at(13).isoformat(),
                    "default_currency": "EUR",
                }
            },
            "create_project",
        ),
        (
            {
                "command": {
                    "action": "set_project_default_currency",
                    "project_id": "project-alpha",
                    "default_currency": "GBP",
                }
            },
            "set_project_default_currency",
        ),
        (
            {
                "command": {
                    "action": "create_role",
                    "project_id": "project-alpha",
                    "role_id": "role-engineer",
                    "name": "Engineer",
                }
            },
            "create_role",
        ),
        (
            {
                "command": {
                    "action": "rename_role",
                    "project_id": "project-alpha",
                    "role_id": "role-engineer",
                    "name": "Senior Engineer",
                }
            },
            "rename_role",
        ),
        (
            {
                "command": {
                    "action": "deactivate_role",
                    "project_id": "project-alpha",
                    "role_id": "role-engineer",
                    "force": True,
                }
            },
            "deactivate_role",
        ),
        (
            {
                "command": {
                    "action": "upsert_resource_calendar",
                    "project_id": "project-alpha",
                    "calendar_id": "calendar-nyc",
                    "name": "New York Weekdays",
                    "timezone": "America/New_York",
                    "weekly_windows": [
                        {
                            "window_id": "weekday-am",
                            "weekday": 0,
                            "start_local_time": "09:00",
                            "end_local_time": "17:00",
                            "capacity_hours": 8,
                        }
                    ],
                    "active": True,
                }
            },
            "upsert_resource_calendar",
        ),
        (
            {
                "command": {
                    "action": "set_calendar_active",
                    "project_id": "project-alpha",
                    "calendar_id": "calendar-nyc",
                    "active": False,
                    "force": True,
                }
            },
            "set_calendar_active",
        ),
        (
            {
                "command": {
                    "action": "add_calendar_exception",
                    "project_id": "project-alpha",
                    "calendar_id": "calendar-nyc",
                    "exception_id": "exception-holiday",
                    "starts_at": _at(18, 13).isoformat(),
                    "ends_at": _at(18, 17).isoformat(),
                    "capacity_hours": 0,
                    "reason": "Holiday",
                }
            },
            "add_calendar_exception",
        ),
        (
            {
                "command": {
                    "action": "remove_calendar_exception",
                    "project_id": "project-alpha",
                    "calendar_id": "calendar-nyc",
                    "exception_id": "exception-holiday",
                }
            },
            "remove_calendar_exception",
        ),
        (
            {
                "command": {
                    "action": "upsert_resource",
                    "project_id": "project-alpha",
                    "resource_id": "resource-ada",
                    "name": "Ada",
                    "role_ids": ["role-engineer"],
                    "calendar_id": "calendar-nyc",
                    "available_from_at": _at(13, 13).isoformat(),
                    "available_until_at": _at(20, 21).isoformat(),
                    "cost_rate": "125.00",
                    "cost_unit": "hour",
                    "cost_currency": "USD",
                    "active": True,
                }
            },
            "upsert_resource",
        ),
        (
            {
                "command": {
                    "action": "set_resource_active",
                    "project_id": "project-alpha",
                    "resource_id": "resource-ada",
                    "active": False,
                }
            },
            "set_resource_active",
        ),
        (
            {
                "command": {
                    "action": "set_resource_roles",
                    "project_id": "project-alpha",
                    "resource_id": "resource-ada",
                    "role_ids": ["role-engineer"],
                }
            },
            "set_resource_roles",
        ),
        (
            {
                "command": {
                    "action": "set_resource_calendar",
                    "project_id": "project-alpha",
                    "resource_id": "resource-ada",
                    "calendar_id": "calendar-nyc",
                }
            },
            "set_resource_calendar",
        ),
        (
            {
                "command": {
                    "action": "upsert_process_revision",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "name": "Build API",
                    "effective_at": _at(13).isoformat(),
                    "duration_business_days": 1,
                    "role_requirements": [
                        {
                            "requirement_id": "req-api-eng",
                            "role_id": "role-engineer",
                            "effort_hours": 8,
                            "min_allocation_hours_per_day": 1,
                            "max_allocation_hours_per_day": 6,
                            "required_resource_count": 2,
                            "allocation_policy": "contiguous",
                        }
                    ],
                }
            },
            "upsert_process_revision",
        ),
    ],
)
def test_resource_command_envelopes_json_round_trip(payload, expected_action):
    envelope = CommandEnvelope.model_validate(payload)

    decoded = CommandEnvelope.model_validate_json(envelope.model_dump_json())

    assert decoded.command.action == expected_action


@pytest.mark.parametrize("cost_unit", ["hour", "day", "week", "fixed"])
def test_resource_cost_units_are_accepted_by_command_schema(cost_unit):
    envelope = CommandEnvelope.model_validate(
        {
            "command": {
                "action": "upsert_resource",
                "project_id": "project-alpha",
                "resource_id": f"resource-{cost_unit}",
                "name": f"Resource {cost_unit}",
                "role_ids": ["role-engineer"],
                "calendar_id": "calendar-nyc",
                "available_from_at": _at(13, 13).isoformat(),
                "cost_rate": "125.00",
                "cost_unit": cost_unit,
                "cost_currency": "USD",
            }
        }
    )

    assert envelope.command.cost_unit == cost_unit


@pytest.mark.parametrize(
    "payload",
    [
        {
            "command": {
                "action": "create_role",
                "project_id": "project-alpha",
                "name": "Engineer",
                "unexpected": True,
            }
        },
        {
            "command": {
                "action": "upsert_resource_calendar",
                "project_id": "project-alpha",
                "name": "Bad Calendar",
                "timezone": "America/New_York",
                "weekly_windows": [
                    {
                        "weekday": 1,
                        "start_local_time": "09:00",
                        "end_local_time": "17:00",
                        "capacity_hours": 8,
                        "unexpected": True,
                    }
                ],
            }
        },
        {
            "command": {
                "action": "upsert_resource_calendar",
                "project_id": "project-alpha",
                "name": "Bad Calendar",
                "timezone": "America/New_York",
                "weekly_windows": [
                    {
                        "weekday": 1,
                        "start_local_time": "09:00",
                        "end_local_time": "17:00",
                        "capacity_hours": 8,
                    }
                ],
                "exceptions": [
                    {
                        "exception_id": "exception-inline",
                        "starts_at": _at(18, 13).isoformat(),
                        "ends_at": _at(18, 17).isoformat(),
                        "capacity_hours": 0,
                    }
                ],
            }
        },
        {
            "command": {
                "action": "upsert_process_revision",
                "project_id": "project-alpha",
                "name": "Bad requirement",
                "effective_at": _at(13).isoformat(),
                "duration_business_days": 1,
                "role_requirements": [
                    {
                        "role_id": "role-engineer",
                        "effort_hours": 8,
                        "unexpected": True,
                    }
                ],
            }
        },
    ],
)
def test_resource_command_payloads_reject_extra_fields(payload):
    with pytest.raises(ValidationError):
        CommandEnvelope.model_validate(payload)


@pytest.mark.parametrize(
    "field_name, payload",
    [
        (
            "starts_at",
            {
                "command": {
                    "action": "add_calendar_exception",
                    "project_id": "project-alpha",
                    "calendar_id": "calendar-nyc",
                    "starts_at": "2026-05-13T13:00:00",
                    "ends_at": _at(13, 17).isoformat(),
                    "capacity_hours": 0,
                }
            },
        ),
        (
            "available_from_at",
            {
                "command": {
                    "action": "upsert_resource",
                    "project_id": "project-alpha",
                    "name": "Ada",
                    "role_ids": ["role-engineer"],
                    "calendar_id": "calendar-nyc",
                    "available_from_at": "2026-05-13T13:00:00",
                    "cost_rate": "125.00",
                    "cost_unit": "hour",
                }
            },
        ),
    ],
)
def test_resource_command_moments_must_be_timezone_aware(field_name, payload):
    with pytest.raises(ValidationError) as exc_info:
        CommandEnvelope.model_validate(payload)

    assert field_name in str(exc_info.value)


def test_resource_command_defaults_are_applied():
    envelope = CommandEnvelope.model_validate(
        {
            "command": {
                "action": "upsert_process_revision",
                "project_id": "project-alpha",
                "name": "Defaulted requirement",
                "effective_at": _at(13).isoformat(),
                "duration_business_days": 1,
                "role_requirements": [
                    {
                        "role_id": "role-engineer",
                        "effort_hours": 8,
                    }
                ],
            }
        }
    )

    requirement = envelope.command.role_requirements[0]
    assert requirement.required_resource_count == 1
    assert requirement.allocation_policy == "split_allowed"


@pytest.mark.parametrize(
    "requirement",
    [
        {"role_id": "role-engineer", "effort_hours": 0},
        {
            "role_id": "role-engineer",
            "effort_hours": 8,
            "required_resource_count": 0,
        },
        {
            "role_id": "role-engineer",
            "effort_hours": 8,
            "max_allocation_hours_per_day": 0,
        },
        {
            "role_id": "role-engineer",
            "effort_hours": 8,
            "min_allocation_hours_per_day": 5,
            "max_allocation_hours_per_day": 4,
        },
        {
            "role_id": "role-engineer",
            "effort_hours": 8,
            "allocation_policy": "parallel",
        },
    ],
)
def test_role_requirement_validation_rejects_invalid_values(requirement):
    with pytest.raises(ValidationError):
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "upsert_process_revision",
                    "project_id": "project-alpha",
                    "name": "Bad requirement",
                    "effective_at": _at(13).isoformat(),
                    "duration_business_days": 1,
                    "role_requirements": [requirement],
                }
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "command": {
                "action": "upsert_resource_calendar",
                "project_id": "project-alpha",
                "name": "Bad Calendar",
                "timezone": "America/New_York",
                "weekly_windows": [
                    {
                        "weekday": 7,
                        "start_local_time": "09:00",
                        "end_local_time": "17:00",
                        "capacity_hours": 8,
                    }
                ],
            }
        },
        {
            "command": {
                "action": "upsert_resource_calendar",
                "project_id": "project-alpha",
                "name": "Bad Calendar",
                "timezone": "America/New_York",
                "weekly_windows": [
                    {
                        "weekday": 1,
                        "start_local_time": "17:00",
                        "end_local_time": "09:00",
                        "capacity_hours": 8,
                    }
                ],
            }
        },
        {
            "command": {
                "action": "upsert_resource_calendar",
                "project_id": "project-alpha",
                "name": "Bad Calendar",
                "timezone": "America/New_York",
                "weekly_windows": [
                    {
                        "weekday": 1,
                        "start_local_time": "09:00",
                        "end_local_time": "17:00",
                        "capacity_hours": -1,
                    }
                ],
            }
        },
        {
            "command": {
                "action": "add_calendar_exception",
                "project_id": "project-alpha",
                "calendar_id": "calendar-nyc",
                "starts_at": _at(13, 17).isoformat(),
                "ends_at": _at(13, 13).isoformat(),
                "capacity_hours": 0,
            }
        },
        {
            "command": {
                "action": "upsert_resource",
                "project_id": "project-alpha",
                "name": "Ada",
                "role_ids": ["role-engineer"],
                "calendar_id": "calendar-nyc",
                "available_from_at": _at(13, 13).isoformat(),
                "cost_rate": "-1.00",
                "cost_unit": "hour",
            }
        },
        {
            "command": {
                "action": "upsert_resource",
                "project_id": "project-alpha",
                "name": "Ada",
                "role_ids": ["role-engineer"],
                "calendar_id": "calendar-nyc",
                "available_from_at": _at(13, 13).isoformat(),
                "cost_rate": "125.00",
                "cost_unit": "month",
            }
        },
    ],
)
def test_resource_command_validation_rejects_invalid_values(payload):
    with pytest.raises(ValidationError):
        CommandEnvelope.model_validate(payload)


@pytest.mark.parametrize(
    "payload, expected_action",
    [
        (
            {
                "query": {
                    "action": "query_resource_schedule",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "horizon_starts_at": _at(13, 0).isoformat(),
                    "horizon_ends_at": _at(20, 0).isoformat(),
                    "include_allocation_slices": True,
                    "planning_granularity": "hour",
                    "max_iterations": 5,
                    "convergence_tolerance_hours": 0.25,
                    "blocked_policy": "include_normally",
                }
            },
            "query_resource_schedule",
        ),
        (
            {
                "query": {
                    "action": "query_utilization",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "horizon_starts_at": _at(13, 0).isoformat(),
                    "horizon_ends_at": _at(20, 0).isoformat(),
                }
            },
            "query_utilization",
        ),
        (
            {
                "query": {
                    "action": "query_costs",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "horizon_starts_at": _at(13, 0).isoformat(),
                    "horizon_ends_at": _at(20, 0).isoformat(),
                    "currency": "USD",
                }
            },
            "query_costs",
        ),
        (
            {
                "query": {
                    "action": "query_resource_capacity",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "horizon_starts_at": _at(13, 0).isoformat(),
                    "horizon_ends_at": _at(20, 0).isoformat(),
                    "resource_ids": ["resource-ada"],
                    "role_ids": ["role-engineer"],
                }
            },
            "query_resource_capacity",
        ),
        (
            {
                "query": {
                    "action": "query_unallocated_requirements",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "horizon_starts_at": _at(13, 0).isoformat(),
                    "horizon_ends_at": _at(20, 0).isoformat(),
                }
            },
            "query_unallocated_requirements",
        ),
    ],
)
def test_resource_query_envelopes_json_round_trip(payload, expected_action):
    envelope = QueryEnvelope.model_validate(payload)

    decoded = QueryEnvelope.model_validate_json(envelope.model_dump_json())

    assert decoded.query.action == expected_action


def test_resource_query_defaults_are_applied():
    horizon_starts_at, horizon_ends_at, as_of = _resource_horizon()

    schedule = QueryEnvelope.model_validate(
        {
            "query": {
                "action": "query_resource_schedule",
                "project_id": "project-alpha",
                "as_of": as_of,
                "now": as_of,
                "horizon_starts_at": horizon_starts_at,
                "horizon_ends_at": horizon_ends_at,
            }
        }
    )
    costs = QueryEnvelope.model_validate(
        {
            "query": {
                "action": "query_costs",
                "project_id": "project-alpha",
                "as_of": as_of,
                "now": as_of,
                "horizon_starts_at": horizon_starts_at,
                "horizon_ends_at": horizon_ends_at,
            }
        }
    )

    assert schedule.query.planning_granularity == "hour"
    assert schedule.query.blocked_policy == "exclude"
    assert schedule.query.max_iterations == 20
    assert schedule.query.convergence_tolerance_hours == 0
    assert schedule.query.include_allocation_slices is False
    assert costs.query.currency is None


@pytest.mark.parametrize(
    "payload",
    [
        {
            "query": {
                "action": "query_resource_schedule",
                "project_id": "project-alpha",
                "as_of": "2026-05-13T12:00:00",
                "now": _at(13, 12).isoformat(),
                "horizon_starts_at": _at(13, 0).isoformat(),
                "horizon_ends_at": _at(20, 0).isoformat(),
            }
        },
        {
            "query": {
                "action": "query_resource_schedule",
                "project_id": "project-alpha",
                "as_of": _at(13, 12).isoformat(),
                "now": _at(13, 12).isoformat(),
                "horizon_starts_at": _at(20, 0).isoformat(),
                "horizon_ends_at": _at(13, 0).isoformat(),
            }
        },
        {
            "query": {
                "action": "query_resource_schedule",
                "project_id": "project-alpha",
                "as_of": _at(13, 12).isoformat(),
                "now": _at(13, 12).isoformat(),
                "horizon_starts_at": _at(13, 0).isoformat(),
                "horizon_ends_at": _at(20, 0).isoformat(),
                "bucket_size": "hour",
            }
        },
        {
            "query": {
                "action": "query_resource_schedule",
                "project_id": "project-alpha",
                "as_of": _at(13, 12).isoformat(),
                "now": _at(13, 12).isoformat(),
                "horizon_starts_at": _at(13, 0).isoformat(),
                "horizon_ends_at": _at(20, 0).isoformat(),
                "planning_granularity": "day",
            }
        },
        {
            "query": {
                "action": "query_resource_schedule",
                "project_id": "project-alpha",
                "as_of": _at(13, 12).isoformat(),
                "now": _at(13, 12).isoformat(),
                "horizon_starts_at": _at(13, 0).isoformat(),
                "horizon_ends_at": _at(20, 0).isoformat(),
                "max_iterations": 0,
            }
        },
        {
            "query": {
                "action": "query_resource_schedule",
                "project_id": "project-alpha",
                "as_of": _at(13, 12).isoformat(),
                "now": _at(13, 12).isoformat(),
                "horizon_starts_at": _at(13, 0).isoformat(),
                "horizon_ends_at": _at(20, 0).isoformat(),
                "convergence_tolerance_hours": -0.1,
            }
        },
        {
            "query": {
                "action": "query_resource_schedule",
                "project_id": "project-alpha",
                "as_of": _at(13, 12).isoformat(),
                "now": _at(13, 12).isoformat(),
                "horizon_starts_at": _at(13, 0).isoformat(),
                "horizon_ends_at": _at(20, 0).isoformat(),
                "blocked_policy": "ignore",
            }
        },
        {
            "query": {
                "action": "query_utilization",
                "project_id": "project-alpha",
                "as_of": _at(13, 12).isoformat(),
                "now": _at(13, 12).isoformat(),
                "horizon_starts_at": _at(13, 0).isoformat(),
                "horizon_ends_at": _at(20, 0).isoformat(),
                "include_allocation_slices": True,
            }
        },
        {
            "query": {
                "action": "query_costs",
                "project_id": "project-alpha",
                "as_of": _at(13, 12).isoformat(),
                "now": _at(13, 12).isoformat(),
                "horizon_starts_at": _at(13, 0).isoformat(),
                "horizon_ends_at": _at(20, 0).isoformat(),
                "include_allocation_slices": True,
            }
        },
        {
            "query": {
                "action": "query_unallocated_requirements",
                "project_id": "project-alpha",
                "as_of": _at(13, 12).isoformat(),
                "now": _at(13, 12).isoformat(),
                "horizon_starts_at": _at(13, 0).isoformat(),
                "horizon_ends_at": _at(20, 0).isoformat(),
                "include_allocation_slices": True,
            }
        },
    ],
)
def test_resource_query_validation_rejects_invalid_payloads(payload):
    with pytest.raises(ValidationError):
        QueryEnvelope.model_validate(payload)


LEGACY_REQUIRED_ROLES_WARNING = {
    "code": "legacy_required_roles",
    "message": "required_roles was accepted in transition mode.",
    "severity": "warning",
    "details": {"mode": "dual_write_warn"},
}


def _service_with_resource_project(
    *,
    required_roles_transition_mode: str,
) -> tuple[ProjectService, str, str]:
    service = ProjectService(
        InMemoryProjectRepository(),
        required_roles_transition_mode=required_roles_transition_mode,
    )
    project_id = service.handle_command(
        CommandEnvelope(
            command=CreateProject(
                name="Resource Transition Project",
                start_at=_at(13),
            )
        )
    ).entity_ids["project_id"]
    role_id = service.handle_command(
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "create_role",
                    "project_id": project_id,
                    "name": "Engineer",
                }
            }
        )
    ).entity_ids["role_id"]

    return service, project_id, role_id


def _revision_payload(
    project_id: str,
    *,
    name: str,
    role_fields: dict[str, object],
) -> dict[str, object]:
    command = {
        "action": "upsert_process_revision",
        "project_id": project_id,
        "name": name,
        "effective_at": _at(13).isoformat(),
        "duration_business_days": 2,
    }
    command.update(role_fields)
    return {"command": command}


def _assert_validation_error_result(result) -> None:
    assert result.ok is False
    assert result.error.code == "validation_error"
    assert result.error.message
    assert result.error.details == {}
    assert result.error.validation_errors
    assert result.error.validation_errors[0].loc[0] == "command"
    assert result.error.validation_errors[0].type
    assert result.warnings == []


def test_allow_legacy_transition_mode_accepts_legacy_required_roles():
    service, project_id, _role_id = _service_with_resource_project(
        required_roles_transition_mode="allow_legacy",
    )

    result = service.handle_command(
        CommandEnvelope.model_validate(
            _revision_payload(
                project_id,
                name="Legacy requirement",
                role_fields={"required_roles": {"engineer": 1.0}},
            )
        )
    )

    assert result.ok is True
    assert result.entity_ids["process_id"]
    assert result.entity_ids["revision_id"]
    assert result.warnings == []


def test_allow_legacy_transition_mode_accepts_role_requirements():
    service, project_id, role_id = _service_with_resource_project(
        required_roles_transition_mode="allow_legacy",
    )

    result = service.handle_command(
        CommandEnvelope.model_validate(
            _revision_payload(
                project_id,
                name="Effort requirement",
                role_fields={
                    "role_requirements": [
                        {
                            "role_id": role_id,
                            "effort_hours": 16,
                        }
                    ]
                },
            )
        )
    )

    assert result.ok is True
    assert result.entity_ids["process_id"]
    assert result.entity_ids["revision_id"]
    assert result.warnings == []


def test_allow_legacy_transition_mode_rejects_mixed_role_requirement_shapes():
    service, project_id, role_id = _service_with_resource_project(
        required_roles_transition_mode="allow_legacy",
    )

    result = service.handle_command(
        CommandEnvelope.model_validate(
            _revision_payload(
                project_id,
                name="Mixed requirements",
                role_fields={
                    "required_roles": {"engineer": 1.0},
                    "role_requirements": [
                        {
                            "role_id": role_id,
                            "effort_hours": 16,
                        }
                    ],
                },
            )
        )
    )

    _assert_validation_error_result(result)


def test_require_role_requirements_transition_mode_rejects_legacy_required_roles():
    service, project_id, _role_id = _service_with_resource_project(
        required_roles_transition_mode="require_role_requirements",
    )

    result = service.handle_command(
        CommandEnvelope.model_validate(
            _revision_payload(
                project_id,
                name="Legacy requirement",
                role_fields={"required_roles": {"engineer": 1.0}},
            )
        )
    )

    _assert_validation_error_result(result)


def test_wrapper_warning_for_legacy_required_roles_is_not_nested_in_data():
    service = ProjectService(
        InMemoryProjectRepository(),
        required_roles_transition_mode="dual_write_warn",
    )
    project_id = service.handle_command(
        CommandEnvelope(
            command=CreateProject(
                name="Legacy Resource Project",
                start_at=_at(13),
            )
        )
    ).entity_ids["project_id"]

    result = service.handle_command(
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "upsert_process_revision",
                    "project_id": project_id,
                    "name": "Legacy requirement",
                    "effective_at": _at(13).isoformat(),
                    "duration_business_days": 2,
                    "required_roles": {"engineer": 1.0},
                }
            }
        )
    )

    assert result.warnings == [LEGACY_REQUIRED_ROLES_WARNING]

    query_result = service.handle_query(
        QueryEnvelope(
            query=QuerySchedule(
                project_id=project_id,
                as_of=_at(13),
                now=_at(13),
            )
        )
    )
    assert "warnings" not in query_result.data
    assert "cost_warnings" not in query_result.data


def test_command_and_query_result_wrappers_use_documented_warning_shape():
    command_result = result_models.CommandResult.model_validate(
        {
            "command_id": "00000000-0000-4000-8000-000000000301",
            "ok": True,
            "entity_ids": {"project_id": "project-alpha"},
            "warnings": [
                {
                    "code": "legacy_required_roles",
                    "message": "required_roles was accepted in transition mode.",
                    "severity": "warning",
                    "details": {"mode": "dual_write_warn"},
                }
            ],
        }
    )
    query_result = result_models.QueryResult.model_validate(
        {
            "query_id": "00000000-0000-4000-8000-000000000302",
            "ok": True,
            "data": {"project_id": "project-alpha"},
            "warnings": [
                {
                    "code": "max_iterations_reached",
                    "message": "Resource schedule did not converge.",
                    "severity": "warning",
                    "details": {"max_iterations": 2},
                }
            ],
        }
    )

    assert command_result.warnings[0]["severity"] == "warning"
    assert query_result.warnings[0]["code"] == "max_iterations_reached"


def test_error_result_wrappers_use_single_structured_error():
    command_error_model = _result_model("CommandErrorResult")
    query_error_model = _result_model("QueryErrorResult")
    error = {
        "code": "validation_error",
        "message": "Payload validation failed.",
        "details": {},
        "validation_errors": [
            {
                "loc": ["command", "operations", 0, "role_id"],
                "msg": "Field required",
                "type": "missing",
                "ctx": {},
            }
        ],
    }

    command_error = command_error_model.model_validate(
        {
            "command_id": "00000000-0000-4000-8000-000000000303",
            "ok": False,
            "error": error,
            "warnings": [],
        }
    )
    query_error = query_error_model.model_validate(
        {
            "query_id": "00000000-0000-4000-8000-000000000304",
            "ok": False,
            "error": error,
            "warnings": [],
        }
    )

    assert command_error.ok is False
    assert query_error.error.validation_errors[0].type == "missing"


@pytest.mark.parametrize(
    "case",
    [
        ApiCase(
            "set_process_status_done_infers_finished_at",
            _command_payload(
                {
                    "action": "set_process_status",
                    "project_id": "project-alpha",
                    "process_symbol": "build-api",
                    "status": "done",
                    "edit_at": _at(13, 17).isoformat(),
                }
            ),
        ),
        ApiCase(
            "set_process_status_done_explicit_finished_at",
            _command_payload(
                {
                    "action": "set_process_status",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "status": "done",
                    "edit_at": _at(13, 17).isoformat(),
                    "finished_at": _at(13, 16).isoformat(),
                }
            ),
        ),
        ApiCase(
            "reopen_done_input_clears_finished_at_in_service",
            _command_payload(
                {
                    "action": "set_process_status",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "status": "in_progress",
                    "edit_at": _at(14).isoformat(),
                    "finished_at": None,
                }
            ),
        ),
        ApiCase(
            "set_project_due_at",
            _command_payload(
                {
                    "action": "set_project_due_at",
                    "project_id": "project-alpha",
                    "due_at": _at(30, 17).isoformat(),
                    "edit_at": _at(13).isoformat(),
                }
            ),
        ),
        ApiCase(
            "clear_project_due_at",
            _command_payload(
                {
                    "action": "clear_project_due_at",
                    "project_id": "project-alpha",
                    "edit_at": _at(14).isoformat(),
                }
            ),
        ),
        ApiCase(
            "set_process_due_at_clear",
            _command_payload(
                {
                    "action": "set_process_due_at",
                    "project_id": "project-alpha",
                    "process_symbol": "build-api",
                    "due_at": None,
                    "edit_at": _at(14).isoformat(),
                }
            ),
        ),
        ApiCase(
            "add_blocker",
            _command_payload(
                {
                    "action": "add_blocker",
                    "project_id": "project-alpha",
                    "process_symbol": "build-api",
                    "summary": "Waiting on vendor credentials",
                    "details": "Credential request is pending approval.",
                    "severity": "blocking",
                    "created_at": _at(13, 10).isoformat(),
                }
            ),
        ),
        ApiCase(
            "resolve_blocker",
            _command_payload(
                {
                    "action": "resolve_blocker",
                    "project_id": "project-alpha",
                    "blocker_id": "blocker-vendor",
                    "resolved_at": _at(14, 11).isoformat(),
                    "resolution": "Credentials provisioned.",
                }
            ),
        ),
        ApiCase(
            "rename_process",
            _command_payload(
                {
                    "action": "rename_process",
                    "project_id": "project-alpha",
                    "process_symbol": "build-api",
                    "new_symbol": "build-service-api",
                    "edit_at": _at(14).isoformat(),
                    "keep_old_symbol_as_alias": True,
                }
            ),
        ),
        ApiCase(
            "add_process_aliases",
            _command_payload(
                {
                    "action": "add_process_aliases",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "aliases": ["api", "service-api"],
                    "edit_at": _at(14).isoformat(),
                }
            ),
        ),
    ],
    ids=lambda case: case.name,
)
def test_lifecycle_due_blocker_and_alias_command_payloads_round_trip(case):
    envelope = CommandEnvelope.model_validate(case.payload)

    decoded = CommandEnvelope.model_validate_json(envelope.model_dump_json())

    assert decoded.command.action == case.payload["command"]["action"]


@pytest.mark.parametrize(
    "case",
    [
        ApiCase(
            "status_finished_at_naive",
            _command_payload(
                {
                    "action": "set_process_status",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "status": "done",
                    "edit_at": _at(13, 17).isoformat(),
                    "finished_at": "2026-05-13T16:00:00",
                }
            ),
        ),
        ApiCase(
            "status_finished_at_after_edit",
            _command_payload(
                {
                    "action": "set_process_status",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "status": "done",
                    "edit_at": _at(13, 17).isoformat(),
                    "finished_at": _at(13, 18).isoformat(),
                }
            ),
        ),
        ApiCase(
            "blocked_is_not_a_stored_lifecycle_status",
            _command_payload(
                {
                    "action": "set_process_status",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "status": "blocked",
                    "edit_at": _at(13, 17).isoformat(),
                }
            ),
        ),
        ApiCase(
            "set_project_due_at_naive_due_at",
            _command_payload(
                {
                    "action": "set_project_due_at",
                    "project_id": "project-alpha",
                    "due_at": "2026-05-30T17:00:00",
                    "edit_at": _at(13).isoformat(),
                }
            ),
        ),
        ApiCase(
            "set_project_due_at_naive_edit_at",
            _command_payload(
                {
                    "action": "set_project_due_at",
                    "project_id": "project-alpha",
                    "due_at": _at(30, 17).isoformat(),
                    "edit_at": "2026-05-13T09:00:00",
                }
            ),
        ),
        ApiCase(
            "set_process_due_at_naive_due_at",
            _command_payload(
                {
                    "action": "set_process_due_at",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "due_at": "2026-05-30T17:00:00",
                    "edit_at": _at(13).isoformat(),
                }
            ),
        ),
        ApiCase(
            "set_process_due_at_naive_edit_at",
            _command_payload(
                {
                    "action": "set_process_due_at",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "due_at": _at(30, 17).isoformat(),
                    "edit_at": "2026-05-13T09:00:00",
                }
            ),
        ),
        ApiCase(
            "clear_project_due_at_naive_edit_at",
            _command_payload(
                {
                    "action": "clear_project_due_at",
                    "project_id": "project-alpha",
                    "edit_at": "2026-05-14T09:00:00",
                }
            ),
        ),
        ApiCase(
            "add_blocker_naive_created_at",
            _command_payload(
                {
                    "action": "add_blocker",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "summary": "Vendor credentials",
                    "created_at": "2026-05-13T10:00:00",
                }
            ),
        ),
        ApiCase(
            "add_blocker_invalid_severity",
            _command_payload(
                {
                    "action": "add_blocker",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "summary": "Vendor credentials",
                    "severity": "fatal",
                    "created_at": _at(13, 10).isoformat(),
                }
            ),
        ),
        ApiCase(
            "resolve_blocker_naive_resolved_at",
            _command_payload(
                {
                    "action": "resolve_blocker",
                    "project_id": "project-alpha",
                    "blocker_id": "blocker-vendor",
                    "resolved_at": "2026-05-14T11:00:00",
                }
            ),
        ),
        ApiCase(
            "add_alias_duplicate",
            _command_payload(
                {
                    "action": "add_process_aliases",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "aliases": ["api", "api"],
                    "edit_at": _at(14).isoformat(),
                }
            ),
        ),
        ApiCase(
            "add_alias_empty",
            _command_payload(
                {
                    "action": "add_process_aliases",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "aliases": [""],
                    "edit_at": _at(14).isoformat(),
                }
            ),
        ),
        ApiCase(
            "new_command_extra_field",
            _command_payload(
                {
                    "action": "set_project_due_at",
                    "project_id": "project-alpha",
                    "due_at": _at(30, 17).isoformat(),
                    "edit_at": _at(13).isoformat(),
                    "unexpected": True,
                }
            ),
        ),
    ],
    ids=lambda case: case.name,
)
def test_lifecycle_due_blocker_and_alias_commands_reject_invalid_inputs(case):
    with pytest.raises(ValidationError):
        CommandEnvelope.model_validate(case.payload)


@pytest.mark.parametrize(
    "field_name, payload",
    [
        (
            "edit_at",
            _command_payload(
                {
                    "action": "set_process_due_at",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "due_at": _at(30, 17).isoformat(),
                    "edit_at": "2026-05-13T09:00:00",
                }
            ),
        ),
        (
            "due_at",
            _command_payload(
                {
                    "action": "set_process_due_at",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "due_at": "2026-05-30T17:00:00",
                    "edit_at": _at(13).isoformat(),
                }
            ),
        ),
        (
            "edit_at",
            _command_payload(
                {
                    "action": "set_project_due_at",
                    "project_id": "project-alpha",
                    "due_at": _at(30, 17).isoformat(),
                    "edit_at": "2026-05-13T09:00:00",
                }
            ),
        ),
        (
            "edit_at",
            _command_payload(
                {
                    "action": "clear_project_due_at",
                    "project_id": "project-alpha",
                    "edit_at": "2026-05-14T09:00:00",
                }
            ),
        ),
        (
            "created_at",
            _command_payload(
                {
                    "action": "add_blocker",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "summary": "Vendor credentials",
                    "created_at": "2026-05-13T10:00:00",
                }
            ),
        ),
    ],
)
def test_new_command_moments_reject_naive_datetimes(field_name, payload):
    _assert_validation_error_locates(
        payload,
        envelope=CommandEnvelope,
        field_name=field_name,
    )


@pytest.mark.parametrize(
    "case",
    [
        ApiCase(
            "lifecycle_extra",
            _command_payload(
                {
                    "action": "set_process_status",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "status": "paused",
                    "edit_at": _at(13).isoformat(),
                    "unexpected": True,
                }
            ),
        ),
        ApiCase(
            "due_extra",
            _command_payload(
                {
                    "action": "set_process_due_at",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "due_at": _at(30, 17).isoformat(),
                    "edit_at": _at(13).isoformat(),
                    "unexpected": True,
                }
            ),
        ),
        ApiCase(
            "blocker_extra",
            _command_payload(
                {
                    "action": "add_blocker",
                    "project_id": "project-alpha",
                    "process_id": "process-api",
                    "summary": "Vendor credentials",
                    "created_at": _at(13, 10).isoformat(),
                    "unexpected": True,
                }
            ),
        ),
        ApiCase(
            "batch_top_level_extra",
            _command_payload(
                {
                    "action": "batch_update_process_graph",
                    "project_id": "project-alpha",
                    "edit_at": _at(14).isoformat(),
                    "operations": [
                        {
                            "action": "add_dependency",
                            "predecessor_process_symbol": "design",
                            "successor_process_symbol": "build",
                        }
                    ],
                    "unexpected": True,
                }
            ),
        ),
        ApiCase(
            "batch_operation_extra",
            _command_payload(
                {
                    "action": "batch_update_process_graph",
                    "project_id": "project-alpha",
                    "edit_at": _at(14).isoformat(),
                    "operations": [
                        {
                            "action": "add_dependency",
                            "predecessor_process_symbol": "design",
                            "successor_process_symbol": "build",
                            "unexpected": True,
                        }
                    ],
                }
            ),
        ),
        ApiCase(
            "topology_replace_extra",
            _command_payload(
                {
                    "action": "replace_process_with_subgraph",
                    "project_id": "project-alpha",
                    "edit_at": _at(14).isoformat(),
                    "process_symbol": "legacy-api",
                    "processes": [
                        {
                            "process_symbol": "service-api",
                            "name": "Service API",
                            "duration_hours": 16,
                        }
                    ],
                    "dependencies": [],
                    "root_symbols": ["service-api"],
                    "leaf_symbols": ["service-api"],
                    "unexpected": True,
                }
            ),
        ),
        ApiCase(
            "topology_collapse_nested_extra",
            _command_payload(
                {
                    "action": "collapse_subgraph",
                    "project_id": "project-alpha",
                    "edit_at": _at(14).isoformat(),
                    "process_symbols": ["api-contract", "api-implementation"],
                    "new_process": {
                        "process_symbol": "api-delivery",
                        "name": "API Delivery",
                        "duration_hours": 24,
                        "unexpected": True,
                    },
                }
            ),
        ),
    ],
    ids=lambda case: case.name,
)
def test_new_command_envelopes_reject_strict_extra_fields(case):
    _assert_validation_error_locates(
        case.payload,
        envelope=CommandEnvelope,
        field_name="unexpected",
    )


@pytest.mark.parametrize(
    "case",
    [
        ApiCase(
            "query_blockers",
            _query_payload(
                {
                    "action": "query_blockers",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "process_symbols": ["build-api"],
                    "include_resolved": True,
                }
            ),
        ),
        ApiCase(
            "query_due_date_history_project_total",
            _query_payload(
                {
                    "action": "query_due_date_history",
                    "project_id": "project-alpha",
                    "as_of": _at(20).isoformat(),
                    "include_project_total": True,
                }
            ),
        ),
        ApiCase(
            "query_due_date_history_topology_scope",
            _query_payload(
                {
                    "action": "query_due_date_history",
                    "project_id": "project-alpha",
                    "as_of": _at(20).isoformat(),
                    "scope": {
                        "type": "topo_filter",
                        "root_process_symbols": ["build-api"],
                        "direction": "ancestors_and_descendants",
                    },
                }
            ),
        ),
        ApiCase(
            "query_process_graph_dependency_only",
            _query_payload(
                {
                    "action": "query_process_graph",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                }
            ),
        ),
        ApiCase(
            "query_process_graph_resource_aware",
            _query_payload(
                {
                    "action": "query_process_graph",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "include_resource_fields": True,
                    "horizon_starts_at": _at(13, 0).isoformat(),
                    "horizon_ends_at": _at(20, 0).isoformat(),
                    "include_allocation_slices": True,
                    "blocked_policy": "include_as_zero_capacity",
                }
            ),
        ),
        ApiCase(
            "query_costs_with_filters_and_group_by",
            _query_payload(
                {
                    "action": "query_costs",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "horizon_starts_at": _at(13, 0).isoformat(),
                    "horizon_ends_at": _at(20, 0).isoformat(),
                    "scope": {
                        "type": "target_process",
                        "process_symbol": "build-api",
                    },
                    "resource_ids": ["resource-ada"],
                    "role_ids": ["role-engineer"],
                    "currency": "USD",
                    "group_by": ["resource", "process", "time"],
                }
            ),
        ),
    ],
    ids=lambda case: case.name,
)
def test_new_query_payloads_round_trip(case):
    envelope = QueryEnvelope.model_validate(case.payload)

    decoded = QueryEnvelope.model_validate_json(envelope.model_dump_json())

    assert decoded.query.action == case.payload["query"]["action"]


@pytest.mark.parametrize(
    "case",
    [
        ApiCase(
            "query_blockers_naive_as_of",
            _query_payload(
                {
                    "action": "query_blockers",
                    "project_id": "project-alpha",
                    "as_of": "2026-05-13T12:00:00",
                }
            ),
        ),
        ApiCase(
            "query_due_history_naive_as_of",
            _query_payload(
                {
                    "action": "query_due_date_history",
                    "project_id": "project-alpha",
                    "as_of": "2026-05-20T09:00:00",
                }
            ),
        ),
        ApiCase(
            "query_process_graph_naive_now",
            _query_payload(
                {
                    "action": "query_process_graph",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": "2026-05-13T12:00:00",
                }
            ),
        ),
        ApiCase(
            "resource_schedule_naive_horizon_starts_at",
            _query_payload(
                {
                    "action": "query_resource_schedule",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "horizon_starts_at": "2026-05-13T00:00:00",
                    "horizon_ends_at": _at(20, 0).isoformat(),
                }
            ),
        ),
        ApiCase(
            "resource_schedule_naive_horizon_ends_at",
            _query_payload(
                {
                    "action": "query_resource_schedule",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "horizon_starts_at": _at(13, 0).isoformat(),
                    "horizon_ends_at": "2026-05-20T00:00:00",
                }
            ),
        ),
        ApiCase(
            "dependency_graph_rejects_resource_horizon",
            _query_payload(
                {
                    "action": "query_process_graph",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "horizon_starts_at": _at(13, 0).isoformat(),
                    "horizon_ends_at": _at(20, 0).isoformat(),
                }
            ),
        ),
        ApiCase(
            "dependency_graph_rejects_allocation_slices",
            _query_payload(
                {
                    "action": "query_process_graph",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "include_resource_fields": False,
                    "include_allocation_slices": True,
                }
            ),
        ),
        ApiCase(
            "resource_graph_requires_horizon",
            _query_payload(
                {
                    "action": "query_process_graph",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "include_resource_fields": True,
                }
            ),
        ),
        ApiCase(
            "costs_reject_empty_resource_filter",
            _query_payload(
                {
                    "action": "query_costs",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "horizon_starts_at": _at(13, 0).isoformat(),
                    "horizon_ends_at": _at(20, 0).isoformat(),
                    "resource_ids": [],
                }
            ),
        ),
        ApiCase(
            "costs_reject_target_alias_conflict",
            _query_payload(
                {
                    "action": "query_costs",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "horizon_starts_at": _at(13, 0).isoformat(),
                    "horizon_ends_at": _at(20, 0).isoformat(),
                    "scope": {"type": "project"},
                    "target_process_id": "process-api",
                }
            ),
        ),
        ApiCase(
            "new_query_extra_field",
            _query_payload(
                {
                    "action": "query_costs",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "horizon_starts_at": _at(13, 0).isoformat(),
                    "horizon_ends_at": _at(20, 0).isoformat(),
                    "unexpected": True,
                }
            ),
        ),
    ],
    ids=lambda case: case.name,
)
def test_new_query_payloads_reject_invalid_inputs(case):
    with pytest.raises(ValidationError):
        QueryEnvelope.model_validate(case.payload)


@pytest.mark.parametrize(
    "field_name, payload",
    [
        (
            "as_of",
            _query_payload(
                {
                    "action": "query_blockers",
                    "project_id": "project-alpha",
                    "as_of": "2026-05-13T12:00:00",
                }
            ),
        ),
        (
            "now",
            _query_payload(
                {
                    "action": "query_process_graph",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": "2026-05-13T12:00:00",
                }
            ),
        ),
        (
            "horizon_starts_at",
            _query_payload(
                {
                    "action": "query_resource_schedule",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "horizon_starts_at": "2026-05-13T00:00:00",
                    "horizon_ends_at": _at(20, 0).isoformat(),
                }
            ),
        ),
        (
            "horizon_ends_at",
            _query_payload(
                {
                    "action": "query_resource_schedule",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "horizon_starts_at": _at(13, 0).isoformat(),
                    "horizon_ends_at": "2026-05-20T00:00:00",
                }
            ),
        ),
    ],
)
def test_new_query_moments_reject_naive_datetimes(field_name, payload):
    _assert_validation_error_locates(
        payload,
        envelope=QueryEnvelope,
        field_name=field_name,
    )


@pytest.mark.parametrize(
    "case",
    [
        ApiCase(
            "work_window_graph_extra",
            _query_payload(
                {
                    "action": "query_process_graph",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "unexpected": True,
                }
            ),
        ),
        ApiCase(
            "graph_resource_extra",
            _query_payload(
                {
                    "action": "query_process_graph",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "include_resource_fields": True,
                    "horizon_starts_at": _at(13, 0).isoformat(),
                    "horizon_ends_at": _at(20, 0).isoformat(),
                    "unexpected": True,
                }
            ),
        ),
        ApiCase(
            "cost_extra",
            _query_payload(
                {
                    "action": "query_costs",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "horizon_starts_at": _at(13, 0).isoformat(),
                    "horizon_ends_at": _at(20, 0).isoformat(),
                    "unexpected": True,
                }
            ),
        ),
        ApiCase(
            "resource_schedule_extra",
            _query_payload(
                {
                    "action": "query_resource_schedule",
                    "project_id": "project-alpha",
                    "as_of": _at(13, 12).isoformat(),
                    "now": _at(13, 12).isoformat(),
                    "horizon_starts_at": _at(13, 0).isoformat(),
                    "horizon_ends_at": _at(20, 0).isoformat(),
                    "unexpected": True,
                }
            ),
        ),
        ApiCase(
            "topology_scope_extra",
            _query_payload(
                {
                    "action": "query_due_date_history",
                    "project_id": "project-alpha",
                    "as_of": _at(20).isoformat(),
                    "scope": {
                        "type": "topo_filter",
                        "root_process_symbols": ["build-api"],
                        "direction": "ancestors",
                        "unexpected": True,
                    },
                }
            ),
        ),
    ],
    ids=lambda case: case.name,
)
def test_new_query_envelopes_reject_strict_extra_fields(case):
    _assert_validation_error_locates(
        case.payload,
        envelope=QueryEnvelope,
        field_name="unexpected",
    )


def test_batch_graph_mutation_payload_accepts_every_operation_schema():
    payload = _command_payload(
        {
            "action": "batch_update_process_graph",
            "project_id": "project-alpha",
            "edit_at": _at(14).isoformat(),
            "idempotency_key": "graph-edit-1",
            "operations": [
                {
                    "operation_id": "op-add-edge",
                    "action": "add_dependency",
                    "predecessor_process_symbol": "design",
                    "successor_process_symbol": "build",
                    "dependency_type": "finish_to_start",
                    "edge_id": "edge-design-build",
                },
                {
                    "operation_id": "op-remove-edge",
                    "action": "remove_dependency",
                    "predecessor_process_symbol": "design",
                    "successor_process_symbol": "build",
                },
                {
                    "operation_id": "op-add-req",
                    "action": "add_role_requirement",
                    "process_symbol": "build",
                    "requirement": {
                        "requirement_id": "req-build-eng",
                        "role_id": "role-engineer",
                        "effort_hours": 12,
                        "min_allocation_hours_per_day": 1,
                        "max_allocation_hours_per_day": 6,
                        "required_resource_count": 2,
                        "allocation_policy": "split_allowed",
                    },
                },
                {
                    "operation_id": "op-remove-req",
                    "action": "remove_role_requirement",
                    "process_symbol": "build",
                    "requirement_id": "req-build-eng",
                },
                {
                    "operation_id": "op-upsert-resource",
                    "action": "upsert_resource",
                    "resource": {
                        "resource_id": "resource-ada",
                        "name": "Ada",
                        "role_ids": ["role-engineer"],
                        "calendar_id": "calendar-nyc",
                        "available_from_at": _at(13, 13).isoformat(),
                        "available_until_at": _at(20, 17).isoformat(),
                        "cost_rate": "125.00",
                        "cost_unit": "hour",
                        "cost_currency": "USD",
                        "active": True,
                    },
                },
                {
                    "operation_id": "op-set-roles",
                    "action": "set_resource_roles",
                    "resource_id": "resource-ada",
                    "role_ids": ["role-engineer", "role-reviewer"],
                },
                {
                    "operation_id": "op-set-calendar",
                    "action": "set_resource_calendar",
                    "resource_id": "resource-ada",
                    "calendar_id": "calendar-nyc",
                },
            ],
        }
    )

    envelope = CommandEnvelope.model_validate(payload)

    assert envelope.command.action == "batch_update_process_graph"
    assert [operation.action for operation in envelope.command.operations] == [
        "add_dependency",
        "remove_dependency",
        "add_role_requirement",
        "remove_role_requirement",
        "upsert_resource",
        "set_resource_roles",
        "set_resource_calendar",
    ]


def test_batch_operation_result_ids_are_objects_not_strings():
    result = result_models.CommandResult.model_validate(
        {
            "command_id": "00000000-0000-4000-8000-000000000305",
            "ok": True,
            "entity_ids": {
                "process_ids": ["process-build"],
                "edge_ids": ["edge-design-build"],
                "requirement_ids": ["req-build-eng"],
                "resource_ids": ["resource-ada"],
                "revision_ids": ["revision-build-2"],
                "operation_ids": [
                    {
                        "operation_index": 0,
                        "operation_id": "op-add-req",
                        "action": "add_role_requirement",
                        "status": "applied",
                        "revision_id": "revision-build-2",
                        "requirement_ids": ["req-build-eng"],
                        "edge_ids": [],
                        "alias_process_id": None,
                        "created_ids": {
                            "process_ids": [],
                            "edge_ids": [],
                            "requirement_ids": ["req-build-eng"],
                            "resource_ids": [],
                            "revision_ids": ["revision-build-2"],
                            "calendar_ids": [],
                            "blocker_ids": [],
                            "due_history_event_ids": [],
                            "retirement_event_ids": [],
                        },
                        "retired_ids": {
                            "process_ids": [],
                            "edge_ids": [],
                            "retirement_event_ids": [],
                        },
                        "removed_ids": {
                            "edge_ids": [],
                            "requirement_ids": [],
                            "calendar_exception_ids": [],
                        },
                        "matched_ids": {
                            "process_ids": [],
                            "edge_ids": [],
                            "requirement_ids": [],
                            "resource_ids": [],
                            "calendar_ids": [],
                            "revision_ids": [],
                        },
                        "candidate_only_ids": {"requirement_ids": []},
                        "no_op_reason": None,
                        "validation_reason": None,
                    }
                ],
            },
            "warnings": [],
        }
    )

    operation_result = result.entity_ids["operation_ids"][0]
    assert operation_result["operation_index"] == 0
    assert operation_result["created_ids"]["revision_ids"] == ["revision-build-2"]


@pytest.mark.parametrize(
    "case",
    [
        ApiCase(
            "replace_process_one_child_defaults_alias_target",
            _command_payload(
                {
                    "action": "replace_process_with_subgraph",
                    "project_id": "project-alpha",
                    "edit_at": _at(14).isoformat(),
                    "process_symbol": "legacy-api",
                    "processes": [
                        {
                            "process_symbol": "service-api",
                            "name": "Service API",
                            "duration_hours": 16,
                            "earliest_start_at": _at(15).isoformat(),
                            "due_at": _at(30).isoformat(),
                            "status": "planned",
                            "aliases": ["api-v2"],
                            "role_requirements": [
                                {
                                    "requirement_id": "req-service-api-eng",
                                    "role_id": "role-engineer",
                                    "effort_hours": 16,
                                }
                            ],
                        }
                    ],
                    "dependencies": [],
                    "root_symbols": ["service-api"],
                    "leaf_symbols": ["service-api"],
                    "preserve_parent_symbol_as_alias": True,
                }
            ),
        ),
        ApiCase(
            "replace_process_multi_child_explicit_alias_target",
            _command_payload(
                {
                    "action": "replace_process_with_subgraph",
                    "project_id": "project-alpha",
                    "edit_at": _at(14).isoformat(),
                    "process_id": "process-legacy-api",
                    "processes": [
                        {
                            "process_symbol": "api-contract",
                            "name": "API Contract",
                            "duration_hours": 8,
                            "role_requirements": [
                                {
                                    "role_id": "role-engineer",
                                    "effort_hours": 8,
                                }
                            ],
                        },
                        {
                            "process_symbol": "api-implementation",
                            "name": "API Implementation",
                            "duration_hours": 16,
                            "finished_at": None,
                        },
                    ],
                    "dependencies": [
                        {
                            "predecessor_symbol": "api-contract",
                            "successor_symbol": "api-implementation",
                            "dependency_type": "finish_to_start",
                            "edge_id": "edge-contract-implementation",
                        }
                    ],
                    "root_symbols": ["api-contract"],
                    "leaf_symbols": ["api-implementation"],
                    "parent_alias_target_symbol": "api-implementation",
                }
            ),
        ),
        ApiCase(
            "replace_process_multi_child_omits_roots_and_leaves",
            _command_payload(
                {
                    "action": "replace_process_with_subgraph",
                    "project_id": "project-alpha",
                    "edit_at": _at(14).isoformat(),
                    "process_id": "process-legacy-api",
                    "processes": [
                        {
                            "process_symbol": "api-contract",
                            "name": "API Contract",
                            "duration_hours": 8,
                        },
                        {
                            "process_symbol": "api-implementation",
                            "name": "API Implementation",
                            "duration_hours": 16,
                        },
                    ],
                    "dependencies": [
                        {
                            "predecessor_symbol": "api-contract",
                            "successor_symbol": "api-implementation",
                        }
                    ],
                    "parent_alias_target_symbol": "api-implementation",
                }
            ),
        ),
        ApiCase(
            "collapse_subgraph_with_replacement_process",
            _command_payload(
                {
                    "action": "collapse_subgraph",
                    "project_id": "project-alpha",
                    "edit_at": _at(14).isoformat(),
                    "process_symbols": ["api-contract", "api-implementation"],
                    "new_process": {
                        "process_symbol": "api-delivery",
                        "name": "API Delivery",
                        "duration_hours": 24,
                        "earliest_start_at": None,
                        "due_at": _at(30).isoformat(),
                        "status": "planned",
                        "aliases": ["api"],
                        "role_requirements": [
                            {
                                "requirement_id": "req-api-delivery-eng",
                                "role_id": "role-engineer",
                                "effort_hours": 24,
                                "required_resource_count": 1,
                            }
                        ],
                    },
                    "role_conflict_policy": "reject",
                }
            ),
        ),
    ],
    ids=lambda case: case.name,
)
def test_topology_rewrite_command_payloads_round_trip(case):
    envelope = CommandEnvelope.model_validate(case.payload)

    decoded = CommandEnvelope.model_validate_json(envelope.model_dump_json())

    assert decoded.command.action == case.payload["command"]["action"]


@pytest.mark.parametrize(
    "case",
    [
        ApiCase(
            "replace_multi_child_requires_alias_target_by_default",
            _command_payload(
                {
                    "action": "replace_process_with_subgraph",
                    "project_id": "project-alpha",
                    "edit_at": _at(14).isoformat(),
                    "process_symbol": "legacy-api",
                    "processes": [
                        {
                            "process_symbol": "api-contract",
                            "name": "API Contract",
                            "duration_hours": 8,
                        },
                        {
                            "process_symbol": "api-implementation",
                            "name": "API Implementation",
                            "duration_hours": 16,
                        },
                    ],
                    "dependencies": [],
                    "root_symbols": ["api-contract"],
                    "leaf_symbols": ["api-implementation"],
                }
            ),
        ),
        ApiCase(
            "replace_forbids_alias_target_when_not_preserving",
            _command_payload(
                {
                    "action": "replace_process_with_subgraph",
                    "project_id": "project-alpha",
                    "edit_at": _at(14).isoformat(),
                    "process_symbol": "legacy-api",
                    "processes": [
                        {
                            "process_symbol": "service-api",
                            "name": "Service API",
                            "duration_hours": 16,
                        }
                    ],
                    "dependencies": [],
                    "root_symbols": ["service-api"],
                    "leaf_symbols": ["service-api"],
                    "preserve_parent_symbol_as_alias": False,
                    "parent_alias_target_symbol": "service-api",
                }
            ),
        ),
        ApiCase(
            "replace_rejects_explicit_empty_roots_and_leaves",
            _command_payload(
                {
                    "action": "replace_process_with_subgraph",
                    "project_id": "project-alpha",
                    "edit_at": _at(14).isoformat(),
                    "process_symbol": "legacy-api",
                    "processes": [
                        {
                            "process_symbol": "service-api",
                            "name": "Service API",
                            "duration_hours": 16,
                        }
                    ],
                    "dependencies": [],
                    "root_symbols": [],
                    "leaf_symbols": [],
                    "preserve_parent_symbol_as_alias": True,
                }
            ),
        ),
        ApiCase(
            "collapse_rejects_duplicate_symbols",
            _command_payload(
                {
                    "action": "collapse_subgraph",
                    "project_id": "project-alpha",
                    "edit_at": _at(14).isoformat(),
                    "process_symbols": ["api-contract", "api-contract"],
                    "new_process": {
                        "process_symbol": "api-delivery",
                        "name": "API Delivery",
                    },
                }
            ),
        ),
    ],
    ids=lambda case: case.name,
)
def test_topology_rewrite_command_payloads_reject_invalid_inputs(case):
    with pytest.raises(ValidationError):
        CommandEnvelope.model_validate(case.payload)


def test_topology_results_expose_soft_retirement_and_alias_ids():
    result = result_models.CommandResult.model_validate(
        {
            "command_id": "00000000-0000-4000-8000-000000000306",
            "ok": True,
            "entity_ids": {
                "process_ids": ["process-api-contract", "process-api-impl"],
                "retired_process_ids": ["process-legacy-api"],
                "retirement_event_ids": ["retire-legacy-api"],
                "edge_ids": ["edge-contract-impl"],
                "retired_edge_ids": ["edge-old-api-ship"],
                "alias_process_id": "process-api-impl",
            },
            "warnings": [],
        }
    )

    assert result.entity_ids["retired_process_ids"] == ["process-legacy-api"]
    assert result.entity_ids["alias_process_id"] == "process-api-impl"

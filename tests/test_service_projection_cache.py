import datetime as dt
from collections.abc import Mapping
from zoneinfo import ZoneInfo

from projdash.service import service as service_module
from projdash.service.commands import (
    BatchCommandEnvelope,
    CommandEnvelope,
    CreateProject,
)
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
):
    return service.handle_command(CommandEnvelope.model_validate({"command": command}))


def _query(
    service: ProjectService,
    query: Mapping[str, object],
):
    result = service.handle_query(QueryEnvelope.model_validate({"query": query}))
    assert result.ok is True
    return result.data


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
    project_id = service.handle_command(
        CommandEnvelope(command=CreateProject(name="Cache", start_at=_at(13)))
    ).entity_ids["project_id"]
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
    as_of: str | None = None,
    now: str | None = None,
    include_allocation_slices: bool = True,
) -> dict[str, object]:
    return {
        "action": "query_resource_schedule",
        "project_id": project_id,
        "as_of": as_of or _iso(13, 12),
        "now": now or _iso(13, 12),
        "include_allocation_slices": include_allocation_slices,
    }


def _counting_scheduler(calls: list[dict[str, object]]):
    def scheduler(input_data: dict[str, object]) -> dict[str, object]:
        calls.append(input_data)
        process = input_data["processes"][0]
        requirement = input_data["role_requirements"][0]
        resource = input_data["resources"][0]
        starts_at = _at(13, 13)
        ends_at = _at(13, 15)
        include_slices = input_data["options"]["include_allocation_slices"]
        allocation_slices = []
        if include_slices:
            allocation_slices.append(
                {
                    "slice_id": "slice-cache",
                    "project_id": input_data["project_id"],
                    "process_id": process["process_id"],
                    "requirement_id": requirement["requirement_id"],
                    "role_id": requirement["role_id"],
                    "resource_id": resource["resource_id"],
                    "starts_at": starts_at,
                    "ends_at": ends_at,
                    "effort_hours": 2,
                    "capacity_hours": 2,
                    "iteration": 1,
                }
            )
        return {
            "project_id": input_data["project_id"],
            "as_of": input_data["as_of"],
            "now": input_data["now"],
            "horizon_starts_at": input_data["options"]["horizon_starts_at"],
            "horizon_ends_at": input_data["options"]["horizon_ends_at"],
            "planning_granularity": input_data["options"]["planning_granularity"],
            "processes": [
                {
                    "process_id": process["process_id"],
                    "name": process["name"],
                    "ready_at": starts_at,
                    "starts_at": starts_at,
                    "ends_at": ends_at,
                    "resource_es_at": starts_at,
                    "resource_ef_at": ends_at,
                    "resource_ls_at": starts_at,
                    "resource_lf_at": ends_at,
                    "inferred_duration_hours": 2,
                    "resource_delay_hours": 0,
                    "resource_slack_hours": 0,
                    "allocation_state": "allocated",
                }
            ],
            "allocation_slices": allocation_slices,
            "critical_path_process_ids": [process["process_id"]],
            "converged": True,
            "iteration_count": 1,
        }

    return scheduler


def test_resource_projection_cache_reuses_across_query_family():
    scheduler_calls: list[dict[str, object]] = []
    service = ProjectService(
        InMemoryProjectRepository(),
        resource_scheduler=_counting_scheduler(scheduler_calls),
    )
    project_id, _role_id, _calendar_id, _resource_id, _process_id = (
        _seed_allocatable_project(service)
    )
    common = {
        "project_id": project_id,
        "as_of": _iso(13, 12),
        "now": _iso(13, 12),
    }

    _query(
        service,
        {
            "action": "query_process_graph",
            **common,
            "include_resource_fields": True,
            "include_allocation_slices": True,
        },
    )
    _query(
        service,
        {
            "action": "query_resource_schedule",
            **common,
            "include_allocation_slices": True,
        },
    )
    _query(service, {"action": "query_utilization", **common})
    _query(service, {"action": "query_costs", **common})
    _query(service, {"action": "query_agent_context", **common})

    assert len(scheduler_calls) == 1


def test_resource_projection_cache_key_uses_canonical_aware_datetimes():
    scheduler_calls: list[dict[str, object]] = []
    service = ProjectService(
        InMemoryProjectRepository(),
        resource_scheduler=_counting_scheduler(scheduler_calls),
    )
    project_id, _role_id, _calendar_id, _resource_id, _process_id = (
        _seed_allocatable_project(service)
    )
    as_of = _at(13, 12)
    amsterdam = ZoneInfo("Europe/Amsterdam")

    first = _query(
        service,
        _resource_schedule_query(
            project_id,
            as_of=as_of.isoformat(),
            now=as_of.isoformat(),
        ),
    )
    second = _query(
        service,
        _resource_schedule_query(
            project_id,
            as_of=as_of.astimezone(amsterdam).isoformat(),
            now=as_of.astimezone(amsterdam).isoformat(),
        ),
    )

    assert len(scheduler_calls) == 1
    assert first["as_of"] == as_of.isoformat()
    assert second["as_of"] == as_of.astimezone(amsterdam).isoformat()


def test_resource_projection_cache_stores_full_projection_for_no_slice_callers():
    scheduler_calls: list[dict[str, object]] = []
    service = ProjectService(
        InMemoryProjectRepository(),
        resource_scheduler=_counting_scheduler(scheduler_calls),
    )
    project_id, _role_id, _calendar_id, _resource_id, _process_id = (
        _seed_allocatable_project(service)
    )

    without_slices = _query(
        service,
        _resource_schedule_query(
            project_id,
            include_allocation_slices=False,
        ),
    )
    with_slices = _query(
        service,
        _resource_schedule_query(
            project_id,
            include_allocation_slices=True,
        ),
    )

    assert len(scheduler_calls) == 1
    assert scheduler_calls[0]["options"]["include_allocation_slices"] is True
    assert without_slices["allocation_slices"] == []
    assert with_slices["allocation_slices"]


def test_resource_projection_cache_is_invalidated_after_successful_command():
    scheduler_calls: list[dict[str, object]] = []
    service = ProjectService(
        InMemoryProjectRepository(),
        resource_scheduler=_counting_scheduler(scheduler_calls),
    )
    project_id, role_id, _calendar_id, resource_id, process_id = (
        _seed_allocatable_project(service)
    )

    _query(service, _resource_schedule_query(project_id))
    _query(service, _resource_schedule_query(project_id))
    assert len(scheduler_calls) == 1

    result = _handle(
        service,
        {
            "action": "upsert_process_role_pin",
            "project_id": project_id,
            "process_id": process_id,
            "requirement_id": "req-api-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "pinned_at": _iso(13, 12),
            "forecast_finish_at": _iso(13, 15),
            "updated_at": _iso(13, 12),
        },
    )
    assert result.ok is True

    _query(service, _resource_schedule_query(project_id))
    assert len(scheduler_calls) == 2


def test_dependency_projection_cache_reuses_schedule_computation(monkeypatch):
    schedule_calls = []
    original_compute_schedule = service_module.compute_schedule

    def counting_compute_schedule(schedule_input, now):
        schedule_calls.append((schedule_input.project_id, now))
        return original_compute_schedule(schedule_input, now)

    monkeypatch.setattr(
        service_module,
        "compute_schedule",
        counting_compute_schedule,
    )
    service = ProjectService(InMemoryProjectRepository())
    project_id, _role_id, _calendar_id, _resource_id, _process_id = (
        _seed_allocatable_project(service)
    )
    common = {
        "project_id": project_id,
        "as_of": _iso(13, 12),
        "now": _iso(13, 12),
    }

    _query(service, {"action": "query_schedule", **common})
    _query(service, {"action": "query_critical_path", **common})
    _query(service, {"action": "query_process_graph", **common})

    assert len(schedule_calls) == 1


def test_projection_cache_is_invalidated_after_successful_batch(monkeypatch):
    schedule_calls = []
    original_compute_schedule = service_module.compute_schedule

    def counting_compute_schedule(schedule_input, now):
        schedule_calls.append((schedule_input.project_id, now))
        return original_compute_schedule(schedule_input, now)

    monkeypatch.setattr(
        service_module,
        "compute_schedule",
        counting_compute_schedule,
    )
    service = ProjectService(InMemoryProjectRepository())
    project_id, role_id, _calendar_id, _resource_id, _process_id = (
        _seed_allocatable_project(service)
    )
    query = {
        "action": "query_schedule",
        "project_id": project_id,
        "as_of": _iso(13, 12),
        "now": _iso(13, 12),
    }

    _query(service, query)
    _query(service, query)
    assert len(schedule_calls) == 1

    results = service.handle_batch(
        BatchCommandEnvelope.model_validate(
            {
                "commands": [
                    {
                        "command": {
                            "action": "upsert_process_revision",
                            "project_id": project_id,
                            "process_id": "process-api",
                            "name": "Build API",
                            "effective_at": _iso(13, 11),
                            "duration_business_days": 2,
                            "role_requirements": [
                                {
                                    "requirement_id": "req-api-eng",
                                    "role_id": role_id,
                                    "effort_hours": 16,
                                }
                            ],
                        }
                    }
                ]
            }
        )
    )
    assert [result.ok for result in results] == [True]

    _query(service, query)
    assert len(schedule_calls) == 2

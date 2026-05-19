import datetime as dt
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from projdash.service.commands import CommandEnvelope
from projdash.service.queries import QueryEnvelope
from projdash.service.repository import InMemoryProjectRepository
from projdash.service.service import ProjectService

UTC = dt.UTC
NYC = dt.timezone(dt.timedelta(hours=-4))


def _at(day: int, hour: int = 9, tz: dt.tzinfo = UTC) -> dt.datetime:
    return dt.datetime(2026, 5, day, hour, tzinfo=tz)


def _iso(day: int, hour: int = 9, tz: dt.tzinfo = UTC) -> str:
    return _at(day, hour, tz).isoformat()


def _handle(service: ProjectService, command: Mapping[str, Any]) -> dict[str, str]:
    result = service.handle_command(CommandEnvelope.model_validate({"command": command}))
    assert result.ok is True
    return result.entity_ids


def _query(service: ProjectService, query: Mapping[str, Any]) -> dict[str, Any]:
    result = service.handle_query(QueryEnvelope.model_validate({"query": query}))
    assert result.ok is True
    assert result.warnings == []
    return result.data


def _query_result(service: ProjectService, query: Mapping[str, Any]):
    return service.handle_query(QueryEnvelope.model_validate({"query": query}))


def _create_project(service: ProjectService) -> str:
    ids = _handle(
        service,
        {
            "action": "create_project",
            "name": "Resource Query Contract",
            "start_at": _iso(13, 9, NYC),
        },
    )
    return ids["project_id"]


def test_upsert_process_revision_respects_process_symbol_identity():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)

    created = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_symbol": "design",
            "name": "Design",
            "effective_at": _iso(13, 9),
            "duration_business_days": 0,
        },
    )
    updated = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_symbol": "design",
            "name": "Design updated",
            "description": "Updated definition of design completion",
            "effective_at": _iso(13, 10),
            "duration_business_days": 0,
        },
    )
    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
        },
    )

    assert created["process_id"] == "design"
    assert updated["process_id"] == "design"
    assert graph["nodes"][0]["process_symbol"] == "design"
    assert graph["nodes"][0]["name"] == "Design updated"
    assert graph["nodes"][0]["description"] == "Updated definition of design completion"


def test_staked_resource_ids_pin_resource_schedule_allocation():
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
    )["role_id"]
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
    )["calendar_id"]
    for resource_id, name in (
        ("resource-ada", "Ada"),
        ("resource-grace", "Grace"),
    ):
        _handle(
            service,
            {
                "action": "upsert_resource",
                "project_id": project_id,
                "resource_id": resource_id,
                "name": name,
                "role_ids": [role_id],
                "calendar_id": calendar_id,
                "available_from_at": _iso(13, 9),
                "cost_rate": "100",
                "cost_unit": "hour",
            },
        )
    _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-build",
            "name": "Build",
            "effective_at": _iso(13, 9),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-build",
                    "role_id": role_id,
                    "effort_hours": 8,
                }
            ],
            "staked_resource_ids": ["resource-grace"],
        },
    )

    schedule = _query(
        service,
        {
            "action": "query_resource_schedule",
            "project_id": project_id,
            "as_of": _iso(13, 9),
            "now": _iso(13, 9),
            "include_allocation_slices": True,
        },
    )

    assert {
        slice_["resource_id"] for slice_ in schedule["allocation_slices"]
    } == {"resource-grace"}


def test_upsert_process_revision_auto_generates_unique_symbol_from_name():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)

    first = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "name": "Design API",
            "effective_at": _iso(13, 9),
            "duration_business_days": 0,
        },
    )
    second = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "name": "Design API",
            "effective_at": _iso(13, 10),
            "duration_business_days": 0,
        },
    )
    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
        },
    )

    symbols_by_process = {
        node["process_id"]: node["process_symbol"]
        for node in graph["nodes"]
    }
    assert symbols_by_process[first["process_id"]] == "design-api"
    assert symbols_by_process[second["process_id"]] == "design-api1"


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


def _seed_dependency_project(service: ProjectService) -> dict[str, str]:
    project_id = _create_project(service)
    design_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-design",
            "name": "Design API",
            "effective_at": _iso(13, 9, NYC),
            "duration_business_days": 1,
        },
    )["process_id"]
    build_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-build",
            "name": "Build API",
            "effective_at": _iso(13, 9, NYC),
            "duration_business_days": 2,
            "dependencies": [design_id],
        },
    )["process_id"]
    _handle(
        service,
        {
            "action": "add_blocker",
            "project_id": project_id,
            "process_id": build_id,
            "summary": "Vendor security review pending",
            "details": "External sign-off gates implementation.",
            "severity": "blocking",
            "created_at": _iso(14, 10, NYC),
        },
    )
    return {
        "project_id": project_id,
        "design_id": design_id,
        "build_id": build_id,
    }


def _seed_resource_project(service: ProjectService) -> dict[str, str]:
    project_id = _create_project(service)
    engineer_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    )["role_id"]
    designer_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-designer",
            "name": "Designer",
        },
    )["role_id"]
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
    )["calendar_id"]
    engineer_resource_id = _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": "resource-ada",
            "name": "Ada",
            "role_ids": [engineer_id],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13, 13),
            "cost_rate": "125.00",
            "cost_unit": "hour",
            "cost_currency": "USD",
        },
    )["resource_id"]
    design_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-design",
            "name": "Design API",
            "effective_at": _iso(13, 9),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-design-eng",
                    "role_id": engineer_id,
                    "effort_hours": 4,
                }
            ],
        },
    )["process_id"]
    build_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-build",
            "name": "Build API",
            "effective_at": _iso(13, 9),
            "duration_business_days": 1,
            "dependencies": [design_id],
            "role_requirements": [
                {
                    "requirement_id": "req-build-eng",
                    "role_id": engineer_id,
                    "effort_hours": 8,
                }
            ],
        },
    )["process_id"]
    brand_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-brand",
            "name": "Brand Review",
            "effective_at": _iso(13, 9),
            "duration_business_days": 1,
            "role_requirements": [],
        },
    )["process_id"]
    return {
        "project_id": project_id,
        "engineer_id": engineer_id,
        "designer_id": designer_id,
        "calendar_id": calendar_id,
        "engineer_resource_id": engineer_resource_id,
        "design_id": design_id,
        "build_id": build_id,
        "brand_id": brand_id,
    }


def _seed_cost_unit_project(service: ProjectService) -> dict[str, str]:
    project_id = _handle(
        service,
        {
            "action": "create_project",
            "name": "Cost Unit Contract",
            "start_at": _iso(18, 9, NYC),
            "default_currency": "USD",
        },
    )["project_id"]
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-cost-units",
            "name": "Cost Unit Weekdays",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    )["calendar_id"]
    ids = {"project_id": project_id, "calendar_id": calendar_id}
    cases = [
        ("hour", "role-hourly", "resource-hourly", "process-hourly", "80.00", 8),
        ("day", "role-daily", "resource-daily", "process-daily", "400.00", 8),
        ("week", "role-weekly", "resource-weekly", "process-weekly", "2000.00", 40),
        ("fixed", "role-fixed", "resource-fixed", "process-fixed", "300.00", 6),
    ]
    for cost_unit, role_id, resource_id, process_id, cost_rate, effort_hours in cases:
        ids[f"{cost_unit}_role_id"] = _handle(
            service,
            {
                "action": "create_role",
                "project_id": project_id,
                "role_id": role_id,
                "name": f"{cost_unit.title()} Role",
            },
        )["role_id"]
        ids[f"{cost_unit}_resource_id"] = _handle(
            service,
            {
                "action": "upsert_resource",
                "project_id": project_id,
                "resource_id": resource_id,
                "name": f"{cost_unit.title()} Resource",
                "role_ids": [ids[f"{cost_unit}_role_id"]],
                "calendar_id": calendar_id,
                "available_from_at": _iso(18, 13),
                "cost_rate": cost_rate,
                "cost_unit": cost_unit,
                "cost_currency": "USD",
            },
        )["resource_id"]
        ids[f"{cost_unit}_process_id"] = _handle(
            service,
            {
                "action": "upsert_process_revision",
                "project_id": project_id,
                "process_id": process_id,
                "name": f"{cost_unit.title()} Work",
                "effective_at": _iso(18, 9),
                "duration_business_days": 5 if cost_unit == "week" else 1,
                "role_requirements": [
                    {
                        "requirement_id": f"req-{cost_unit}",
                        "role_id": ids[f"{cost_unit}_role_id"],
                        "effort_hours": effort_hours,
                    }
                ],
            },
        )["process_id"]
    return ids


def _seed_multi_role_resource_project(service: ProjectService) -> dict[str, str]:
    project_id = _create_project(service)
    dev_role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-dev",
            "name": "Developer",
        },
    )["role_id"]
    qa_role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-qa",
            "name": "QA",
        },
    )["role_id"]
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-shared",
            "name": "Shared Weekdays",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    )["calendar_id"]
    resource_id = _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": "resource-sam",
            "name": "Sam",
            "role_ids": [dev_role_id, qa_role_id],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13, 13),
            "cost_rate": "100.00",
            "cost_unit": "hour",
            "cost_currency": "USD",
        },
    )["resource_id"]
    for role_id, process_id, requirement_id in [
        (dev_role_id, "process-dev", "req-dev"),
        (qa_role_id, "process-qa", "req-qa"),
    ]:
        _handle(
            service,
            {
                "action": "upsert_process_revision",
                "project_id": project_id,
                "process_id": process_id,
                "name": process_id.replace("-", " ").title(),
                "effective_at": _iso(13, 9),
                "duration_business_days": 1,
                "role_requirements": [
                    {
                        "requirement_id": requirement_id,
                        "role_id": role_id,
                        "effort_hours": 4,
                    }
                ],
            },
        )
    return {
        "project_id": project_id,
        "dev_role_id": dev_role_id,
        "qa_role_id": qa_role_id,
        "resource_id": resource_id,
    }


def _resource_horizon() -> dict[str, str]:
    return {
        "as_of": _iso(13, 12),
        "now": _iso(13, 12),
    }


def _capacity_horizon() -> dict[str, str]:
    return {
        "horizon_starts_at": _iso(13, 13),
        "horizon_ends_at": _iso(20, 0),
    }


def _assert_no_nested_warnings(data: Mapping[str, Any]) -> None:
    assert "warnings" not in data
    assert "cost_warnings" not in data
    assert "resource_warnings" not in data


def _assert_cost_buckets_sum_to_total(costs: Mapping[str, Any]) -> None:
    bucket_total = sum(
        Decimal(bucket["cost_amount"]) for bucket in costs["time_series"]
    )
    assert bucket_total == Decimal(costs["total_cost"])


def _assert_cost_bucket_shape(bucket: Mapping[str, Any]) -> None:
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


def test_process_graph_dependency_only_contract_includes_cpm_status_and_blockers():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_dependency_project(service)

    data = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": ids["project_id"],
            "as_of": _iso(14, 12, NYC),
            "now": _iso(14, 12, NYC),
            "include_resource_fields": False,
        },
    )

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
    assert data["critical_path_process_ids"] == [
        ids["design_id"],
        ids["build_id"],
    ]

    nodes = {node["process_id"]: node for node in data["nodes"]}
    build = nodes[ids["build_id"]]
    assert set(build) == {
        "process_id",
        "process_symbol",
        "aliases",
        "name",
        "description",
        "duration_hours",
        "inferred_duration_hours",
        "earliest_start_at",
        "status",
        "started_at",
        "finished_at",
        "computed_status",
        "blocker_summary",
        "dependency_only",
        "resource_aware",
        "work_now_window",
        "late_risk_window",
    }
    assert build["status"] == "planned"
    assert build["description"] == ""
    assert build["computed_status"] == "blocked"
    assert build["blocker_summary"] == {
        "unresolved_count": 1,
        "blocking_count": 1,
        "blocker_ids": ["blocker-vendor-security-review-pending"],
    }
    assert build["resource_aware"] is None
    assert set(build["dependency_only"]) == {
        "es_at",
        "ef_at",
        "ls_at",
        "lf_at",
        "slack_hours",
        "criticality_label",
    }
    assert build["dependency_only"]["criticality_label"] == "critical"
    assert set(build["work_now_window"]) == {"starts_at", "ends_at", "active"}
    assert set(build["late_risk_window"]) == {"starts_at", "ends_at", "active"}

    edge = data["edges"][0]
    assert edge == {
        "edge_id": "edge-process-design-process-build",
        "project_id": ids["project_id"],
        "predecessor_process_id": ids["design_id"],
        "successor_process_id": ids["build_id"],
        "predecessor_process_symbol": "process-design",
        "successor_process_symbol": "process-build",
        "dependency_type": "finish_to_start",
    }


def test_process_graph_resource_aware_contract_keeps_allocations_out_of_edges():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_resource_project(service)

    data = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": ids["project_id"],
            **_resource_horizon(),
            "include_resource_fields": True,
            "include_allocation_slices": True,
            "planning_granularity": "hour",
        },
    )

    assert data["schedule_basis"] == "resource_aware"
    assert data["converged"] is True
    assert data["allocation_slices"]
    assert {
        allocation["process_id"]
        for allocation in data["allocation_slices"]
    } <= {ids["design_id"], ids["build_id"], ids["brand_id"]}

    for edge in data["edges"]:
        assert set(edge) == {
            "edge_id",
            "project_id",
            "predecessor_process_id",
            "successor_process_id",
            "predecessor_process_symbol",
            "successor_process_symbol",
            "dependency_type",
        }
        assert "resource_id" not in edge
        assert "requirement_id" not in edge

    build = next(
        node for node in data["nodes"] if node["process_id"] == ids["build_id"]
    )
    assert set(build["resource_aware"]) == {
        "ready_at",
        "starts_at",
        "ends_at",
        "es_at",
        "ef_at",
        "ls_at",
        "lf_at",
        "inferred_duration_hours",
        "resource_delay_hours",
        "slack_hours",
        "criticality_label",
        "allocation_state",
        "allocation_diagnostic",
    }
    assert build["resource_aware"]["allocation_state"] == "complete"
    assert build["resource_aware"]["ends_at"] < build["dependency_only"]["ef_at"]
    assert build["resource_aware"]["es_at"] == build["resource_aware"]["starts_at"]
    assert build["resource_aware"]["ef_at"] == build["resource_aware"]["ends_at"]
    assert build["resource_aware"]["lf_at"] >= build["resource_aware"]["ef_at"]
    assert build["resource_aware"]["inferred_duration_hours"] > 0
    assert build["inferred_duration_hours"] == (
        build["resource_aware"]["inferred_duration_hours"]
    )


def test_process_graph_resource_aware_status_uses_role_effort_windows():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _handle(
        service,
        {
            "action": "create_project",
            "name": "Resource Windows",
            "start_at": _iso(13, 9),
        },
    )["project_id"]
    role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role_eng",
            "name": "Engineer",
        },
    )["role_id"]
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-nyc",
            "name": "NYC Weekdays",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    )["calendar_id"]
    _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": "resource-ada",
            "name": "Ada",
            "role_ids": [role_id],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13, 13),
            "cost_rate": "0",
            "cost_unit": "hour",
        },
    )
    process_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-build",
            "name": "Build",
            "effective_at": _iso(13, 9),
            "duration_business_days": 0,
            "role_requirements": [
                {
                    "requirement_id": "req-build-eng",
                    "role_id": role_id,
                    "effort_hours": 16,
                }
            ],
        },
    )["process_id"]

    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
            "include_resource_fields": True,
            "planning_granularity": "hour",
        },
    )
    node = next(item for item in graph["nodes"] if item["process_id"] == process_id)

    assert node["dependency_only"]["ls_at"] == _iso(13, 9)
    assert node["resource_aware"]["ls_at"] == _iso(13, 13)
    assert node["resource_aware"]["lf_at"] == _iso(14, 21)
    assert node["computed_status"] == "ready"
    assert node["late_risk_window"]["active"] is False


def test_blockers_mark_process_without_changing_resource_schedule():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_resource_project(service)
    query = {
        "action": "query_resource_schedule",
        "project_id": ids["project_id"],
        **_resource_horizon(),
        "include_allocation_slices": True,
        "planning_granularity": "hour",
    }
    baseline = _query(service, query)
    _handle(
        service,
        {
            "action": "add_blocker",
            "project_id": ids["project_id"],
            "process_id": ids["build_id"],
            "summary": "Waiting on decision",
            "severity": "blocking",
            "created_at": _iso(13, 11),
        },
    )

    blocked_schedule = _query(service, query)
    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": ids["project_id"],
            **_resource_horizon(),
            "include_resource_fields": True,
            "include_allocation_slices": True,
            "planning_granularity": "hour",
        },
    )
    baseline_row = next(
        row for row in baseline["processes"] if row["process_id"] == ids["build_id"]
    )
    blocked_row = next(
        row
        for row in blocked_schedule["processes"]
        if row["process_id"] == ids["build_id"]
    )
    blocked_node = next(
        node for node in graph["nodes"] if node["process_id"] == ids["build_id"]
    )

    assert blocked_row["allocation_state"] == "complete"
    assert blocked_row["starts_at"] == baseline_row["starts_at"]
    assert blocked_row["ends_at"] == baseline_row["ends_at"]
    assert blocked_row["inferred_duration_hours"] == (
        baseline_row["inferred_duration_hours"]
    )
    assert blocked_node["computed_status"] == "blocked"
    assert blocked_node["resource_aware"]["inferred_duration_hours"] == (
        baseline_row["inferred_duration_hours"]
    )


def test_resource_schedule_without_public_horizon_extends_to_required_work():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _handle(
        service,
        {
            "action": "create_project",
            "project_id": "project-new-project",
            "name": "New project",
            "start_at": "2026-05-14T13:42:00+00:00",
        },
    )["project_id"]
    design_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-design",
            "name": "Design",
        },
    )["role_id"]
    lead_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-lead",
            "name": "Lead",
        },
    )["role_id"]
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-weekday",
            "name": "Weekday calendar",
            "timezone": "UTC",
            "weekly_windows": _weekday_windows(),
        },
    )["calendar_id"]
    _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": "res-josh",
            "name": "Josh",
            "role_ids": [design_id, lead_id],
            "calendar_id": calendar_id,
            "available_from_at": "2026-05-14T13:42:00+00:00",
            "cost_rate": "0",
            "cost_unit": "hour",
            "cost_currency": "USD",
        },
    )
    first_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_symbol": "first-step",
            "name": "first step",
            "effective_at": "2026-05-14T13:42:00+00:00",
            "duration_business_days": 1,
            "role_requirements": [
                {"role_id": design_id, "effort_hours": 2},
                {"role_id": lead_id, "effort_hours": 1},
            ],
        },
    )["process_id"]
    second_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_symbol": "2nd-step",
            "name": "2nd step",
            "effective_at": "2026-05-14T13:42:00+00:00",
            "duration_business_days": 1,
            "dependencies": [first_id],
            "role_requirements": [
                {"role_id": design_id, "effort_hours": 30},
                {"role_id": lead_id, "effort_hours": 10},
            ],
        },
    )["process_id"]

    schedule = _query(
        service,
        {
            "action": "query_resource_schedule",
            "project_id": project_id,
            "as_of": "2026-05-14T15:06:00+00:00",
            "now": "2026-05-14T15:06:00+00:00",
            "include_allocation_slices": True,
        },
    )
    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": "2026-05-14T15:06:00+00:00",
            "now": "2026-05-14T15:06:00+00:00",
            "include_resource_fields": True,
        },
    )

    rows = {row["process_id"]: row for row in schedule["processes"]}
    assert rows[second_id]["allocation_state"] == "complete"
    assert rows[second_id]["starts_at"] > rows[first_id]["ends_at"]
    assert rows[second_id]["ends_at"] is not None
    assert rows[second_id]["inferred_duration_hours"] == 40
    second_node = next(
        node for node in graph["nodes"] if node["process_symbol"] == "2nd-step"
    )
    assert second_node["resource_aware"]["allocation_state"] == "complete"
    assert second_node["resource_aware"]["inferred_duration_hours"] == 40


def test_resource_schedule_capacity_and_utilization_contracts():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_resource_project(service)

    schedule = _query(
        service,
        {
            "action": "query_resource_schedule",
            "project_id": ids["project_id"],
            **_resource_horizon(),
            "include_allocation_slices": True,
            "planning_granularity": "hour",
        },
    )
    assert set(schedule) == {
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
    assert schedule["critical_path_process_ids"] == [
        ids["design_id"],
        ids["build_id"],
    ]
    row = next(
        process
        for process in schedule["processes"]
        if process["process_id"] == ids["build_id"]
    )
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
        "inferred_duration_hours",
        "resource_delay_hours",
        "allocation_state",
        "allocation_diagnostic",
        "status",
        "started_at",
        "finished_at",
        "requirement_ids",
    }
    assert row["allocation_state"] == "complete"
    assert row["description"] == ""
    assert row["finished_at"] is None
    assert row["resource_es_at"] == row["starts_at"]
    assert row["resource_ef_at"] == row["ends_at"]
    assert row["resource_lf_at"] >= row["resource_ef_at"]
    assert row["inferred_duration_hours"] > 0

    capacity = _query(
        service,
        {
            "action": "query_resource_capacity",
            "project_id": ids["project_id"],
            "as_of": _iso(13, 12),
            "horizon_starts_at": _iso(13, 13),
            "horizon_ends_at": _iso(13, 15),
            "resource_ids": [ids["engineer_resource_id"]],
            "role_ids": [ids["engineer_id"]],
            "planning_granularity": "hour",
        },
    )
    bucket = capacity["buckets"][0]
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
    assert bucket["resource_id"] == ids["engineer_resource_id"]
    assert bucket["role_ids"] == [ids["engineer_id"]]
    assert bucket["capacity_hours"] == 1
    assert bucket["allocated_hours"] == 1
    assert bucket["remaining_hours"] == 0

    utilization = _query(
        service,
        {
            "action": "query_utilization",
            "project_id": ids["project_id"],
            **_resource_horizon(),
            "planning_granularity": "hour",
        },
    )
    assert set(utilization) == {
        "project_id",
        "as_of",
        "planning_granularity",
        "by_resource",
        "by_role",
        "time_series",
        "overallocated_buckets",
    }
    assert utilization["by_resource"][0]["resource_id"] == (
        ids["engineer_resource_id"]
    )
    assert utilization["by_role"][0]["role_id"] == ids["engineer_id"]
    assert utilization["overallocated_buckets"] == []


def test_resource_capacity_uses_time_ranged_calendar_overrides():
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
    )["role_id"]
    default_calendar = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-default-one-hour",
            "name": "Default one hour",
            "timezone": "UTC",
            "weekly_windows": [
                {
                    "window_id": f"default-{weekday}",
                    "weekday": weekday,
                    "start_local_time": "09:00",
                    "end_local_time": "10:00",
                    "capacity_hours": 1,
                }
                for weekday in range(5)
            ],
        },
    )["calendar_id"]
    august_calendar = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-august-one-and-half",
            "name": "August one and a half hours",
            "timezone": "UTC",
            "weekly_windows": [
                {
                    "window_id": f"august-{weekday}",
                    "weekday": weekday,
                    "start_local_time": "09:00",
                    "end_local_time": "10:30",
                    "capacity_hours": 1.5,
                }
                for weekday in range(5)
            ],
        },
    )["calendar_id"]
    september_calendar = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-september-two-hours",
            "name": "September two hours",
            "timezone": "UTC",
            "weekly_windows": [
                {
                    "window_id": f"september-{weekday}",
                    "weekday": weekday,
                    "start_local_time": "09:00",
                    "end_local_time": "11:00",
                    "capacity_hours": 2,
                }
                for weekday in range(5)
            ],
        },
    )["calendar_id"]
    resource_id = _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": "resource-josh",
            "name": "Josh",
            "role_ids": [role_id],
            "calendar_id": default_calendar,
            "available_from_at": "2026-05-14T09:00:00+00:00",
            "cost_rate": "0",
            "cost_unit": "hour",
            "cost_currency": "USD",
            "calendar_overrides": [
                {
                    "rule_id": "august-capacity",
                    "calendar_id": august_calendar,
                    "starts_at": "2026-08-01T00:00:00+00:00",
                    "ends_at": "2026-09-01T00:00:00+00:00",
                    "reason": "Expected August availability increase.",
                },
                {
                    "rule_id": "september-capacity",
                    "calendar_id": september_calendar,
                    "starts_at": "2026-09-01T00:00:00+00:00",
                    "ends_at": "2026-10-01T00:00:00+00:00",
                    "reason": "Expected September availability increase.",
                },
            ],
        },
    )["resource_id"]

    catalog = _query(
        service,
        {
            "action": "query_project_catalog",
            "project_id": project_id,
        },
    )
    resource = next(
        item for item in catalog["resources"] if item["resource_id"] == resource_id
    )
    assert resource["calendar_id"] == default_calendar
    assert resource["calendar_overrides"] == [
        {
            "rule_id": "august-capacity",
            "calendar_id": august_calendar,
            "starts_at": "2026-08-01T00:00:00+00:00",
            "ends_at": "2026-09-01T00:00:00+00:00",
            "reason": "Expected August availability increase.",
        },
        {
            "rule_id": "september-capacity",
            "calendar_id": september_calendar,
            "starts_at": "2026-09-01T00:00:00+00:00",
            "ends_at": "2026-10-01T00:00:00+00:00",
            "reason": "Expected September availability increase.",
        },
    ]

    july = _query(
        service,
        {
            "action": "query_resource_capacity",
            "project_id": project_id,
            "as_of": "2026-07-06T08:00:00+00:00",
            "horizon_starts_at": "2026-07-06T09:00:00+00:00",
            "horizon_ends_at": "2026-07-06T11:00:00+00:00",
            "resource_ids": [resource_id],
        },
    )
    august = _query(
        service,
        {
            "action": "query_resource_capacity",
            "project_id": project_id,
            "as_of": "2026-08-03T08:00:00+00:00",
            "horizon_starts_at": "2026-08-03T09:00:00+00:00",
            "horizon_ends_at": "2026-08-03T11:00:00+00:00",
            "resource_ids": [resource_id],
        },
    )
    september = _query(
        service,
        {
            "action": "query_resource_capacity",
            "project_id": project_id,
            "as_of": "2026-09-01T08:00:00+00:00",
            "horizon_starts_at": "2026-09-01T09:00:00+00:00",
            "horizon_ends_at": "2026-09-01T11:00:00+00:00",
            "resource_ids": [resource_id],
        },
    )

    assert [
        (bucket["calendar_id"], bucket["starts_at"], bucket["capacity_hours"])
        for bucket in july["buckets"]
    ] == [
        (
            default_calendar,
            "2026-07-06T09:00:00+00:00",
            1.0,
        )
    ]
    assert [
        (bucket["calendar_id"], bucket["starts_at"], bucket["capacity_hours"])
        for bucket in august["buckets"]
    ] == [
        (
            august_calendar,
            "2026-08-03T09:00:00+00:00",
            1.0,
        ),
        (
            august_calendar,
            "2026-08-03T10:00:00+00:00",
            0.5,
        ),
    ]
    assert [
        (bucket["calendar_id"], bucket["starts_at"], bucket["capacity_hours"])
        for bucket in september["buckets"]
    ] == [
        (
            september_calendar,
            "2026-09-01T09:00:00+00:00",
            1.0,
        ),
        (
            september_calendar,
            "2026-09-01T10:00:00+00:00",
            1.0,
        ),
    ]

    process_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-september-work",
            "name": "September work",
            "effective_at": "2026-05-14T09:00:00+00:00",
            "earliest_start_at": "2026-09-01T09:00:00+00:00",
            "role_requirements": [
                {
                    "role_id": role_id,
                    "effort_hours": 2,
                }
            ],
        },
    )["process_id"]
    schedule = _query(
        service,
        {
            "action": "query_resource_schedule",
            "project_id": project_id,
            "as_of": "2026-09-01T08:00:00+00:00",
            "now": "2026-09-01T08:00:00+00:00",
            "include_allocation_slices": True,
        },
    )
    row = next(
        item for item in schedule["processes"] if item["process_id"] == process_id
    )
    assert row["starts_at"] == "2026-09-01T09:00:00+00:00"
    assert row["ends_at"] == "2026-09-01T11:00:00+00:00"
    assert [
        slice_data["calendar_id"]
        for slice_data in september["buckets"]
        if slice_data["capacity_hours"] > 0
    ] == [september_calendar, september_calendar]


def test_terminal_topology_scope_filters_resource_schedule_and_utilization():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_resource_project(service)
    scope = {
        "type": "topo_filter",
        "root_process_symbols": ["process-build"],
        "direction": "ancestors",
    }

    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": ids["project_id"],
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
            "scope": scope,
            **_resource_horizon(),
            "include_resource_fields": True,
            "include_allocation_slices": True,
            "planning_granularity": "hour",
        },
    )
    schedule = _query(
        service,
        {
            "action": "query_resource_schedule",
            "project_id": ids["project_id"],
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
            "scope": scope,
            **_resource_horizon(),
            "include_allocation_slices": True,
            "planning_granularity": "hour",
        },
    )
    utilization = _query(
        service,
        {
            "action": "query_utilization",
            "project_id": ids["project_id"],
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
            "scope": scope,
            **_resource_horizon(),
            "planning_granularity": "hour",
        },
    )

    assert {node["process_id"] for node in graph["nodes"]} == {
        ids["design_id"],
        ids["build_id"],
    }
    assert {row["process_id"] for row in schedule["processes"]} == {
        ids["design_id"],
        ids["build_id"],
    }
    assert {
        allocation["process_id"] for allocation in schedule["allocation_slices"]
    } == {ids["design_id"], ids["build_id"]}
    assert utilization["by_role"] == [
        {
            "role_id": ids["engineer_id"],
            "demanded_effort_hours": 12,
            "fulfilled_effort_hours": 12,
        }
    ]


def test_query_critical_path_remains_dependency_only_under_resource_delay():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-critical-engineer",
            "name": "Critical Engineer",
        },
    )["role_id"]
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-critical",
            "name": "Critical Weekdays",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    )["calendar_id"]
    _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": "resource-critical-ada",
            "name": "Critical Ada",
            "role_ids": [role_id],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13, 13),
            "cost_rate": "125.00",
            "cost_unit": "hour",
            "cost_currency": "USD",
        },
    )
    dependency_only_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-dependency-long",
            "name": "Dependency Long Work",
            "effective_at": _iso(13, 9),
            "duration_business_days": 2,
        },
    )["process_id"]
    resource_delayed_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-resource-delayed",
            "name": "Resource Delayed Work",
            "effective_at": _iso(13, 9),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-resource-delayed",
                    "role_id": role_id,
                    "effort_hours": 32,
                }
            ],
        },
    )["process_id"]

    critical_path = _query(
        service,
        {
            "action": "query_critical_path",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
        },
    )
    resource_schedule = _query(
        service,
        {
            "action": "query_resource_schedule",
            "project_id": project_id,
            **_resource_horizon(),
            "planning_granularity": "hour",
        },
    )
    delayed_row = next(
        row
        for row in resource_schedule["processes"]
        if row["process_id"] == resource_delayed_id
    )

    assert critical_path["critical_path_process_ids"] == [dependency_only_id]
    assert "allocation_slices" not in critical_path
    assert "unallocated_requirements" not in critical_path
    assert resource_schedule["critical_path_process_ids"] == [resource_delayed_id]
    assert delayed_row["resource_delay_hours"] == 0
    assert delayed_row["ends_at"] > delayed_row["dependency_only_ends_at"]


def test_resource_schedule_rejects_missing_role_capacity_with_structured_error():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    engineer_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    )["role_id"]
    designer_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-designer",
            "name": "Designer",
        },
    )["role_id"]
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-nyc",
            "name": "NYC Weekdays",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    )["calendar_id"]
    _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": "resource-ada",
            "name": "Ada",
            "role_ids": [engineer_id],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13, 13),
            "cost_rate": "125.00",
            "cost_unit": "hour",
            "cost_currency": "USD",
        },
    )
    setup_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-setup",
            "name": "Setup",
            "effective_at": _iso(13, 9),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-setup-eng",
                    "role_id": engineer_id,
                    "effort_hours": 1,
                }
            ],
        },
    )["process_id"]
    _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-build",
            "name": "Build",
            "effective_at": _iso(13, 9),
            "duration_business_days": 1,
            "dependencies": [setup_id],
            "role_requirements": [
                {
                    "requirement_id": "req-partial-eng",
                    "role_id": engineer_id,
                    "effort_hours": 4,
                }
            ],
        },
    )
    _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-design",
            "name": "Dependency-Ready Design",
            "effective_at": _iso(13, 9),
            "duration_business_days": 1,
            "dependencies": [setup_id],
            "role_requirements": [
                {
                    "requirement_id": "req-ready-design",
                    "role_id": designer_id,
                    "effort_hours": 2,
                }
            ],
        },
    )

    result = _query_result(
        service,
        {
            "action": "query_resource_schedule",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
            "include_allocation_slices": True,
            "planning_granularity": "hour",
        },
    )

    assert result.ok is False
    assert result.error.code == "resource_schedule_unsatisfiable"
    assert "no_eligible_resource" in result.error.message
    assert result.error.details == {}


def test_cost_queries_cover_filters_grouping_and_totals():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_resource_project(service)

    project_costs = _query(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            **_resource_horizon(),
            "currency": "USD",
            "group_by": ["resource"],
        },
    )
    assert set(project_costs) == {
        "project_id",
        "as_of",
        "currency",
        "total_cost",
        "by_resource",
        "by_process",
        "by_role",
        "time_series",
    }
    assert project_costs["currency"] == "USD"
    assert project_costs["total_cost"] == "1500.00"
    assert project_costs["by_resource"] == [
        {
            "resource_id": ids["engineer_resource_id"],
            "cost_unit": "hour",
            "allocated_hours": 12,
            "currency": "USD",
            "cost_amount": "1500.00",
        }
    ]
    assert project_costs["by_process"] == []
    assert project_costs["by_role"] == []
    assert project_costs["time_series"] == []

    process_costs = _query(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            **_resource_horizon(),
            "target_process_symbol": "process-build",
            "resource_ids": [ids["engineer_resource_id"]],
            "role_ids": [ids["engineer_id"]],
            "currency": "USD",
            "group_by": ["process"],
        },
    )
    assert process_costs["total_cost"] == "1000.00"
    assert process_costs["by_resource"] == []
    assert process_costs["by_process"] == [
        {
            "process_id": ids["build_id"],
            "allocated_hours": 8,
            "currency": "USD",
            "cost_amount": "1000.00",
        }
    ]
    assert process_costs["by_role"] == []
    assert process_costs["time_series"] == []

    topology_costs = _query(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
            "scope": {
                "type": "topo_filter",
                "root_process_symbols": ["process-design"],
                "direction": "descendants",
            },
            "currency": "USD",
            "group_by": ["time"],
        },
    )
    assert topology_costs["by_resource"] == []
    assert topology_costs["by_process"] == []
    assert topology_costs["by_role"] == []
    assert topology_costs["time_series"]
    first_bucket = topology_costs["time_series"][0]
    _assert_cost_bucket_shape(first_bucket)
    assert first_bucket["resource_id"] is None
    assert first_bucket["process_id"] is None
    assert first_bucket["role_id"] is None
    _assert_cost_buckets_sum_to_total(topology_costs)


def test_cost_query_recomputes_authoritative_costs_from_resource_facts():
    service = ProjectService(
        InMemoryProjectRepository(),
        resource_scheduler=lambda _input_data: {
            "project_id": "project",
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
            "horizon_starts_at": _iso(13, 13),
            "horizon_ends_at": _iso(13, 15),
            "planning_granularity": "hour",
            "processes": [],
            "allocation_slices": [
                {
                    "slice_id": "slice-cost-spoof",
                    "project_id": "project",
                    "process_id": "process-cost",
                    "requirement_id": "req-cost-eng",
                    "role_id": "role-engineer",
                    "resource_id": "resource-ada",
                    "starts_at": _iso(13, 13),
                    "ends_at": _iso(13, 15),
                    "effort_hours": 2,
                    "capacity_hours": 2,
                    "cost_amount": "999999.00",
                    "cost_currency": "USD",
                    "iteration": 1,
                }
            ],
            "critical_path_process_ids": ["process-cost"],
            "converged": True,
            "iteration_count": 1,
            "convergence": {},
        },
    )
    project_id = _create_project(service)
    role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    )["role_id"]
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-cost",
            "name": "Cost Calendar",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    )["calendar_id"]
    _handle(
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
            "cost_currency": "USD",
        },
    )
    process_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-cost",
            "name": "Costed Work",
            "effective_at": _iso(13, 9),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-cost-eng",
                    "role_id": role_id,
                    "effort_hours": 2,
                }
            ],
        },
    )["process_id"]

    costs = _query(
        service,
        {
            "action": "query_costs",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
            "currency": "USD",
            "group_by": ["resource", "process", "role", "time"],
        },
    )

    assert process_id == "process-cost"
    assert costs["total_cost"] == "250.00"
    assert costs["by_resource"] == [
        {
            "resource_id": "resource-ada",
            "cost_unit": "hour",
            "allocated_hours": 2,
            "currency": "USD",
            "cost_amount": "250.00",
        }
    ]
    assert costs["by_process"] == [
        {
            "process_id": "process-cost",
            "allocated_hours": 2,
            "currency": "USD",
            "cost_amount": "250.00",
        }
    ]
    assert costs["by_role"] == [
        {
            "role_id": role_id,
            "allocated_hours": 2,
            "currency": "USD",
            "cost_amount": "250.00",
        }
    ]
    assert {bucket["cost_amount"] for bucket in costs["time_series"]} == {"125.00"}
    assert "999999.00" not in {
        bucket["cost_amount"] for bucket in costs["time_series"]
    }
    _assert_cost_buckets_sum_to_total(costs)


def test_cost_queries_cover_all_cost_units():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_cost_unit_project(service)

    costs = _query(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            "as_of": _iso(18, 12),
            "now": _iso(18, 12),
            "currency": "USD",
            "group_by": ["resource"],
        },
    )

    assert costs["total_cost"] == "3340.00"
    assert sorted(costs["by_resource"], key=lambda row: row["cost_unit"]) == [
        {
            "resource_id": ids["day_resource_id"],
            "cost_unit": "day",
            "allocated_hours": 8,
            "currency": "USD",
            "cost_amount": "400.00",
        },
        {
            "resource_id": ids["fixed_resource_id"],
            "cost_unit": "fixed",
            "allocated_hours": 6,
            "currency": "USD",
            "cost_amount": "300.00",
        },
        {
            "resource_id": ids["hour_resource_id"],
            "cost_unit": "hour",
            "allocated_hours": 8,
            "currency": "USD",
            "cost_amount": "640.00",
        },
        {
            "resource_id": ids["week_resource_id"],
            "cost_unit": "week",
            "allocated_hours": 40,
            "currency": "USD",
            "cost_amount": "2000.00",
        },
    ]

    daily_buckets = _query(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            "as_of": _iso(18, 12),
            "now": _iso(18, 12),
            "resource_ids": [ids["day_resource_id"]],
            "role_ids": [ids["day_role_id"]],
            "currency": "USD",
            "group_by": ["resource", "time"],
        },
    )
    assert daily_buckets["total_cost"] == "400.00"
    assert daily_buckets["by_resource"] == [
        {
            "resource_id": ids["day_resource_id"],
            "cost_unit": "day",
            "allocated_hours": 8,
            "currency": "USD",
            "cost_amount": "400.00",
        }
    ]
    assert [
        (
            bucket["starts_at"],
            bucket["ends_at"],
            bucket["resource_id"],
            bucket["process_id"],
            bucket["role_id"],
            bucket["allocated_hours"],
            bucket["cost_amount"],
        )
        for bucket in daily_buckets["time_series"]
    ] == [
        (
            _iso(18, hour),
            _iso(18, hour + 1),
            ids["day_resource_id"],
            None,
            None,
            1,
            "50.00",
        )
        for hour in range(13, 21)
    ]
    for bucket in daily_buckets["time_series"]:
        _assert_cost_bucket_shape(bucket)
    _assert_cost_buckets_sum_to_total(daily_buckets)

    weekly_buckets = _query(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            "as_of": _iso(18, 12),
            "now": _iso(18, 12),
            "resource_ids": [ids["week_resource_id"]],
            "role_ids": [ids["week_role_id"]],
            "currency": "USD",
            "group_by": ["resource", "time"],
        },
    )
    assert weekly_buckets["total_cost"] == "2000.00"
    assert len(weekly_buckets["time_series"]) == 40
    assert {
        bucket["cost_amount"] for bucket in weekly_buckets["time_series"]
    } == {"50.00"}
    assert {
        bucket["allocated_hours"] for bucket in weekly_buckets["time_series"]
    } == {1}
    for bucket in weekly_buckets["time_series"]:
        _assert_cost_bucket_shape(bucket)
        assert bucket["starts_at"] >= _iso(18, 13)
        assert bucket["ends_at"] <= _iso(23, 0)
        assert bucket["resource_id"] == ids["week_resource_id"]
        assert bucket["process_id"] is None
        assert bucket["role_id"] is None
    _assert_cost_buckets_sum_to_total(weekly_buckets)

    fixed_buckets = _query(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            "as_of": _iso(18, 12),
            "now": _iso(18, 12),
            "resource_ids": [ids["fixed_resource_id"]],
            "role_ids": [ids["fixed_role_id"]],
            "currency": "USD",
            "group_by": ["resource", "time"],
        },
    )
    assert fixed_buckets["total_cost"] == "300.00"
    assert fixed_buckets["by_resource"] == [
        {
            "resource_id": ids["fixed_resource_id"],
            "cost_unit": "fixed",
            "allocated_hours": 6,
            "currency": "USD",
            "cost_amount": "300.00",
        }
    ]
    assert [
        (
            bucket["starts_at"],
            bucket["ends_at"],
            bucket["allocated_hours"],
            bucket["cost_amount"],
        )
        for bucket in fixed_buckets["time_series"]
    ] == [
        (_iso(18, hour), _iso(18, hour + 1), 1, "50.00")
        for hour in range(13, 19)
    ]
    for bucket in fixed_buckets["time_series"]:
        _assert_cost_bucket_shape(bucket)
        assert bucket["resource_id"] == ids["fixed_resource_id"]
        assert bucket["process_id"] is None
        assert bucket["role_id"] is None
    _assert_cost_buckets_sum_to_total(fixed_buckets)


def test_cost_time_series_uses_requested_cross_product_dimensions():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_resource_project(service)

    resource_process_costs = _query(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            **_resource_horizon(),
            "currency": "USD",
            "group_by": ["resource", "process", "time"],
        },
    )

    assert resource_process_costs["total_cost"] == "1500.00"
    assert resource_process_costs["by_role"] == []
    assert resource_process_costs["time_series"]
    bucket_keys = [
        (
            bucket["resource_id"],
            bucket["process_id"],
            bucket["role_id"],
            bucket["starts_at"],
            bucket["ends_at"],
        )
        for bucket in resource_process_costs["time_series"]
    ]
    assert bucket_keys == sorted(bucket_keys)
    assert len(bucket_keys) == len(set(bucket_keys))
    assert {
        bucket["resource_id"] for bucket in resource_process_costs["time_series"]
    } == {ids["engineer_resource_id"]}
    assert {
        bucket["process_id"] for bucket in resource_process_costs["time_series"]
    } == {ids["design_id"], ids["build_id"]}
    assert {
        bucket["role_id"] for bucket in resource_process_costs["time_series"]
    } == {None}
    _assert_cost_buckets_sum_to_total(resource_process_costs)

    role_time_costs = _query(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            **_resource_horizon(),
            "currency": "USD",
            "group_by": ["role", "time"],
        },
    )

    assert role_time_costs["total_cost"] == "1500.00"
    assert role_time_costs["by_resource"] == []
    assert role_time_costs["by_process"] == []
    assert role_time_costs["by_role"] == [
        {
            "role_id": ids["engineer_id"],
            "allocated_hours": 12,
            "currency": "USD",
            "cost_amount": "1500.00",
        }
    ]
    assert role_time_costs["time_series"]
    for bucket in role_time_costs["time_series"]:
        _assert_cost_bucket_shape(bucket)
        assert bucket["resource_id"] is None
        assert bucket["process_id"] is None
        assert bucket["role_id"] == ids["engineer_id"]
    assert {bucket["role_id"] for bucket in role_time_costs["time_series"]} == {
        ids["engineer_id"]
    }
    _assert_cost_buckets_sum_to_total(role_time_costs)

    full_cross_product_costs = _query(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            **_resource_horizon(),
            "currency": "USD",
            "group_by": ["resource", "process", "role", "time"],
        },
    )

    assert full_cross_product_costs["total_cost"] == "1500.00"
    assert full_cross_product_costs["time_series"]
    for bucket in full_cross_product_costs["time_series"]:
        _assert_cost_bucket_shape(bucket)
        assert bucket["resource_id"] == ids["engineer_resource_id"]
        assert bucket["process_id"] in {ids["design_id"], ids["build_id"]}
        assert bucket["role_id"] == ids["engineer_id"]
    full_bucket_keys = [
        (
            bucket["resource_id"],
            bucket["process_id"],
            bucket["role_id"],
            bucket["starts_at"],
            bucket["ends_at"],
        )
        for bucket in full_cross_product_costs["time_series"]
    ]
    assert full_bucket_keys == sorted(full_bucket_keys)
    assert len(full_bucket_keys) == len(set(full_bucket_keys))
    _assert_cost_buckets_sum_to_total(full_cross_product_costs)


def test_cost_filters_reject_inactive_resource_and_role_capacity():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_resource_project(service)
    _handle(
        service,
        {
            "action": "set_resource_active",
            "project_id": ids["project_id"],
            "resource_id": ids["engineer_resource_id"],
            "active": False,
        },
    )
    _handle(
        service,
        {
            "action": "deactivate_role",
            "project_id": ids["project_id"],
            "role_id": ids["engineer_id"],
            "force": True,
        },
    )

    result = _query_result(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            **_resource_horizon(),
            "resource_ids": [ids["engineer_resource_id"]],
            "role_ids": [ids["engineer_id"]],
            "currency": "USD",
            "group_by": ["resource", "process", "role", "time"],
        },
    )

    assert result.ok is False
    assert result.error.code == "resource_schedule_unsatisfiable"
    assert "missing_role" in result.error.message


def test_cost_filters_reject_unknown_resource_ids_with_structured_error():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_resource_project(service)

    result = _query_result(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            **_resource_horizon(),
            "resource_ids": ["resource-missing"],
            "currency": "USD",
            "group_by": ["resource"],
        },
    )

    assert result.ok is False
    assert result.error.code == "not_found"
    assert result.error.details == {
        "entity_type": "resource",
        "entity_id": "resource-missing",
        "field": "resource_ids",
    }


def test_cost_filters_reject_unknown_role_ids_with_structured_error():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_resource_project(service)

    result = _query_result(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            **_resource_horizon(),
            "role_ids": ["role-missing"],
            "currency": "USD",
            "group_by": ["role"],
        },
    )

    assert result.ok is False
    assert result.error.code == "not_found"
    assert result.error.details == {
        "entity_type": "role",
        "entity_id": "role-missing",
        "field": "role_ids",
    }


def test_resource_schedule_max_iteration_warning_is_query_result_warning():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_resource_project(service)

    result = _query_result(
        service,
        {
            "action": "query_resource_schedule",
            "project_id": ids["project_id"],
            **_resource_horizon(),
            "planning_granularity": "hour",
            "max_iterations": 1,
        },
    )

    assert result.ok is True
    _assert_no_nested_warnings(result.data)
    assert result.data["converged"] is False
    assert result.warnings == [
        {
            "code": "max_iterations_reached",
            "message": "Resource schedule did not converge.",
            "severity": "warning",
            "details": {"max_iterations": 1},
        }
    ]


def test_utilization_does_not_multiply_multi_role_resource_capacity():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_multi_role_resource_project(service)

    utilization = _query(
        service,
        {
            "action": "query_utilization",
            "project_id": ids["project_id"],
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
            "planning_granularity": "hour",
        },
    )

    resource_row = utilization["by_resource"][0]
    assert resource_row["resource_id"] == ids["resource_id"]
    assert resource_row["allocated_hours"] == 8
    assert resource_row["capacity_hours"] >= resource_row["allocated_hours"]
    assert resource_row["remaining_hours"] == (
        resource_row["capacity_hours"] - resource_row["allocated_hours"]
    )
    assert resource_row["utilization_ratio"] == pytest.approx(
        resource_row["allocated_hours"] / resource_row["capacity_hours"],
        abs=1e-6,
    )
    assert resource_row["capacity_hours"] == sum(
        bucket["capacity_hours"] for bucket in utilization["time_series"]
    )
    assert sorted(utilization["by_role"], key=lambda row: row["role_id"]) == [
        {
            "role_id": ids["dev_role_id"],
            "demanded_effort_hours": 4,
            "fulfilled_effort_hours": 4,
        },
        {
            "role_id": ids["qa_role_id"],
            "demanded_effort_hours": 4,
            "fulfilled_effort_hours": 4,
        },
    ]
    assert utilization["overallocated_buckets"] == []
    for bucket in utilization["time_series"]:
        assert bucket["resource_id"] == ids["resource_id"]
        assert set(bucket["role_ids"]) <= {ids["dev_role_id"], ids["qa_role_id"]}
        assert bucket["allocated_hours"] <= bucket["capacity_hours"] + 0.0001


def test_resource_upsert_rejects_currency_outside_project_default():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_resource_project(service)
    result = service.handle_command(
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "upsert_resource",
                    "project_id": ids["project_id"],
                    "resource_id": "resource-eu-vendor",
                    "name": "EU Vendor",
                    "role_ids": [ids["engineer_id"]],
                    "calendar_id": ids["calendar_id"],
                    "available_from_at": _iso(13, 13),
                    "cost_rate": "90.00",
                    "cost_unit": "hour",
                    "cost_currency": "EUR",
                },
            }
        )
    )

    assert result.ok is False
    assert result.error.code == "resource_currency_mismatch"
    assert result.error.details == {
        "field_path": "cost_currency",
        "project_default_currency": "USD",
        "resource_cost_currency": "EUR",
    }


def test_cost_query_keeps_project_currency_for_grouped_costs():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_resource_project(service)

    result = _query_result(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            **_resource_horizon(),
            "currency": "USD",
            "group_by": ["resource", "process", "role", "time"],
        },
    )

    assert result.ok is True
    assert result.data["currency"] == "USD"


def test_project_currency_changes_reprice_resources_to_project_currency():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_resource_project(service)

    changed = _handle(
        service,
        {
            "action": "set_project_default_currency",
            "project_id": ids["project_id"],
            "default_currency": "EUR",
        },
    )
    query = _query_result(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            **_resource_horizon(),
            "currency": "EUR",
            "group_by": ["resource"],
        },
    )
    wrong_currency = _query_result(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            **_resource_horizon(),
            "currency": "USD",
            "group_by": ["resource"],
        },
    )

    assert changed["project_id"] == ids["project_id"]
    assert query.ok is True
    assert query.data["currency"] == "EUR"
    assert {
        row["currency"] for row in query.data["by_resource"]
    } == {"EUR"}
    assert service._repository.resources[
        ids["engineer_resource_id"]
    ]["cost_currency"] == "EUR"
    updated = _handle(
        service,
        {
            "action": "update_project",
            "project_id": ids["project_id"],
            "default_currency": "GBP",
        },
    )
    assert updated["project_id"] == ids["project_id"]
    assert service._repository.resources[
        ids["engineer_resource_id"]
    ]["cost_currency"] == "GBP"
    assert wrong_currency.ok is False
    assert wrong_currency.error.code == "project_currency_mismatch"
    assert wrong_currency.error.details == {
        "field_path": "currency",
        "project_default_currency": "EUR",
        "requested_currency": "USD",
    }


def test_target_history_query_is_removed_from_resource_contract():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_dependency_project(service)

    with pytest.raises(ValueError):
        QueryEnvelope.model_validate(
            {
                "query": {
                    "action": "query_target_history",
                    "project_id": ids["project_id"],
                    "as_of": _iso(15, 12, NYC),
                }
            }
        )


def test_work_now_and_late_risk_use_es_ls_with_timezone_aware_as_of():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    process_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-launch",
            "name": "Launch",
            "effective_at": _iso(13, 9, NYC),
            "duration_business_days": 1,
            "earliest_start_at": _iso(13, 9, NYC),
        },
    )["process_id"]
    zero_slack_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-critical",
            "name": "Critical Work",
            "effective_at": _iso(13, 9, NYC),
            "duration_business_days": 3,
            "earliest_start_at": _iso(13, 9, NYC),
        },
    )["process_id"]

    work_now = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(13, 12, NYC),
            "now": _iso(13, 12, NYC),
            "include_resource_fields": False,
        },
    )
    work_node = next(
        node for node in work_now["nodes"] if node["process_id"] == process_id
    )
    assert work_node["computed_status"] == "work_now"
    assert work_node["work_now_window"] == {
        "starts_at": work_node["dependency_only"]["es_at"],
        "ends_at": work_node["dependency_only"]["ls_at"],
        "active": True,
    }
    assert work_node["late_risk_window"]["active"] is False

    late_risk = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(15, 12, NYC),
            "now": _iso(15, 12, NYC),
            "include_resource_fields": False,
        },
    )
    late_node = next(
        node for node in late_risk["nodes"] if node["process_id"] == process_id
    )
    assert late_node["computed_status"] == "late_risk"
    assert late_node["late_risk_window"] == {
        "starts_at": late_node["dependency_only"]["ls_at"],
        "ends_at": late_node["dependency_only"]["lf_at"],
        "active": True,
    }

    boundary = _at(13, 9, NYC).isoformat()
    after_boundary = (_at(13, 9, NYC) + dt.timedelta(minutes=1)).isoformat()

    zero_slack = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": boundary,
            "now": boundary,
            "include_resource_fields": False,
        },
    )
    zero_slack_node = next(
        node
        for node in zero_slack["nodes"]
        if node["process_id"] == zero_slack_id
    )
    assert zero_slack_node["dependency_only"]["es_at"] == (
        zero_slack_node["dependency_only"]["ls_at"]
    )
    assert zero_slack_node["computed_status"] == "late_risk"
    assert zero_slack_node["work_now_window"] == {
        "starts_at": zero_slack_node["dependency_only"]["es_at"],
        "ends_at": zero_slack_node["dependency_only"]["ls_at"],
        "active": False,
    }
    assert zero_slack_node["late_risk_window"] == {
        "starts_at": zero_slack_node["dependency_only"]["ls_at"],
        "ends_at": zero_slack_node["dependency_only"]["lf_at"],
        "active": True,
    }

    zero_slack_after_boundary = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": after_boundary,
            "now": after_boundary,
            "include_resource_fields": False,
        },
    )
    after_boundary_node = next(
        node
        for node in zero_slack_after_boundary["nodes"]
        if node["process_id"] == zero_slack_id
    )
    assert after_boundary_node["computed_status"] == "late_risk"
    assert after_boundary_node["late_risk_window"]["active"] is True

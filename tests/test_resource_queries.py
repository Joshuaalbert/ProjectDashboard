import datetime as dt
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

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
            "due_at": _iso(15, 17, NYC),
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
            "due_at": _iso(18, 17, NYC),
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
    unallocated_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-brand",
            "name": "Brand Review",
            "effective_at": _iso(13, 9),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-brand-design",
                    "role_id": designer_id,
                    "effort_hours": 3,
                }
            ],
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
        "unallocated_id": unallocated_id,
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
        "duration_hours",
        "earliest_start_at",
        "due_at",
        "status",
        "finished_at",
        "computed_status",
        "blocker_summary",
        "dependency_only",
        "resource_aware",
        "work_now_window",
        "late_risk_window",
    }
    assert build["status"] == "planned"
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
            "blocked_policy": "exclude",
        },
    )

    assert data["schedule_basis"] == "resource_aware"
    assert data["converged"] is True
    assert data["allocation_slices"]
    assert {
        allocation["process_id"]
        for allocation in data["allocation_slices"]
    } <= {ids["design_id"], ids["build_id"], ids["unallocated_id"]}

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
        "resource_delay_hours",
        "slack_hours",
        "criticality_label",
        "allocation_state",
    }
    assert build["resource_aware"]["allocation_state"] == "complete"
    assert build["resource_aware"]["ends_at"] >= build["dependency_only"]["ef_at"]


def test_resource_schedule_capacity_unallocated_and_utilization_contracts():
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
        "horizon_starts_at",
        "horizon_ends_at",
        "planning_granularity",
        "processes",
        "allocation_slices",
        "critical_path_process_ids",
        "unallocated_requirements",
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
        "ready_at",
        "starts_at",
        "ends_at",
        "dependency_only_starts_at",
        "dependency_only_ends_at",
        "resource_delay_hours",
        "allocation_state",
        "status",
        "finished_at",
        "requirement_ids",
    }
    assert row["allocation_state"] == "complete"
    assert row["finished_at"] is None

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
    assert bucket["allocated_hours"] <= bucket["capacity_hours"] + 0.0001

    unallocated = _query(
        service,
        {
            "action": "query_unallocated_requirements",
            "project_id": ids["project_id"],
            **_resource_horizon(),
            "planning_granularity": "hour",
        },
    )
    assert unallocated["unallocated_requirements"] == (
        schedule["unallocated_requirements"]
    )
    item = unallocated["unallocated_requirements"][0]
    assert set(item) == {
        "project_id",
        "process_id",
        "requirement_id",
        "role_id",
        "reason",
        "message",
        "remaining_effort_hours",
        "allocated_effort_hours",
        "eligible_resource_ids",
        "first_feasible_starts_at",
    }
    assert item["process_id"] == ids["unallocated_id"]
    assert item["reason"] == "no_eligible_resource"

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
        "horizon_starts_at",
        "horizon_ends_at",
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
    assert schedule["unallocated_requirements"] == []
    assert {
        allocation["process_id"] for allocation in schedule["allocation_slices"]
    } == {ids["design_id"], ids["build_id"]}
    assert utilization["by_role"] == [
        {
            "role_id": ids["engineer_id"],
            "demanded_effort_hours": 12,
            "fulfilled_effort_hours": 12,
            "unallocated_effort_hours": 0,
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
    assert delayed_row["resource_delay_hours"] > 0


def test_resource_schedule_row_nullability_for_partial_and_unallocated_rows():
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
    partial_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-partial",
            "name": "Partial Build",
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
    )["process_id"]
    unallocated_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-unallocated",
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
    )["process_id"]

    schedule = _query(
        service,
        {
            "action": "query_resource_schedule",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "now": _iso(13, 12),
            "horizon_starts_at": _iso(13, 13),
            "horizon_ends_at": _iso(13, 16),
            "include_allocation_slices": True,
            "planning_granularity": "hour",
        },
    )

    rows = {row["process_id"]: row for row in schedule["processes"]}
    setup = rows[setup_id]
    assert setup["allocation_state"] == "complete"
    assert setup["ends_at"] is not None

    partial = rows[partial_id]
    assert partial["allocation_state"] == "partial"
    assert partial["ready_at"] is not None
    assert partial["starts_at"] is not None
    assert partial["ends_at"] is None
    assert partial["resource_delay_hours"] == 0
    partial_slices = [
        allocation
        for allocation in schedule["allocation_slices"]
        if allocation["process_id"] == partial_id
    ]
    assert partial_slices
    assert partial["starts_at"] == min(
        allocation["starts_at"] for allocation in partial_slices
    )
    assert partial["starts_at"] >= partial["ready_at"]

    unallocated = rows[unallocated_id]
    assert unallocated["allocation_state"] == "unallocated"
    assert unallocated["ready_at"] is not None
    assert unallocated["ready_at"] >= setup["ends_at"]
    assert unallocated["starts_at"] is None
    assert unallocated["ends_at"] is None
    assert unallocated["resource_delay_hours"] == 0
    unallocated_item = next(
        item
        for item in schedule["unallocated_requirements"]
        if item["process_id"] == unallocated_id
    )
    assert unallocated_item["requirement_id"] == "req-ready-design"
    assert unallocated_item["reason"] == "no_eligible_resource"
    assert unallocated_item["message"]


def test_cost_queries_cover_filters_grouping_totals_and_horizon_clipping():
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
        "horizon_starts_at",
        "horizon_ends_at",
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
            "horizon_starts_at": _iso(13, 14),
            "horizon_ends_at": _iso(13, 16),
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
    assert first_bucket["starts_at"] >= _iso(13, 14)
    assert first_bucket["ends_at"] <= _iso(13, 16)
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
            "unallocated_requirements": [],
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
            "horizon_starts_at": _iso(13, 13),
            "horizon_ends_at": _iso(13, 15),
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
            "horizon_starts_at": _iso(18, 13),
            "horizon_ends_at": _iso(23, 0),
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
            "horizon_starts_at": _iso(18, 15),
            "horizon_ends_at": _iso(18, 17),
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
            "allocated_hours": 2,
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
            _iso(18, 15),
            _iso(18, 16),
            ids["day_resource_id"],
            None,
            None,
            1,
            "200.00",
        ),
        (
            _iso(18, 16),
            _iso(18, 17),
            ids["day_resource_id"],
            None,
            None,
            1,
            "200.00",
        ),
    ]
    for bucket in daily_buckets["time_series"]:
        _assert_cost_bucket_shape(bucket)
        assert bucket["starts_at"] >= _iso(18, 15)
        assert bucket["ends_at"] <= _iso(18, 17)
    _assert_cost_buckets_sum_to_total(daily_buckets)

    weekly_buckets = _query(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            "as_of": _iso(18, 12),
            "now": _iso(18, 12),
            "horizon_starts_at": _iso(18, 13),
            "horizon_ends_at": _iso(23, 0),
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

    weekly_clipped_buckets = _query(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            "as_of": _iso(18, 12),
            "now": _iso(18, 12),
            "horizon_starts_at": _iso(18, 13),
            "horizon_ends_at": _iso(18, 15),
            "resource_ids": [ids["week_resource_id"]],
            "role_ids": [ids["week_role_id"]],
            "currency": "USD",
            "group_by": ["resource", "time"],
        },
    )
    assert weekly_clipped_buckets["total_cost"] == "2000.00"
    assert [
        (
            bucket["starts_at"],
            bucket["ends_at"],
            bucket["allocated_hours"],
            bucket["cost_amount"],
        )
        for bucket in weekly_clipped_buckets["time_series"]
    ] == [
        (_iso(18, 13), _iso(18, 14), 1, "1000.00"),
        (_iso(18, 14), _iso(18, 15), 1, "1000.00"),
    ]
    for bucket in weekly_clipped_buckets["time_series"]:
        _assert_cost_bucket_shape(bucket)
        assert bucket["resource_id"] == ids["week_resource_id"]
        assert bucket["process_id"] is None
        assert bucket["role_id"] is None
    _assert_cost_buckets_sum_to_total(weekly_clipped_buckets)

    fixed_buckets = _query(
        service,
        {
            "action": "query_costs",
            "project_id": ids["project_id"],
            "as_of": _iso(18, 12),
            "now": _iso(18, 12),
            "horizon_starts_at": _iso(18, 13),
            "horizon_ends_at": _iso(18, 16),
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
            "allocated_hours": 3,
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
        (_iso(18, 13), _iso(18, 14), 1, "100.00"),
        (_iso(18, 14), _iso(18, 15), 1, "100.00"),
        (_iso(18, 15), _iso(18, 16), 1, "100.00"),
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
    horizon = _resource_horizon()
    assert role_time_costs["time_series"]
    for bucket in role_time_costs["time_series"]:
        _assert_cost_bucket_shape(bucket)
        assert bucket["resource_id"] is None
        assert bucket["process_id"] is None
        assert bucket["role_id"] == ids["engineer_id"]
        assert bucket["starts_at"] >= horizon["horizon_starts_at"]
        assert bucket["ends_at"] <= horizon["horizon_ends_at"]
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
        assert bucket["starts_at"] >= horizon["horizon_starts_at"]
        assert bucket["ends_at"] <= horizon["horizon_ends_at"]
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


def test_cost_filters_accept_inactive_resource_and_role_ids_as_empty_scope():
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

    costs = _query(
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

    assert costs["total_cost"] == "0.00"
    assert costs["by_resource"] == []
    assert costs["by_process"] == []
    assert costs["by_role"] == []
    assert costs["time_series"] == []


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
            "horizon_starts_at": _iso(13, 13),
            "horizon_ends_at": _iso(14, 0),
            "planning_granularity": "hour",
        },
    )

    resource_row = utilization["by_resource"][0]
    assert resource_row["resource_id"] == ids["resource_id"]
    assert resource_row["capacity_hours"] == 8
    assert resource_row["allocated_hours"] == 8
    assert resource_row["remaining_hours"] == 0
    assert resource_row["utilization_ratio"] == 1
    assert sorted(utilization["by_role"], key=lambda row: row["role_id"]) == [
        {
            "role_id": ids["dev_role_id"],
            "demanded_effort_hours": 4,
            "fulfilled_effort_hours": 4,
            "unallocated_effort_hours": 0,
        },
        {
            "role_id": ids["qa_role_id"],
            "demanded_effort_hours": 4,
            "fulfilled_effort_hours": 4,
            "unallocated_effort_hours": 0,
        },
    ]
    assert utilization["overallocated_buckets"] == []
    for bucket in utilization["time_series"]:
        assert bucket["resource_id"] == ids["resource_id"]
        assert set(bucket["role_ids"]) <= {ids["dev_role_id"], ids["qa_role_id"]}
        assert bucket["allocated_hours"] <= bucket["capacity_hours"] + 0.0001


def test_cost_query_rejects_mixed_currencies_before_grouping():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_resource_project(service)
    _handle(
        service,
        {
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
    )

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

    assert result.ok is False
    assert result.error.code == "mixed_currency"
    assert result.error.details == {
        "requested_currency": "USD",
        "resource_currencies": {
            ids["engineer_resource_id"]: "USD",
            "resource-eu-vendor": "EUR",
        },
    }


def test_due_date_history_query_shape_for_project_target_and_topology_filters():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_dependency_project(service)
    _handle(
        service,
        {
            "action": "set_project_due_at",
            "project_id": ids["project_id"],
            "due_at": _iso(20, 17, NYC),
            "edit_at": _iso(13, 12, NYC),
        },
    )
    _handle(
        service,
        {
            "action": "set_process_due_at",
            "project_id": ids["project_id"],
            "process_id": ids["build_id"],
            "due_at": _iso(19, 17, NYC),
            "edit_at": _iso(14, 12, NYC),
        },
    )

    project_history = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": ids["project_id"],
            "as_of": _iso(15, 12, NYC),
        },
    )
    assert set(project_history) == {
        "project_id",
        "as_of",
        "scope",
        "target_process_id",
        "process_events",
        "project_total_events",
        "current_due_at",
        "current_project_due_at",
        "derived_project_due_at",
    }
    assert project_history["current_project_due_at"] == _iso(20, 17, NYC)
    assert project_history["derived_project_due_at"] == _iso(19, 17, NYC)
    total_event = project_history["project_total_events"][0]
    assert set(total_event) == {
        "event_id",
        "project_id",
        "process_id",
        "process_symbol",
        "mutation_action",
        "edit_at",
        "before_due_at",
        "after_due_at",
        "command_id",
    }
    assert total_event["process_id"] is None
    assert total_event["mutation_action"] == "set_project_due_at"

    target_history = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": ids["project_id"],
            "as_of": _iso(15, 12, NYC),
            "target_process_symbol": "process-build",
            "include_project_total": False,
        },
    )
    assert target_history["scope"] == {
        "type": "target_process",
        "process_id": ids["build_id"],
    }
    assert target_history["target_process_id"] == ids["build_id"]
    assert target_history["current_due_at"] == _iso(19, 17, NYC)
    assert target_history["project_total_events"] == []

    topology_history = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": ids["project_id"],
            "as_of": _iso(15, 12, NYC),
            "scope": {
                "type": "topo_filter",
                "root_process_symbols": ["process-design"],
                "direction": "descendants",
            },
        },
    )
    assert {
        event["process_id"] for event in topology_history["process_events"]
    } <= {ids["design_id"], ids["build_id"]}


def test_due_date_history_by_retired_process_id_survives_replace_and_collapse():
    service = ProjectService(InMemoryProjectRepository())
    ids = _seed_dependency_project(service)
    project_id = ids["project_id"]
    design_id = ids["design_id"]
    build_id = ids["build_id"]
    ship_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-ship",
            "name": "Ship API",
            "effective_at": _iso(13, 9, NYC),
            "duration_business_days": 1,
            "dependencies": [build_id],
            "due_at": _iso(22, 17, NYC),
        },
    )["process_id"]
    _handle(
        service,
        {
            "action": "set_process_due_at",
            "project_id": project_id,
            "process_id": build_id,
            "due_at": _iso(25, 17, NYC),
            "edit_at": _iso(14, 12, NYC),
        },
    )
    replace = _handle(
        service,
        {
            "action": "replace_process_with_subgraph",
            "project_id": project_id,
            "process_id": build_id,
            "edit_at": _iso(15, 9, NYC),
            "processes": [
                {"process_symbol": "api", "name": "API", "duration_hours": 4},
                {"process_symbol": "worker", "name": "Worker", "duration_hours": 4},
            ],
            "dependencies": [
                {"predecessor_symbol": "api", "successor_symbol": "worker"}
            ],
            "root_symbols": ["api"],
            "leaf_symbols": ["worker"],
            "parent_alias_target_symbol": "api",
        },
    )
    api_id = replace["process_ids"][0]
    worker_id = replace["process_ids"][1]
    _handle(
        service,
        {
            "action": "set_process_due_at",
            "project_id": project_id,
            "process_id": api_id,
            "due_at": _iso(20, 17, NYC),
            "edit_at": _iso(15, 12, NYC),
        },
    )
    _handle(
        service,
        {
            "action": "set_process_due_at",
            "project_id": project_id,
            "process_id": worker_id,
            "due_at": _iso(27, 17, NYC),
            "edit_at": _iso(16, 7, NYC),
        },
    )
    collapse = _handle(
        service,
        {
            "action": "collapse_subgraph",
            "project_id": project_id,
            "edit_at": _iso(16, 9, NYC),
            "process_symbols": ["api", "worker"],
            "new_process": {
                "process_symbol": "implementation",
                "name": "Implementation",
            },
        },
    )
    pre_collapse_project_history = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": project_id,
            "as_of": _iso(16, 8, NYC),
        },
    )
    post_collapse_project_history = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": project_id,
            "as_of": _iso(16, 12, NYC),
        },
    )
    historical_project_history = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": project_id,
            "as_of": _iso(15, 8, NYC),
        },
    )
    active_project_history = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": project_id,
            "as_of": _iso(15, 13, NYC),
        },
    )
    historical_topology_history = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": project_id,
            "as_of": _iso(15, 8, NYC),
            "scope": {
                "type": "topo_filter",
                "root_process_symbols": ["process-design"],
                "direction": "descendants",
            },
        },
    )
    active_topology_history = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": project_id,
            "as_of": _iso(15, 13, NYC),
            "scope": {
                "type": "topo_filter",
                "root_process_symbols": ["process-design"],
                "direction": "descendants",
            },
        },
    )
    pre_collapse_topology_history = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": project_id,
            "as_of": _iso(16, 8, NYC),
            "scope": {
                "type": "topo_filter",
                "root_process_symbols": ["process-design"],
                "direction": "descendants",
            },
        },
    )
    post_collapse_topology_history = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": project_id,
            "as_of": _iso(16, 12, NYC),
            "scope": {
                "type": "topo_filter",
                "root_process_symbols": ["process-design"],
                "direction": "descendants",
            },
        },
    )

    replaced_history = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": project_id,
            "as_of": _iso(16, 12, NYC),
            "target_process_id": build_id,
            "include_project_total": False,
        },
    )
    collapsed_history = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": project_id,
            "as_of": _iso(16, 12, NYC),
            "target_process_id": api_id,
            "include_project_total": False,
        },
    )

    assert {design_id, ship_id}.isdisjoint(collapse["retired_process_ids"])
    assert build_id in replace["retired_process_ids"]
    assert api_id in collapse["retired_process_ids"]
    assert worker_id in collapse["retired_process_ids"]
    assert replaced_history["scope"] == {
        "type": "target_process",
        "process_id": build_id,
    }
    assert replaced_history["target_process_id"] == build_id
    assert replaced_history["current_due_at"] == _iso(25, 17, NYC)
    assert replaced_history["project_total_events"] == []
    assert [
        event["process_id"] for event in replaced_history["process_events"]
    ] == [build_id]
    assert historical_project_history["derived_project_due_at"] == _iso(
        25,
        17,
        NYC,
    )
    assert active_project_history["derived_project_due_at"] == _iso(22, 17, NYC)
    assert pre_collapse_project_history["derived_project_due_at"] == _iso(
        27,
        17,
        NYC,
    )
    assert post_collapse_project_history["derived_project_due_at"] == _iso(
        22,
        17,
        NYC,
    )
    assert historical_topology_history["derived_project_due_at"] == _iso(
        25,
        17,
        NYC,
    )
    assert active_topology_history["derived_project_due_at"] == _iso(22, 17, NYC)
    assert pre_collapse_topology_history["derived_project_due_at"] == _iso(
        27,
        17,
        NYC,
    )
    assert post_collapse_topology_history["derived_project_due_at"] == _iso(
        22,
        17,
        NYC,
    )
    assert build_id in {
        event["process_id"] for event in historical_topology_history["process_events"]
    }
    assert build_id not in {
        event["process_id"] for event in active_topology_history["process_events"]
    }
    assert worker_id in {
        event["process_id"] for event in pre_collapse_project_history["process_events"]
    }
    assert worker_id not in {
        event["process_id"] for event in post_collapse_project_history["process_events"]
    }
    assert worker_id in {
        event["process_id"] for event in pre_collapse_topology_history["process_events"]
    }
    assert worker_id not in {
        event["process_id"] for event in post_collapse_topology_history["process_events"]
    }
    assert collapsed_history["scope"] == {
        "type": "target_process",
        "process_id": api_id,
    }
    assert collapsed_history["target_process_id"] == api_id
    assert collapsed_history["current_due_at"] == _iso(20, 17, NYC)
    assert collapsed_history["project_total_events"] == []
    assert [
        event["process_id"] for event in collapsed_history["process_events"]
    ] == [api_id]


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
            "due_at": _iso(14, 17, NYC),
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
            "as_of": _iso(14, 12, NYC),
            "now": _iso(14, 12, NYC),
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

    zero_slack_id = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-zero-slack",
            "name": "Zero Slack",
            "effective_at": _iso(13, 9, NYC),
            "duration_business_days": 1,
            "earliest_start_at": _iso(13, 9, NYC),
            "due_at": _iso(13, 17, NYC),
        },
    )["process_id"]
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

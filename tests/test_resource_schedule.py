import datetime as dt
from importlib import import_module
from typing import NamedTuple

import pytest

UTC = dt.UTC


class WeeklyWindow(NamedTuple):
    weekday: int
    start_local_time: str
    end_local_time: str
    capacity_hours: float


def _at(day: int, hour: int = 9) -> dt.datetime:
    return dt.datetime(2026, 5, day, hour, tzinfo=UTC)


def _resource_schedule_module() -> object:
    try:
        return import_module("projdash.engine.resource_schedule")
    except ModuleNotFoundError as exc:
        if exc.name == "projdash.engine.resource_schedule":
            pytest.fail("expected projdash.engine.resource_schedule module")
        raise


def _resource_schedule_api():
    module = _resource_schedule_module()
    compute = getattr(module, "compute_resource_schedule", None)
    if compute is None:
        pytest.fail("expected projdash.engine.resource_schedule.compute_resource_schedule")
    return compute


def _compute_resource_schedule(input_data: dict[str, object]) -> object:
    return _resource_schedule_api()(input_data)


def _allocation_slice_fingerprint(slices: list[dict[str, object]]) -> object:
    fingerprint = getattr(
        _resource_schedule_module(),
        "allocation_slice_fingerprint",
        None,
    )
    if fingerprint is None:
        pytest.fail(
            "expected projdash.engine.resource_schedule."
            "allocation_slice_fingerprint"
        )
    return fingerprint(slices)


def _compare_resource_schedule_iterations(
    previous: dict[str, object],
    current: dict[str, object],
    *,
    tolerance_hours: float = 0,
) -> object:
    compare = getattr(
        _resource_schedule_module(),
        "compare_resource_schedule_iterations",
        None,
    )
    if compare is None:
        pytest.fail(
            "expected projdash.engine.resource_schedule."
            "compare_resource_schedule_iterations"
        )
    return compare(previous, current, tolerance_hours=tolerance_hours)


def _value(item: object, key: str) -> object:
    if isinstance(item, dict):
        return item[key]
    return getattr(item, key)


def _optional_value(item: object, key: str, default: object = None) -> object:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _data(result: object) -> dict[str, object]:
    if isinstance(result, dict):
        return result
    to_json_dict = getattr(result, "to_json_dict", None)
    if to_json_dict is not None:
        value = to_json_dict()
        if isinstance(value, dict):
            return value
    return {
        "processes": _value(result, "processes"),
        "allocation_slices": _value(result, "allocation_slices"),
        "critical_path_process_ids": _value(result, "critical_path_process_ids"),
        "unallocated_requirements": _value(result, "unallocated_requirements"),
        "converged": _value(result, "converged"),
        "iteration_count": _value(result, "iteration_count"),
        "convergence": _optional_value(result, "convergence", {}),
        "warnings": _optional_value(result, "warnings", []),
    }


def _rows_by_id(data: dict[str, object]) -> dict[str, object]:
    return {str(_value(row, "process_id")): row for row in data["processes"]}


def _unallocated_by_process(data: dict[str, object]) -> dict[str, object]:
    return {
        str(_value(item, "process_id")): item
        for item in data["unallocated_requirements"]
    }


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return str(value)


def _as_datetime(value: object) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str):
        normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            pytest.fail(f"expected timezone-aware datetime, got {value!r}")
        return parsed
    pytest.fail(f"expected datetime-compatible value, got {value!r}")


def _overlap_hours(
    starts_at: dt.datetime,
    ends_at: dt.datetime,
    window_starts_at: dt.datetime,
    window_ends_at: dt.datetime,
) -> float:
    overlap_starts_at = max(starts_at, window_starts_at)
    overlap_ends_at = min(ends_at, window_ends_at)
    if overlap_ends_at <= overlap_starts_at:
        return 0.0
    return (overlap_ends_at - overlap_starts_at).total_seconds() / 3600


def _role(role_id: str, name: str | None = None) -> dict[str, object]:
    return {
        "role_id": role_id,
        "project_id": "project",
        "name": name or role_id.replace("_", " ").title(),
        "active": True,
    }


def _calendar(
    calendar_id: str,
    *,
    timezone: str = "UTC",
    windows: list[WeeklyWindow] | None = None,
    exceptions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    weekly_windows = windows or [
        WeeklyWindow(weekday, "09:00:00", "17:00:00", 8) for weekday in range(5)
    ]
    return {
        "calendar_id": calendar_id,
        "project_id": "project",
        "name": calendar_id.replace("_", " ").title(),
        "timezone": timezone,
        "weekly_windows": [
            {
                "window_id": f"{calendar_id}_{index}",
                "weekday": window.weekday,
                "start_local_time": window.start_local_time,
                "end_local_time": window.end_local_time,
                "capacity_hours": window.capacity_hours,
            }
            for index, window in enumerate(weekly_windows)
        ],
        "exceptions": exceptions or [],
        "active": True,
    }


def _resource(
    resource_id: str,
    *,
    role_ids: list[str] | None = None,
    calendar_id: str = "cal_utc",
    available_from_at: dt.datetime | None = None,
    available_until_at: dt.datetime | None = None,
    holidays: list[dict[str, object]] | None = None,
    cost_rate: str = "100.00",
) -> dict[str, object]:
    return {
        "resource_id": resource_id,
        "project_id": "project",
        "name": resource_id.replace("_", " ").title(),
        "role_ids": role_ids or ["role_dev"],
        "calendar_id": calendar_id,
        "available_from_at": available_from_at or _at(13),
        "available_until_at": available_until_at,
        "cost_rate": cost_rate,
        "cost_unit": "hour",
        "cost_currency": "USD",
        "holidays": holidays or [],
        "active": True,
    }


def _allocation_effort_by_resource(data: dict[str, object]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for allocation in data["allocation_slices"]:
        resource_id = str(_value(allocation, "resource_id"))
        totals[resource_id] = totals.get(resource_id, 0.0) + float(
            _value(allocation, "effort_hours")
        )
    return totals


def _allocation_order(data: dict[str, object]) -> list[tuple[str, str, str]]:
    allocations = sorted(
        data["allocation_slices"],
        key=lambda allocation: (
            _as_datetime(_value(allocation, "starts_at")),
            _as_datetime(_value(allocation, "ends_at")),
            str(_value(allocation, "process_id")),
            str(_value(allocation, "requirement_id")),
            str(_value(allocation, "resource_id")),
        ),
    )
    return [
        (
            str(_value(allocation, "process_id")),
            str(_value(allocation, "requirement_id")),
            str(_value(allocation, "resource_id")),
        )
        for allocation in allocations
    ]


def _allocated_effort_in_window(
    data: dict[str, object],
    *,
    resource_id: str,
    starts_at: dt.datetime,
    ends_at: dt.datetime,
) -> float:
    total = 0.0
    for allocation in data["allocation_slices"]:
        if _value(allocation, "resource_id") != resource_id:
            continue
        slice_starts_at = _as_datetime(_value(allocation, "starts_at"))
        slice_ends_at = _as_datetime(_value(allocation, "ends_at"))
        duration_hours = _overlap_hours(slice_starts_at, slice_ends_at, starts_at, ends_at)
        if duration_hours == 0:
            continue
        slice_duration_hours = _overlap_hours(
            slice_starts_at,
            slice_ends_at,
            slice_starts_at,
            slice_ends_at,
        )
        total += float(_value(allocation, "effort_hours")) * (
            duration_hours / slice_duration_hours
        )
    return total


def _allocated_effort_for_requirement_in_window(
    data: dict[str, object],
    *,
    requirement_id: str,
    starts_at: dt.datetime,
    ends_at: dt.datetime,
) -> float:
    total = 0.0
    for allocation in data["allocation_slices"]:
        if _value(allocation, "requirement_id") != requirement_id:
            continue
        slice_starts_at = _as_datetime(_value(allocation, "starts_at"))
        slice_ends_at = _as_datetime(_value(allocation, "ends_at"))
        duration_hours = _overlap_hours(slice_starts_at, slice_ends_at, starts_at, ends_at)
        if duration_hours == 0:
            continue
        slice_duration_hours = _overlap_hours(
            slice_starts_at,
            slice_ends_at,
            slice_starts_at,
            slice_ends_at,
        )
        total += float(_value(allocation, "effort_hours")) * (
            duration_hours / slice_duration_hours
        )
    return total


def _assert_resource_utilization_never_exceeds_capacity(
    data: dict[str, object],
) -> None:
    allocations_by_resource: dict[str, list[object]] = {}
    for allocation in data["allocation_slices"]:
        resource_id = str(_value(allocation, "resource_id"))
        allocations_by_resource.setdefault(resource_id, []).append(allocation)

    for resource_id, allocations in allocations_by_resource.items():
        boundaries = sorted(
            {
                boundary
                for allocation in allocations
                for boundary in (
                    _as_datetime(_value(allocation, "starts_at")),
                    _as_datetime(_value(allocation, "ends_at")),
                )
            }
        )
        for starts_at, ends_at in zip(boundaries, boundaries[1:], strict=False):
            if starts_at == ends_at:
                continue
            utilization = 0.0
            for allocation in allocations:
                slice_starts_at = _as_datetime(_value(allocation, "starts_at"))
                slice_ends_at = _as_datetime(_value(allocation, "ends_at"))
                if slice_starts_at >= ends_at or slice_ends_at <= starts_at:
                    continue
                slice_duration_hours = _overlap_hours(
                    slice_starts_at,
                    slice_ends_at,
                    slice_starts_at,
                    slice_ends_at,
                )
                utilization += float(_value(allocation, "capacity_hours")) / (
                    slice_duration_hours
                )
            assert utilization <= 1.0001, (resource_id, starts_at, ends_at, utilization)


def _base_input(
    *,
    processes: list[dict[str, object]],
    role_requirements: list[dict[str, object]],
    horizon_ends_at: dt.datetime | None = None,
    blockers: list[dict[str, object]] | None = None,
    roles: list[dict[str, object]] | None = None,
    resources: list[dict[str, object]] | None = None,
    calendars: list[dict[str, object]] | None = None,
    options: dict[str, object] | None = None,
) -> dict[str, object]:
    schedule_options = {
        "planning_granularity": "hour",
        "horizon_starts_at": _at(13),
        "horizon_ends_at": horizon_ends_at or _at(16, 17),
        "max_iterations": 20,
        "convergence_tolerance_hours": 0,
        "blocked_policy": "include_normally",
        "include_allocation_slices": True,
    }
    if options:
        schedule_options.update(options)

    return {
        "project_id": "project",
        "project_start_at": _at(13),
        "as_of": _at(13),
        "now": _at(13),
        "processes": processes,
        "dependencies": [
            {"predecessor_process_id": dependency, "successor_process_id": process["process_id"]}
            for process in processes
            for dependency in process.get("dependencies", [])
        ],
        "role_requirements": role_requirements,
        "roles": roles or [_role("role_dev", "Developer")],
        "resources": resources or [_resource("res_alex")],
        "calendars": calendars or [_calendar("cal_utc")],
        "blockers": blockers or [],
        "options": schedule_options,
    }


def _process(
    process_id: str,
    *,
    dependencies: list[str] | None = None,
    duration_business_days: int = 1,
    earliest_start_at: dt.datetime | None = None,
    delay_after_dependencies_business_days: int = 0,
) -> dict[str, object]:
    process = {
        "process_id": process_id,
        "name": process_id.replace("_", " ").title(),
        "dependencies": dependencies or [],
        "duration_business_days": duration_business_days,
        "explicit_status": "planned",
    }
    if earliest_start_at is not None:
        process["earliest_start_at"] = earliest_start_at
    if delay_after_dependencies_business_days:
        process["delay_after_dependencies_business_days"] = (
            delay_after_dependencies_business_days
        )
    return process


def _requirement(
    process_id: str,
    effort_hours: float,
    *,
    requirement_id: str | None = None,
    role_id: str = "role_dev",
    required_resource_count: int = 1,
    allocation_policy: str = "split_allowed",
    min_allocation_hours_per_day: float | None = None,
    max_allocation_hours_per_day: float | None = None,
) -> dict[str, object]:
    requirement: dict[str, object] = {
        "requirement_id": requirement_id or f"req_{process_id}",
        "project_id": "project",
        "process_id": process_id,
        "role_id": role_id,
        "effort_hours": effort_hours,
        "required_resource_count": required_resource_count,
        "allocation_policy": allocation_policy,
    }
    if min_allocation_hours_per_day is not None:
        requirement["min_allocation_hours_per_day"] = min_allocation_hours_per_day
    if max_allocation_hours_per_day is not None:
        requirement["max_allocation_hours_per_day"] = max_allocation_hours_per_day
    return requirement


def _convergence_row(
    *,
    process_id: str = "task",
    ready_at: dt.datetime | None = None,
    starts_at: dt.datetime | None = None,
    ends_at: dt.datetime | None = None,
    allocation_state: str = "complete",
) -> dict[str, object]:
    return {
        "process_id": process_id,
        "ready_at": ready_at,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "allocation_state": allocation_state,
    }


def _unallocated_requirement(
    reason: str,
    *,
    process_id: str = "task",
    requirement_id: str = "req_task",
) -> dict[str, object]:
    return {
        "process_id": process_id,
        "requirement_id": requirement_id,
        "reason": reason,
        "first_feasible_starts_at": None,
    }


def _allocation_slice(**overrides: object) -> dict[str, object]:
    allocation: dict[str, object] = {
        "slice_id": "slice_task_1",
        "project_id": "project",
        "process_id": "task",
        "requirement_id": "req_task",
        "role_id": "role_dev",
        "resource_id": "res_alex",
        "starts_at": _at(13),
        "ends_at": _at(13, 11),
        "effort_hours": 2.0,
        "capacity_hours": 2.0,
        "cost_amount": None,
        "cost_currency": "USD",
        "iteration": 1,
    }
    allocation.update(overrides)
    return allocation


def _iteration_state(
    *,
    rows: list[dict[str, object]] | None = None,
    unallocated: list[dict[str, object]] | None = None,
    allocation_slices: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "processes": rows or [_convergence_row()],
        "unallocated_requirements": unallocated or [],
        "allocation_slices": allocation_slices or [],
    }


def _changed_process_ids(comparison: object) -> set[str]:
    return {str(process_id) for process_id in _value(comparison, "changed_process_ids")}


def _reason_change_tuples(comparison: object) -> set[tuple[object, ...]]:
    return {
        (
            _value(change, "process_id"),
            _optional_value(change, "requirement_id"),
            _optional_value(change, "before_reason"),
            _optional_value(change, "after_reason"),
        )
        for change in _value(comparison, "reason_changes")
    }


def _reason_change_tuple_list(comparison: object) -> list[tuple[object, ...]]:
    return [
        (
            _value(change, "process_id"),
            _optional_value(change, "requirement_id"),
            _optional_value(change, "before_reason"),
            _optional_value(change, "after_reason"),
        )
        for change in _value(comparison, "reason_changes")
    ]


def test_resource_allocation_uses_global_contention_ledger_without_overbooking():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("alpha"), _process("beta")],
                role_requirements=[
                    _requirement("alpha", 6),
                    _requirement("beta", 6),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert _iso(_value(rows["alpha"], "starts_at")) == "2026-05-13T09:00:00+00:00"
    assert _iso(_value(rows["alpha"], "ends_at")) == "2026-05-14T13:00:00+00:00"
    assert _iso(_value(rows["beta"], "starts_at")) == "2026-05-13T09:00:00+00:00"
    assert _iso(_value(rows["beta"], "ends_at")) == "2026-05-14T13:00:00+00:00"
    assert float(_value(rows["beta"], "resource_delay_hours")) == 0.0

    assert sum(
        float(_value(allocation, "effort_hours"))
        for allocation in data["allocation_slices"]
    ) == 12.0
    assert _allocated_effort_for_requirement_in_window(
        data,
        requirement_id="req_alpha",
        starts_at=_at(13),
        ends_at=_at(13, 10),
    ) == pytest.approx(0.5)
    assert _allocated_effort_for_requirement_in_window(
        data,
        requirement_id="req_beta",
        starts_at=_at(13),
        ends_at=_at(13, 10),
    ) == pytest.approx(0.5)
    _assert_resource_utilization_never_exceeds_capacity(data)


def test_ready_requirements_allocate_breadth_first_by_project_hour_bucket():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("alpha"), _process("beta")],
                role_requirements=[
                    _requirement("alpha", 8, requirement_id="req_alpha"),
                    _requirement("beta", 1, requirement_id="req_beta"),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert _value(rows["alpha"], "allocation_state") == "complete"
    assert _value(rows["beta"], "allocation_state") == "complete"
    assert _iso(_value(rows["alpha"], "starts_at")) == "2026-05-13T09:00:00+00:00"
    assert _iso(_value(rows["beta"], "starts_at")) == "2026-05-13T09:00:00+00:00"
    assert _iso(_value(rows["beta"], "ends_at")) == "2026-05-13T11:00:00+00:00"
    assert _iso(_value(rows["alpha"], "ends_at")) == "2026-05-14T10:00:00+00:00"
    assert _allocated_effort_for_requirement_in_window(
        data,
        requirement_id="req_alpha",
        starts_at=_at(13),
        ends_at=_at(13, 10),
    ) == pytest.approx(0.5)
    assert _allocated_effort_for_requirement_in_window(
        data,
        requirement_id="req_beta",
        starts_at=_at(13),
        ends_at=_at(13, 10),
    ) == pytest.approx(0.5)
    assert _allocated_effort_for_requirement_in_window(
        data,
        requirement_id="req_alpha",
        starts_at=_at(13, 10),
        ends_at=_at(13, 11),
    ) == pytest.approx(0.5)
    _assert_resource_utilization_never_exceeds_capacity(data)


def test_resource_schedule_iterates_until_dependency_finish_converges():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process("design"),
                    _process("build", dependencies=["design"]),
                ],
                role_requirements=[
                    _requirement("design", 10),
                    _requirement("build", 1),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert data["converged"] is True
    assert int(data["iteration_count"]) >= 2
    assert _iso(_value(rows["design"], "ends_at")) == "2026-05-14T11:00:00+00:00"
    assert _iso(_value(rows["build"], "ready_at")) == "2026-05-14T11:00:00+00:00"
    assert _iso(_value(rows["build"], "ends_at")) == "2026-05-14T12:00:00+00:00"
    assert _optional_value(data, "warnings", []) == []


def test_resource_schedule_respects_process_earliest_start_at_constraint():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process(
                        "build",
                        earliest_start_at=_at(14, 11),
                    ),
                ],
                role_requirements=[
                    _requirement("build", 2),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert _iso(_value(rows["build"], "ready_at")) == "2026-05-14T11:00:00+00:00"
    assert _iso(_value(rows["build"], "starts_at")) == "2026-05-14T11:00:00+00:00"
    assert _iso(_value(rows["build"], "ends_at")) == "2026-05-14T13:00:00+00:00"


def test_resource_schedule_respects_dependency_delay_constraint():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process("design"),
                    _process(
                        "build",
                        dependencies=["design"],
                        delay_after_dependencies_business_days=1,
                    ),
                ],
                role_requirements=[
                    _requirement("design", 1),
                    _requirement("build", 1),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert _iso(_value(rows["design"], "ends_at")) == "2026-05-13T10:00:00+00:00"
    assert _iso(_value(rows["build"], "ready_at")) == "2026-05-14T10:00:00+00:00"
    assert _iso(_value(rows["build"], "starts_at")) == "2026-05-14T10:00:00+00:00"
    assert _iso(_value(rows["build"], "ends_at")) == "2026-05-14T11:00:00+00:00"


def test_ready_queue_orders_by_ready_time_before_later_candidates():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process("later_ready", earliest_start_at=_at(13, 10)),
                    _process("earlier_ready"),
                ],
                role_requirements=[
                    _requirement(
                        "later_ready",
                        1,
                        requirement_id="req_later_ready",
                    ),
                    _requirement(
                        "earlier_ready",
                        2,
                        requirement_id="req_earlier_ready",
                    ),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert _iso(_value(rows["earlier_ready"], "starts_at")) == (
        "2026-05-13T09:00:00+00:00"
    )
    assert _iso(_value(rows["earlier_ready"], "ends_at")) == (
        "2026-05-13T12:00:00+00:00"
    )
    assert _iso(_value(rows["later_ready"], "starts_at")) == (
        "2026-05-13T10:00:00+00:00"
    )
    assert _iso(_value(rows["later_ready"], "ends_at")) == (
        "2026-05-13T12:00:00+00:00"
    )
    assert _allocation_order(data) == [
        ("earlier_ready", "req_earlier_ready", "res_alex"),
        ("earlier_ready", "req_earlier_ready", "res_alex"),
        ("later_ready", "req_later_ready", "res_alex"),
        ("earlier_ready", "req_earlier_ready", "res_alex"),
        ("later_ready", "req_later_ready", "res_alex"),
    ]


def test_ready_demand_ties_do_not_use_dependency_only_latest_finish():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process("aa_early_finish_loose_leaf", duration_business_days=0),
                    _process("tight_child", dependencies=["zz_late_finish_first"]),
                    _process("zz_late_finish_first"),
                ],
                role_requirements=[
                    _requirement(
                        "aa_early_finish_loose_leaf",
                        1,
                        requirement_id="req_aa_early_finish_loose_leaf",
                    ),
                    _requirement(
                        "zz_late_finish_first",
                        2,
                        requirement_id="req_zz_late_finish_first",
                    ),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert _as_datetime(
        _value(rows["aa_early_finish_loose_leaf"], "dependency_only_ends_at")
    ) < _as_datetime(_value(rows["zz_late_finish_first"], "dependency_only_ends_at"))
    assert "aa_early_finish_loose_leaf" < "zz_late_finish_first"
    assert _iso(_value(rows["zz_late_finish_first"], "starts_at")) == (
        "2026-05-13T09:00:00+00:00"
    )
    assert _iso(_value(rows["zz_late_finish_first"], "ends_at")) == (
        "2026-05-13T12:00:00+00:00"
    )
    assert _iso(_value(rows["aa_early_finish_loose_leaf"], "starts_at")) == (
        "2026-05-13T09:00:00+00:00"
    )
    assert _iso(_value(rows["aa_early_finish_loose_leaf"], "ends_at")) == (
        "2026-05-13T11:00:00+00:00"
    )
    assert _allocation_order(data) == [
        (
            "aa_early_finish_loose_leaf",
            "req_aa_early_finish_loose_leaf",
            "res_alex",
        ),
        ("zz_late_finish_first", "req_zz_late_finish_first", "res_alex"),
        (
            "aa_early_finish_loose_leaf",
            "req_aa_early_finish_loose_leaf",
            "res_alex",
        ),
        ("zz_late_finish_first", "req_zz_late_finish_first", "res_alex"),
        ("zz_late_finish_first", "req_zz_late_finish_first", "res_alex"),
    ]


def test_ready_queue_ties_by_topological_index_before_process_id():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process("root", duration_business_days=0),
                    _process("zz_topo_first", dependencies=["root"]),
                    _process("aa_process_id_first", dependencies=["root"]),
                ],
                role_requirements=[
                    _requirement(
                        "aa_process_id_first",
                        1,
                        requirement_id="req_aa_process_id_first",
                    ),
                    _requirement(
                        "zz_topo_first",
                        2,
                        requirement_id="req_zz_topo_first",
                    ),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert _iso(_value(rows["zz_topo_first"], "ready_at")) == (
        _iso(_value(rows["aa_process_id_first"], "ready_at"))
    )
    assert _iso(_value(rows["zz_topo_first"], "starts_at")) == (
        "2026-05-13T09:00:00+00:00"
    )
    assert _iso(_value(rows["zz_topo_first"], "ends_at")) == (
        "2026-05-13T12:00:00+00:00"
    )
    assert _iso(_value(rows["aa_process_id_first"], "starts_at")) == (
        "2026-05-13T09:00:00+00:00"
    )
    assert _iso(_value(rows["aa_process_id_first"], "ends_at")) == (
        "2026-05-13T11:00:00+00:00"
    )
    assert _allocation_order(data) == [
        ("aa_process_id_first", "req_aa_process_id_first", "res_alex"),
        ("zz_topo_first", "req_zz_topo_first", "res_alex"),
        ("aa_process_id_first", "req_aa_process_id_first", "res_alex"),
        ("zz_topo_first", "req_zz_topo_first", "res_alex"),
        ("zz_topo_first", "req_zz_topo_first", "res_alex"),
    ]


def test_ready_queue_ties_by_topological_order_then_requirement_id():
    process_id_data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process("zz_process"),
                    _process("aa_process"),
                ],
                role_requirements=[
                    _requirement(
                        "zz_process",
                        1,
                        requirement_id="req_zz_process",
                    ),
                    _requirement(
                        "aa_process",
                        2,
                        requirement_id="req_aa_process",
                    ),
                ],
            )
        )
    )
    requirement_id_data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("multi_requirement")],
                role_requirements=[
                    _requirement(
                        "multi_requirement",
                        1,
                        requirement_id="req_z",
                    ),
                    _requirement(
                        "multi_requirement",
                        1,
                        requirement_id="req_a",
                    ),
                ],
            )
        )
    )

    process_rows = _rows_by_id(process_id_data)
    assert _iso(_value(process_rows["zz_process"], "starts_at")) == (
        "2026-05-13T09:00:00+00:00"
    )
    assert _iso(_value(process_rows["aa_process"], "starts_at")) == (
        "2026-05-13T09:00:00+00:00"
    )
    assert _iso(_value(process_rows["zz_process"], "ends_at")) == (
        "2026-05-13T11:00:00+00:00"
    )
    assert _iso(_value(process_rows["aa_process"], "ends_at")) == (
        "2026-05-13T12:00:00+00:00"
    )
    assert _allocation_order(process_id_data) == [
        ("aa_process", "req_aa_process", "res_alex"),
        ("zz_process", "req_zz_process", "res_alex"),
        ("aa_process", "req_aa_process", "res_alex"),
        ("zz_process", "req_zz_process", "res_alex"),
        ("aa_process", "req_aa_process", "res_alex"),
    ]
    assert _allocation_order(requirement_id_data) == [
        ("multi_requirement", "req_a", "res_alex"),
        ("multi_requirement", "req_z", "res_alex"),
        ("multi_requirement", "req_a", "res_alex"),
        ("multi_requirement", "req_z", "res_alex"),
    ]


def test_eligible_resource_ties_by_earliest_capacity():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build")],
                role_requirements=[_requirement("build", 1)],
                resources=[
                    _resource("res_later", available_from_at=_at(13, 10)),
                    _resource("res_earlier", available_from_at=_at(13, 9)),
                ],
            )
        )
    )

    assert _allocation_order(data) == [("build", "req_build", "res_earlier")]


def test_eligible_resource_ties_by_projected_cost_after_capacity_time():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build")],
                role_requirements=[_requirement("build", 1)],
                resources=[
                    _resource("res_expensive", cost_rate="200.00"),
                    _resource("res_cheap", cost_rate="50.00"),
                ],
            )
        )
    )

    assert _allocation_order(data) == [("build", "req_build", "res_cheap")]


def test_eligible_resource_prefers_lower_cost_before_resource_id():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build")],
                role_requirements=[_requirement("build", 1)],
                resources=[
                    _resource("a_expensive", cost_rate="200.00"),
                    _resource("z_cheap", cost_rate="50.00"),
                ],
            )
        )
    )

    assert _allocation_order(data) == [("build", "req_build", "z_cheap")]


def test_eligible_resource_ties_by_resource_id_after_capacity_and_cost():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build")],
                role_requirements=[_requirement("build", 1)],
                resources=[
                    _resource("res_zed"),
                    _resource("res_ada"),
                ],
            )
        )
    )

    assert _allocation_order(data) == [("build", "req_build", "res_ada")]


def test_required_resource_count_is_concurrency_ceiling_not_staffing_minimum():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build")],
                role_requirements=[
                    _requirement(
                        "build",
                        2,
                        required_resource_count=3,
                    )
                ],
                resources=[
                    _resource("res_only"),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert _value(rows["build"], "allocation_state") == "complete"
    assert _iso(_value(rows["build"], "starts_at")) == "2026-05-13T09:00:00+00:00"
    assert _iso(_value(rows["build"], "ends_at")) == "2026-05-13T11:00:00+00:00"
    assert _allocation_effort_by_resource(data) == {"res_only": 2.0}
    assert data["unallocated_requirements"] == []


def test_no_calendar_capacity_reports_structured_unallocated_reason():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build")],
                role_requirements=[_requirement("build", 2)],
                calendars=[
                    _calendar(
                        "cal_utc",
                        windows=[
                            WeeklyWindow(6, "09:00:00", "17:00:00", 8),
                        ],
                    )
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    unallocated = _unallocated_by_process(data)
    item = unallocated["build"]
    assert _value(rows["build"], "allocation_state") == "unallocated"
    assert _iso(_value(rows["build"], "ready_at")) == "2026-05-13T09:00:00+00:00"
    assert _value(rows["build"], "starts_at") is None
    assert _value(rows["build"], "ends_at") is None
    assert _value(item, "reason") == "no_calendar_capacity"
    assert _value(item, "eligible_resource_ids") == ["res_alex"]
    assert _value(item, "first_feasible_starts_at") is None
    assert _value(item, "message")
    assert data["allocation_slices"] == []


@pytest.mark.parametrize(
    ("blocked_policy", "blocked_state"),
    [
        ("exclude", "unallocated"),
        ("include_as_zero_capacity", "blocked_zero_capacity"),
    ],
)
def test_blocked_or_null_predecessor_prevents_successor_allocation(
    blocked_policy: str,
    blocked_state: str,
):
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process("blocked"),
                    _process("successor", dependencies=["blocked"]),
                ],
                role_requirements=[
                    _requirement("blocked", 2),
                    _requirement("successor", 1),
                ],
                blockers=[
                    {
                        "blocker_id": "blocker_1",
                        "project_id": "project",
                        "process_id": "blocked",
                        "created_at": _at(13),
                        "resolved_at": None,
                    }
                ],
                options={"blocked_policy": blocked_policy},
            )
        )
    )

    rows = _rows_by_id(data)
    unallocated = _unallocated_by_process(data)

    assert _value(rows["blocked"], "allocation_state") == blocked_state
    if blocked_policy == "include_as_zero_capacity":
        assert _iso(_value(rows["blocked"], "ready_at")) == "2026-05-13T09:00:00+00:00"
    assert _value(rows["blocked"], "starts_at") is None
    assert _value(rows["blocked"], "ends_at") is None
    assert _value(rows["successor"], "allocation_state") == "unallocated"
    assert _value(rows["successor"], "ready_at") is None
    assert _value(rows["successor"], "starts_at") is None
    assert _value(rows["successor"], "ends_at") is None
    assert _value(unallocated["successor"], "reason") == "predecessor_unallocated"
    assert _value(unallocated["successor"], "first_feasible_starts_at") is None
    assert not [
        allocation
        for allocation in data["allocation_slices"]
        if _value(allocation, "process_id") == "blocked"
    ]
    if blocked_policy == "exclude":
        assert _value(unallocated["blocked"], "reason") == "blocked"


def test_include_normally_schedules_blocked_process_and_successor():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process("blocked"),
                    _process("successor", dependencies=["blocked"]),
                ],
                role_requirements=[
                    _requirement("blocked", 2),
                    _requirement("successor", 1),
                ],
                blockers=[
                    {
                        "blocker_id": "blocker_1",
                        "project_id": "project",
                        "process_id": "blocked",
                        "created_at": _at(13),
                        "resolved_at": None,
                    }
                ],
                options={"blocked_policy": "include_normally"},
            )
        )
    )

    rows = _rows_by_id(data)
    allocated_process_ids = {
        str(_value(allocation, "process_id"))
        for allocation in data["allocation_slices"]
    }

    assert _value(rows["blocked"], "allocation_state") == "complete"
    assert _iso(_value(rows["blocked"], "ready_at")) == "2026-05-13T09:00:00+00:00"
    assert _iso(_value(rows["blocked"], "starts_at")) == "2026-05-13T09:00:00+00:00"
    assert _iso(_value(rows["blocked"], "ends_at")) == "2026-05-13T11:00:00+00:00"
    assert _value(rows["successor"], "allocation_state") == "complete"
    assert _iso(_value(rows["successor"], "ready_at")) == "2026-05-13T11:00:00+00:00"
    assert _iso(_value(rows["successor"], "starts_at")) == "2026-05-13T11:00:00+00:00"
    assert _iso(_value(rows["successor"], "ends_at")) == "2026-05-13T12:00:00+00:00"
    assert data["unallocated_requirements"] == []
    assert {"blocked", "successor"}.issubset(allocated_process_ids)


def test_resource_schedule_allocation_slices_are_optional_and_shape_complete():
    input_data = _base_input(
        processes=[_process("build")],
        role_requirements=[_requirement("build", 2, requirement_id="req_build_dev")],
        options={"include_allocation_slices": False},
    )
    without_slices = _data(_compute_resource_schedule(input_data))
    assert without_slices["allocation_slices"] == []

    with_slices_input = _base_input(
        processes=[_process("build")],
        role_requirements=[_requirement("build", 2, requirement_id="req_build_dev")],
        options={"include_allocation_slices": True},
    )
    with_slices = _data(_compute_resource_schedule(with_slices_input))
    allocations = with_slices["allocation_slices"]

    assert len(allocations) == 1
    allocation = allocations[0]
    assert _value(allocation, "slice_id")
    assert _value(allocation, "project_id") == "project"
    assert _value(allocation, "process_id") == "build"
    assert _value(allocation, "requirement_id") == "req_build_dev"
    assert _value(allocation, "role_id") == "role_dev"
    assert _value(allocation, "resource_id") == "res_alex"
    assert _iso(_value(allocation, "starts_at")) == "2026-05-13T09:00:00+00:00"
    assert _iso(_value(allocation, "ends_at")) == "2026-05-13T11:00:00+00:00"
    assert float(_value(allocation, "effort_hours")) == 2.0
    assert float(_value(allocation, "capacity_hours")) == 2.0
    assert _value(allocation, "cost_amount") is None
    assert _value(allocation, "cost_currency") == "USD"
    assert _value(allocation, "iteration") == with_slices["iteration_count"]


def test_allocation_slice_ids_are_stable_for_identical_query_inputs():
    input_data = _base_input(
        processes=[_process("build")],
        role_requirements=[_requirement("build", 10, requirement_id="req_build_dev")],
    )

    first = _data(_compute_resource_schedule(input_data))
    second = _data(_compute_resource_schedule(input_data))

    assert [
        _value(allocation, "slice_id")
        for allocation in first["allocation_slices"]
    ] == [
        _value(allocation, "slice_id")
        for allocation in second["allocation_slices"]
    ]


def test_allocation_slice_ids_are_unique_within_multi_slice_result():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build")],
                role_requirements=[
                    _requirement("build", 12, requirement_id="req_build_dev"),
                ],
                calendars=[
                    _calendar(
                        "cal_utc",
                        windows=[
                            WeeklyWindow(2, "09:00:00", "13:00:00", 4),
                            WeeklyWindow(3, "09:00:00", "13:00:00", 4),
                            WeeklyWindow(4, "09:00:00", "13:00:00", 4),
                        ],
                    )
                ],
            )
        )
    )

    slice_ids = [
        str(_value(allocation, "slice_id"))
        for allocation in data["allocation_slices"]
    ]
    assert len(slice_ids) > 1
    assert len(slice_ids) == len(set(slice_ids))


def test_allocation_slice_ids_change_for_material_options_and_intervals():
    base = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build")],
                role_requirements=[
                    _requirement("build", 2, requirement_id="req_build_dev"),
                ],
            )
        )
    )
    changed_options = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build")],
                role_requirements=[
                    _requirement("build", 2, requirement_id="req_build_dev"),
                ],
                options={"convergence_tolerance_hours": 0.5},
            )
        )
    )
    changed_interval = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build", earliest_start_at=_at(13, 10))],
                role_requirements=[
                    _requirement("build", 2, requirement_id="req_build_dev"),
                ],
            )
        )
    )

    base_allocation = base["allocation_slices"][0]
    changed_options_allocation = changed_options["allocation_slices"][0]
    changed_interval_allocation = changed_interval["allocation_slices"][0]

    assert _iso(_value(base_allocation, "starts_at")) == _iso(
        _value(changed_options_allocation, "starts_at")
    )
    assert _iso(_value(base_allocation, "ends_at")) == _iso(
        _value(changed_options_allocation, "ends_at")
    )
    assert _value(base_allocation, "slice_id") != _value(
        changed_options_allocation,
        "slice_id",
    )
    assert _iso(_value(base_allocation, "starts_at")) != _iso(
        _value(changed_interval_allocation, "starts_at")
    )
    assert _value(base_allocation, "slice_id") != _value(
        changed_interval_allocation,
        "slice_id",
    )


def test_water_filling_uniformly_allocates_then_redistributes_for_unavailability():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build")],
                role_requirements=[
                    _requirement(
                        "build",
                        12,
                        required_resource_count=2,
                    )
                ],
                resources=[
                    _resource("res_alex", calendar_id="cal_full_day"),
                    _resource("res_blair", calendar_id="cal_morning_only"),
                ],
                calendars=[
                    _calendar("cal_full_day"),
                    _calendar(
                        "cal_morning_only",
                        windows=[
                            WeeklyWindow(weekday, "09:00:00", "13:00:00", 4)
                            for weekday in range(5)
                        ],
                    ),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert data["converged"] is True
    assert _iso(_value(rows["build"], "starts_at")) == "2026-05-13T09:00:00+00:00"
    assert _iso(_value(rows["build"], "ends_at")) == "2026-05-13T17:00:00+00:00"
    assert _allocation_effort_by_resource(data) == {
        "res_alex": 8.0,
        "res_blair": 4.0,
    }
    assert _allocated_effort_in_window(
        data,
        resource_id="res_alex",
        starts_at=_at(13),
        ends_at=_at(13, 13),
    ) == pytest.approx(4.0)
    assert _allocated_effort_in_window(
        data,
        resource_id="res_blair",
        starts_at=_at(13),
        ends_at=_at(13, 13),
    ) == pytest.approx(4.0)
    assert _allocated_effort_in_window(
        data,
        resource_id="res_alex",
        starts_at=_at(13, 13),
        ends_at=_at(13, 17),
    ) == pytest.approx(4.0)
    assert _allocated_effort_in_window(
        data,
        resource_id="res_blair",
        starts_at=_at(13, 13),
        ends_at=_at(13, 17),
    ) == pytest.approx(0.0)
    _assert_resource_utilization_never_exceeds_capacity(data)


def test_water_filling_redistributes_after_selected_candidate_hits_daily_cap():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build")],
                role_requirements=[
                    _requirement(
                        "build",
                        7,
                        required_resource_count=3,
                        max_allocation_hours_per_day=3,
                    )
                ],
                resources=[
                    _resource("res_alex", calendar_id="cal_parallel_day"),
                    _resource(
                        "res_blair",
                        calendar_id="cal_parallel_day",
                        available_from_at=_at(13, 10),
                    ),
                    _resource(
                        "res_casey",
                        calendar_id="cal_parallel_day",
                        available_from_at=_at(13, 10),
                    ),
                ],
                calendars=[
                    _calendar(
                        "cal_parallel_day",
                        windows=[
                            WeeklyWindow(weekday, "09:00:00", "17:00:00", 16)
                            for weekday in range(5)
                        ],
                    )
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert _value(rows["build"], "allocation_state") == "complete"
    assert _iso(_value(rows["build"], "starts_at")) == "2026-05-13T09:00:00+00:00"
    assert _iso(_value(rows["build"], "ends_at")) == "2026-05-13T11:00:00+00:00"
    assert _allocation_effort_by_resource(data) == {
        "res_alex": 3.0,
        "res_blair": 2.0,
        "res_casey": 2.0,
    }
    assert _allocated_effort_in_window(
        data,
        resource_id="res_alex",
        starts_at=_at(13),
        ends_at=_at(13, 10),
    ) == pytest.approx(2.0)
    assert _allocated_effort_in_window(
        data,
        resource_id="res_alex",
        starts_at=_at(13, 10),
        ends_at=_at(13, 11),
    ) == pytest.approx(1.0)
    assert _allocated_effort_in_window(
        data,
        resource_id="res_blair",
        starts_at=_at(13, 10),
        ends_at=_at(13, 11),
    ) == pytest.approx(2.0)
    assert _allocated_effort_in_window(
        data,
        resource_id="res_casey",
        starts_at=_at(13, 10),
        ends_at=_at(13, 11),
    ) == pytest.approx(2.0)
    assert all(
        allocated <= 3.0001
        for allocated in _allocation_effort_by_resource(data).values()
    )


def test_min_allocation_skips_tiny_fragment_when_more_work_remains():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build")],
                role_requirements=[
                    _requirement(
                        "build",
                        4,
                        min_allocation_hours_per_day=2,
                    )
                ],
                calendars=[
                    _calendar(
                        "cal_utc",
                        windows=[
                            WeeklyWindow(2, "09:00:00", "10:00:00", 1),
                            WeeklyWindow(2, "10:00:00", "14:00:00", 4),
                        ],
                    )
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert _value(rows["build"], "allocation_state") == "complete"
    assert _iso(_value(rows["build"], "starts_at")) == "2026-05-13T10:00:00+00:00"
    assert _iso(_value(rows["build"], "ends_at")) == "2026-05-13T14:00:00+00:00"
    assert _allocated_effort_in_window(
        data,
        resource_id="res_alex",
        starts_at=_at(13),
        ends_at=_at(13, 10),
    ) == pytest.approx(0.0)
    assert _allocated_effort_in_window(
        data,
        resource_id="res_alex",
        starts_at=_at(13, 10),
        ends_at=_at(13, 14),
    ) == pytest.approx(4.0)


def test_min_allocation_schedules_final_remaining_effort_below_minimum():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build")],
                role_requirements=[
                    _requirement(
                        "build",
                        5,
                        min_allocation_hours_per_day=2,
                    )
                ],
                calendars=[
                    _calendar(
                        "cal_utc",
                        windows=[
                            WeeklyWindow(2, "09:00:00", "13:00:00", 4),
                            WeeklyWindow(3, "09:00:00", "10:00:00", 1),
                        ],
                    )
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert _value(rows["build"], "allocation_state") == "complete"
    assert _iso(_value(rows["build"], "starts_at")) == "2026-05-13T09:00:00+00:00"
    assert _iso(_value(rows["build"], "ends_at")) == "2026-05-14T10:00:00+00:00"
    assert _allocated_effort_in_window(
        data,
        resource_id="res_alex",
        starts_at=_at(13),
        ends_at=_at(13, 13),
    ) == pytest.approx(4.0)
    assert _allocated_effort_in_window(
        data,
        resource_id="res_alex",
        starts_at=_at(14),
        ends_at=_at(14, 10),
    ) == pytest.approx(1.0)


def test_min_allocation_respects_max_daily_cap_and_resource_availability():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build")],
                role_requirements=[
                    _requirement(
                        "build",
                        5,
                        min_allocation_hours_per_day=2,
                        max_allocation_hours_per_day=3,
                    )
                ],
                resources=[
                    _resource("res_alex", available_from_at=_at(13, 11)),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert _value(rows["build"], "allocation_state") == "complete"
    assert _iso(_value(rows["build"], "starts_at")) == "2026-05-13T11:00:00+00:00"
    assert _iso(_value(rows["build"], "ends_at")) == "2026-05-14T11:00:00+00:00"
    assert _allocated_effort_in_window(
        data,
        resource_id="res_alex",
        starts_at=_at(13, 11),
        ends_at=_at(13, 14),
    ) == pytest.approx(3.0)
    assert _allocated_effort_in_window(
        data,
        resource_id="res_alex",
        starts_at=_at(13, 14),
        ends_at=_at(13, 17),
    ) == pytest.approx(0.0)
    assert _allocated_effort_in_window(
        data,
        resource_id="res_alex",
        starts_at=_at(14),
        ends_at=_at(14, 11),
    ) == pytest.approx(2.0)


def test_split_allowed_can_combine_resources_but_contiguous_requires_one_sequence():
    resources = [
        _resource("res_alex", calendar_id="cal_alex"),
        _resource("res_blair", calendar_id="cal_blair"),
    ]
    calendars = [
        _calendar(
            "cal_alex",
            windows=[
                WeeklyWindow(weekday, "09:00:00", "13:00:00", 4)
                for weekday in range(5)
            ],
        ),
        _calendar(
            "cal_blair",
            windows=[
                WeeklyWindow(weekday, "09:00:00", "13:00:00", 4)
                for weekday in range(5)
            ],
        ),
    ]
    split_allowed = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build")],
                role_requirements=[
                    _requirement(
                        "build",
                        8,
                        required_resource_count=2,
                        allocation_policy="split_allowed",
                    )
                ],
                resources=resources,
                calendars=calendars,
            )
        )
    )

    split_rows = _rows_by_id(split_allowed)
    assert _value(split_rows["build"], "allocation_state") == "complete"
    assert _iso(_value(split_rows["build"], "ends_at")) == "2026-05-13T13:00:00+00:00"
    assert _allocation_effort_by_resource(split_allowed) == {
        "res_alex": 4.0,
        "res_blair": 4.0,
    }

    contiguous = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build")],
                role_requirements=[
                    _requirement(
                        "build",
                        8,
                        required_resource_count=2,
                        allocation_policy="contiguous",
                    )
                ],
                resources=resources,
                calendars=calendars,
            )
        )
    )

    contiguous_rows = _rows_by_id(contiguous)
    contiguous_unallocated = _unallocated_by_process(contiguous)
    assert _value(contiguous_rows["build"], "allocation_state") == "unallocated"
    assert _value(contiguous_rows["build"], "starts_at") is None
    assert _value(contiguous_rows["build"], "ends_at") is None
    assert (
        _value(contiguous_unallocated["build"], "reason")
        == "contiguous_window_unavailable"
    )
    assert contiguous["allocation_slices"] == []


def test_contiguous_policy_does_not_reserve_future_buckets_ahead_of_ready_split_work():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("contiguous"), _process("split")],
                role_requirements=[
                    _requirement(
                        "contiguous",
                        8,
                        requirement_id="req_contiguous",
                        allocation_policy="contiguous",
                    ),
                    _requirement("split", 1, requirement_id="req_split"),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert _iso(_value(rows["split"], "ends_at")) == "2026-05-13T10:00:00+00:00"
    assert _iso(_value(rows["contiguous"], "ends_at")) == "2026-05-14T17:00:00+00:00"


def test_multi_role_resource_uses_one_capacity_ledger_across_roles():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("release")],
                role_requirements=[
                    _requirement(
                        "release",
                        4,
                        requirement_id="req_release_dev",
                        role_id="role_dev",
                    ),
                    _requirement(
                        "release",
                        4,
                        requirement_id="req_release_qa",
                        role_id="role_qa",
                    ),
                ],
                roles=[
                    _role("role_dev", "Developer"),
                    _role("role_qa", "QA"),
                ],
                resources=[
                    _resource("res_alex", role_ids=["role_dev", "role_qa"]),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert _value(rows["release"], "allocation_state") == "complete"
    assert _iso(_value(rows["release"], "starts_at")) == "2026-05-13T09:00:00+00:00"
    assert _iso(_value(rows["release"], "ends_at")) == "2026-05-13T17:00:00+00:00"
    assert {
        str(_value(allocation, "role_id"))
        for allocation in data["allocation_slices"]
    } == {"role_dev", "role_qa"}
    assert _allocation_effort_by_resource(data) == {"res_alex": 8.0}
    _assert_resource_utilization_never_exceeds_capacity(data)


def test_multi_role_resource_breadth_first_slices_share_one_bucket_ledger():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("release")],
                role_requirements=[
                    _requirement(
                        "release",
                        2,
                        requirement_id="req_release_dev",
                        role_id="role_dev",
                    ),
                    _requirement(
                        "release",
                        2,
                        requirement_id="req_release_qa",
                        role_id="role_qa",
                    ),
                ],
                roles=[
                    _role("role_dev", "Developer"),
                    _role("role_qa", "QA"),
                ],
                resources=[
                    _resource("res_alex", role_ids=["role_dev", "role_qa"]),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert _value(rows["release"], "allocation_state") == "complete"
    assert _iso(_value(rows["release"], "starts_at")) == "2026-05-13T09:00:00+00:00"
    assert _iso(_value(rows["release"], "ends_at")) == "2026-05-13T13:00:00+00:00"
    assert _allocated_effort_for_requirement_in_window(
        data,
        requirement_id="req_release_dev",
        starts_at=_at(13),
        ends_at=_at(13, 10),
    ) == pytest.approx(0.5)
    assert _allocated_effort_for_requirement_in_window(
        data,
        requirement_id="req_release_qa",
        starts_at=_at(13),
        ends_at=_at(13, 10),
    ) == pytest.approx(0.5)
    assert _allocated_effort_for_requirement_in_window(
        data,
        requirement_id="req_release_dev",
        starts_at=_at(13, 10),
        ends_at=_at(13, 11),
    ) == pytest.approx(0.5)
    assert _allocation_effort_by_resource(data) == {"res_alex": 4.0}
    _assert_resource_utilization_never_exceeds_capacity(data)


def test_successor_ready_at_is_null_when_predecessor_is_unallocated():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process("blocked_by_missing_role"),
                    _process(
                        "successor",
                        dependencies=["blocked_by_missing_role"],
                    ),
                ],
                role_requirements=[
                    _requirement(
                        "blocked_by_missing_role",
                        2,
                        role_id="role_missing",
                    ),
                    _requirement("successor", 1),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    unallocated = _unallocated_by_process(data)
    assert _value(rows["blocked_by_missing_role"], "ends_at") is None
    assert _value(rows["successor"], "ready_at") is None
    assert _value(rows["successor"], "starts_at") is None
    assert _value(rows["successor"], "ends_at") is None
    assert _value(unallocated["successor"], "reason") == "predecessor_unallocated"
    assert _value(unallocated["successor"], "first_feasible_starts_at") is None


def test_partial_row_keeps_ready_at_and_first_allocation_start_with_null_finish():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build")],
                role_requirements=[_requirement("build", 6)],
                horizon_ends_at=_at(13, 13),
            )
        )
    )

    rows = _rows_by_id(data)
    unallocated = _unallocated_by_process(data)

    assert _value(rows["build"], "allocation_state") == "partial"
    assert _iso(_value(rows["build"], "ready_at")) == "2026-05-13T09:00:00+00:00"
    assert _iso(_value(rows["build"], "starts_at")) == "2026-05-13T09:00:00+00:00"
    assert _value(rows["build"], "ends_at") is None
    assert _value(unallocated["build"], "reason") == "horizon_exhausted"
    assert sum(
        float(_value(allocation, "effort_hours"))
        for allocation in data["allocation_slices"]
    ) == 4.0


def test_fully_unallocated_row_keeps_ready_at_when_dependencies_are_feasible():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("build")],
                role_requirements=[
                    _requirement("build", 2, role_id="role_missing"),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    unallocated = _unallocated_by_process(data)

    assert _value(rows["build"], "allocation_state") == "unallocated"
    assert _iso(_value(rows["build"], "ready_at")) == "2026-05-13T09:00:00+00:00"
    assert _value(rows["build"], "starts_at") is None
    assert _value(rows["build"], "ends_at") is None
    assert _value(unallocated["build"], "reason") == "missing_role"
    assert _value(unallocated["build"], "message")
    assert data["allocation_slices"] == []


def test_convergence_fingerprint_ignores_slice_id_iteration_and_cost_amount():
    first_iteration = [
        _allocation_slice(
            slice_id="slice_task_first",
            iteration=1,
            cost_amount=None,
        )
    ]
    later_iteration = [
        _allocation_slice(
            slice_id="slice_task_later",
            iteration=2,
            cost_amount="100.00",
        )
    ]

    assert _allocation_slice_fingerprint(first_iteration) == (
        _allocation_slice_fingerprint(later_iteration)
    )

    comparison = _compare_resource_schedule_iterations(
        _iteration_state(allocation_slices=first_iteration),
        _iteration_state(allocation_slices=later_iteration),
    )

    assert _value(comparison, "converged") is True
    assert _value(comparison, "changed_process_ids") == []
    assert _value(comparison, "reason_changes") == []
    assert _value(comparison, "allocation_fingerprint_changed") is False


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("process_id", "other_task"),
        ("requirement_id", "req_other"),
        ("role_id", "role_qa"),
        ("resource_id", "res_blair"),
        ("starts_at", _at(13, 10)),
        ("ends_at", _at(13, 12)),
        ("effort_hours", 3.0),
        ("capacity_hours", 1.0),
        ("cost_currency", "EUR"),
    ],
)
def test_convergence_fingerprint_includes_stable_allocation_fields(
    field: str,
    changed_value: object,
):
    assert _allocation_slice_fingerprint([_allocation_slice()]) != (
        _allocation_slice_fingerprint([_allocation_slice(**{field: changed_value})])
    )


def test_convergence_detects_null_to_non_null_finish_transition():
    comparison = _compare_resource_schedule_iterations(
        _iteration_state(rows=[_convergence_row(ends_at=None)]),
        _iteration_state(rows=[_convergence_row(ends_at=_at(13, 11))]),
    )

    assert _value(comparison, "converged") is False
    assert _changed_process_ids(comparison) == {"task"}


def test_convergence_detects_non_null_to_null_finish_transition():
    comparison = _compare_resource_schedule_iterations(
        _iteration_state(rows=[_convergence_row(ends_at=_at(13, 11))]),
        _iteration_state(rows=[_convergence_row(ends_at=None)]),
    )

    assert _value(comparison, "converged") is False
    assert _changed_process_ids(comparison) == {"task"}


def test_convergence_detects_null_to_non_null_ready_at_transition():
    comparison = _compare_resource_schedule_iterations(
        _iteration_state(
            rows=[
                _convergence_row(
                    ready_at=None,
                    starts_at=None,
                    ends_at=None,
                    allocation_state="unallocated",
                )
            ],
            unallocated=[
                _unallocated_requirement("no_calendar_capacity"),
            ],
        ),
        _iteration_state(
            rows=[
                _convergence_row(
                    ready_at=_at(13),
                    starts_at=None,
                    ends_at=None,
                    allocation_state="unallocated",
                )
            ],
            unallocated=[
                _unallocated_requirement("no_calendar_capacity"),
            ],
        ),
    )

    assert _value(comparison, "converged") is False
    assert _changed_process_ids(comparison) == {"task"}


def test_convergence_detects_non_null_to_null_ready_at_transition():
    comparison = _compare_resource_schedule_iterations(
        _iteration_state(
            rows=[
                _convergence_row(
                    ready_at=_at(13),
                    starts_at=None,
                    ends_at=None,
                    allocation_state="unallocated",
                )
            ],
            unallocated=[
                _unallocated_requirement("no_calendar_capacity"),
            ],
        ),
        _iteration_state(
            rows=[
                _convergence_row(
                    ready_at=None,
                    starts_at=None,
                    ends_at=None,
                    allocation_state="unallocated",
                )
            ],
            unallocated=[
                _unallocated_requirement("no_calendar_capacity"),
            ],
        ),
    )

    assert _value(comparison, "converged") is False
    assert _changed_process_ids(comparison) == {"task"}


def test_convergence_compares_null_ready_at_with_state_and_sorted_reasons():
    row = _convergence_row(
        ready_at=None,
        starts_at=None,
        ends_at=None,
        allocation_state="unallocated",
    )
    same_reasons_reordered = _compare_resource_schedule_iterations(
        _iteration_state(
            rows=[row],
            unallocated=[
                _unallocated_requirement(
                    "horizon_exhausted",
                    requirement_id="req_late",
                ),
                _unallocated_requirement(
                    "no_calendar_capacity",
                    requirement_id="req_capacity",
                ),
            ],
        ),
        _iteration_state(
            rows=[row],
            unallocated=[
                _unallocated_requirement(
                    "no_calendar_capacity",
                    requirement_id="req_capacity",
                ),
                _unallocated_requirement(
                    "horizon_exhausted",
                    requirement_id="req_late",
                ),
            ],
        ),
    )

    changed_state = _compare_resource_schedule_iterations(
        _iteration_state(rows=[row]),
        _iteration_state(
            rows=[
                _convergence_row(
                    ready_at=None,
                    starts_at=None,
                    ends_at=None,
                    allocation_state="partial",
                )
            ]
        ),
    )

    changed_reasons = _compare_resource_schedule_iterations(
        _iteration_state(
            rows=[row],
            unallocated=[
                _unallocated_requirement("no_calendar_capacity"),
            ],
        ),
        _iteration_state(
            rows=[row],
            unallocated=[
                _unallocated_requirement("horizon_exhausted"),
            ],
        ),
    )

    assert _value(same_reasons_reordered, "converged") is True
    assert _changed_process_ids(same_reasons_reordered) == set()
    assert _value(same_reasons_reordered, "reason_changes") == []
    assert _value(changed_state, "converged") is False
    assert _changed_process_ids(changed_state) == {"task"}
    assert _value(changed_reasons, "converged") is False
    assert _changed_process_ids(changed_reasons) == {"task"}


def test_convergence_reports_reason_changes_in_sorted_order():
    row = _convergence_row(
        ready_at=None,
        starts_at=None,
        ends_at=None,
        allocation_state="unallocated",
    )
    comparison = _compare_resource_schedule_iterations(
        _iteration_state(
            rows=[row],
            unallocated=[
                _unallocated_requirement("blocked", requirement_id="req_b"),
                _unallocated_requirement(
                    "no_calendar_capacity",
                    requirement_id="req_a",
                ),
            ],
        ),
        _iteration_state(
            rows=[row],
            unallocated=[
                _unallocated_requirement(
                    "horizon_exhausted",
                    requirement_id="req_a",
                ),
                _unallocated_requirement(
                    "predecessor_unallocated",
                    requirement_id="req_b",
                ),
            ],
        ),
    )

    assert _value(comparison, "converged") is False
    assert _reason_change_tuple_list(comparison) == [
        ("task", "req_a", "no_calendar_capacity", "horizon_exhausted"),
        ("task", "req_b", "blocked", "predecessor_unallocated"),
    ]


def test_convergence_detects_reason_only_changes_for_unallocated_requirements():
    row = _convergence_row(
        ready_at=None,
        starts_at=None,
        ends_at=None,
        allocation_state="unallocated",
    )
    comparison = _compare_resource_schedule_iterations(
        _iteration_state(
            rows=[row],
            unallocated=[
                _unallocated_requirement("predecessor_unallocated"),
            ],
        ),
        _iteration_state(
            rows=[row],
            unallocated=[
                _unallocated_requirement("blocked"),
            ],
        ),
    )

    assert _value(comparison, "converged") is False
    assert _changed_process_ids(comparison) == {"task"}
    assert _reason_change_tuples(comparison) == {
        ("task", "req_task", "predecessor_unallocated", "blocked")
    }


@pytest.mark.parametrize(
    ("before_row", "after_row"),
    [
        (
            _convergence_row(
                ready_at=_at(13),
                starts_at=_at(13, 10),
                ends_at=_at(13, 12),
            ),
            _convergence_row(
                ready_at=_at(13, 9),
                starts_at=_at(13, 10),
                ends_at=_at(13, 12),
            ),
        ),
        (
            _convergence_row(
                ready_at=_at(13),
                starts_at=_at(13, 10),
                ends_at=_at(13, 12),
            ),
            _convergence_row(
                ready_at=_at(13),
                starts_at=_at(13, 11),
                ends_at=_at(13, 12),
            ),
        ),
    ],
    ids=["readiness_change", "start_change"],
)
def test_convergence_detects_readiness_and_start_changes(
    before_row: dict[str, object],
    after_row: dict[str, object],
):
    comparison = _compare_resource_schedule_iterations(
        _iteration_state(rows=[before_row]),
        _iteration_state(rows=[after_row]),
    )

    assert _value(comparison, "converged") is False
    assert _changed_process_ids(comparison) == {"task"}


def test_convergence_detects_allocation_state_changes():
    comparison = _compare_resource_schedule_iterations(
        _iteration_state(
            rows=[
                _convergence_row(
                    ready_at=_at(13),
                    starts_at=_at(13),
                    ends_at=None,
                    allocation_state="partial",
                )
            ]
        ),
        _iteration_state(
            rows=[
                _convergence_row(
                    ready_at=_at(13),
                    starts_at=_at(13),
                    ends_at=None,
                    allocation_state="unallocated",
                )
            ]
        ),
    )

    assert _value(comparison, "converged") is False
    assert _changed_process_ids(comparison) == {"task"}


def test_convergence_detects_slice_fingerprint_changes_in_included_fields():
    comparison = _compare_resource_schedule_iterations(
        _iteration_state(allocation_slices=[_allocation_slice()]),
        _iteration_state(
            allocation_slices=[
                _allocation_slice(resource_id="res_blair"),
            ],
        ),
    )

    assert _value(comparison, "converged") is False
    assert _value(comparison, "allocation_fingerprint_changed") is True


def test_max_iteration_cap_returns_final_engine_output():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process("design"),
                    _process("build", dependencies=["design"]),
                    _process("docs"),
                ],
                role_requirements=[
                    _requirement("design", 10),
                    _requirement("build", 1),
                    _requirement("docs", 1),
                ],
                options={"max_iterations": 1},
            )
        )
    )

    rows = _rows_by_id(data)
    convergence = _optional_value(data, "convergence", {})
    iteration_not_converged_requirement_ids = {
        str(_value(unallocated, "requirement_id"))
        for unallocated in data["unallocated_requirements"]
        if _value(unallocated, "reason") == "iteration_not_converged"
    }

    assert data["processes"]
    assert data["allocation_slices"]
    assert data["converged"] is False
    assert data["iteration_count"] == 1
    assert _value(convergence, "converged") is False
    assert _value(convergence, "iteration_count") == 1
    assert _value(convergence, "max_iterations") == 1
    assert "build" in _value(convergence, "changed_process_ids")
    assert _value(rows["design"], "allocation_state") == "complete"
    assert _value(rows["docs"], "allocation_state") == "complete"
    assert iteration_not_converged_requirement_ids == {"req_build"}


def test_resource_critical_path_uses_hour_bucket_finish_not_business_day_duration():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process(
                        "long_business_duration_short_effort",
                        duration_business_days=30,
                    ),
                    _process("zero_day_hour_heavy", duration_business_days=0),
                ],
                role_requirements=[
                    _requirement(
                        "long_business_duration_short_effort",
                        1,
                        requirement_id="req_short",
                        role_id="role_short",
                    ),
                    _requirement(
                        "zero_day_hour_heavy",
                        6,
                        requirement_id="req_heavy",
                        role_id="role_heavy",
                    ),
                ],
                roles=[
                    _role("role_short", "Short Work"),
                    _role("role_heavy", "Heavy Work"),
                ],
                resources=[
                    _resource("res_short", role_ids=["role_short"]),
                    _resource("res_heavy", role_ids=["role_heavy"]),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert _as_datetime(
        _value(rows["long_business_duration_short_effort"], "dependency_only_ends_at")
    ) > _as_datetime(_value(rows["zero_day_hour_heavy"], "dependency_only_ends_at"))
    assert _iso(
        _value(rows["long_business_duration_short_effort"], "ends_at")
    ) == "2026-05-13T10:00:00+00:00"
    assert _iso(_value(rows["zero_day_hour_heavy"], "ends_at")) == (
        "2026-05-13T15:00:00+00:00"
    )
    assert data["critical_path_process_ids"] == ["zero_day_hour_heavy"]


def test_resource_critical_path_respects_timezone_aware_resource_holidays():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("holiday_shifted")],
                role_requirements=[
                    _requirement(
                        "holiday_shifted",
                        1,
                        requirement_id="req_holiday_shifted",
                    )
                ],
                resources=[
                    _resource(
                        "res_new_york",
                        calendar_id="cal_new_york",
                        holidays=[
                            {
                                "holiday_id": "nyc_morning_closure",
                                "starts_at": dt.datetime(
                                    2026,
                                    5,
                                    13,
                                    9,
                                    tzinfo=dt.timezone(dt.timedelta(hours=-4)),
                                ),
                                "ends_at": dt.datetime(
                                    2026,
                                    5,
                                    13,
                                    13,
                                    tzinfo=dt.timezone(dt.timedelta(hours=-4)),
                                ),
                                "reason": "Local holiday",
                            }
                        ],
                    )
                ],
                calendars=[
                    _calendar(
                        "cal_new_york",
                        timezone="America/New_York",
                        windows=[
                            WeeklyWindow(weekday, "09:00:00", "13:00:00", 4)
                            for weekday in range(5)
                        ],
                    )
                ],
                horizon_ends_at=_at(15, 17),
            )
        )
    )

    rows = _rows_by_id(data)
    assert _value(rows["holiday_shifted"], "allocation_state") == "complete"
    assert _iso(_value(rows["holiday_shifted"], "ready_at")) == (
        "2026-05-13T09:00:00+00:00"
    )
    assert _iso(_value(rows["holiday_shifted"], "starts_at")) == (
        "2026-05-14T13:00:00+00:00"
    )
    assert _iso(_value(rows["holiday_shifted"], "ends_at")) == (
        "2026-05-14T14:00:00+00:00"
    )
    assert data["critical_path_process_ids"] == ["holiday_shifted"]


def test_resource_critical_path_is_process_level_after_convergence():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process("a_design"),
                    _process("b_integrate", dependencies=["a_design"]),
                    _process("c_independent"),
                ],
                role_requirements=[
                    _requirement("a_design", 10),
                    _requirement("b_integrate", 1),
                    _requirement("c_independent", 8),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert data["converged"] is True
    assert _iso(_value(rows["a_design"], "ends_at")) == "2026-05-15T11:00:00+00:00"
    assert _iso(_value(rows["c_independent"], "ends_at")) == "2026-05-14T17:00:00+00:00"
    assert _iso(_value(rows["b_integrate"], "ready_at")) == "2026-05-15T11:00:00+00:00"
    assert _iso(_value(rows["b_integrate"], "ends_at")) == "2026-05-15T12:00:00+00:00"
    assert data["critical_path_process_ids"] == ["a_design", "b_integrate"]


def test_resource_critical_path_can_differ_from_dependency_only_cpm():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process("prep_resource"),
                    _process("chain_a"),
                    _process("chain_b", dependencies=["chain_a"]),
                    _process("solo_heavy"),
                ],
                role_requirements=[
                    _requirement("prep_resource", 1),
                    _requirement("solo_heavy", 23),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert data["converged"] is True
    assert data["unallocated_requirements"] == []
    assert (
        _as_datetime(_value(rows["chain_b"], "dependency_only_ends_at"))
        > _as_datetime(_value(rows["solo_heavy"], "dependency_only_ends_at"))
    )
    assert _iso(_value(rows["solo_heavy"], "ends_at")) == "2026-05-15T17:00:00+00:00"
    assert data["critical_path_process_ids"] == ["solo_heavy"]
    assert _iso(_value(rows["prep_resource"], "ends_at")) == "2026-05-13T11:00:00+00:00"
    assert _iso(_value(rows["solo_heavy"], "starts_at")) == _iso(
        _value(rows["solo_heavy"], "ready_at")
    )


def test_resource_critical_path_terminal_tie_uses_topological_index_before_id():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process("zz_topo_first"),
                    _process("aa_process_id_first"),
                ],
                role_requirements=[
                    _requirement(
                        "zz_topo_first",
                        1,
                        requirement_id="req_zz_topo_first",
                    ),
                    _requirement(
                        "aa_process_id_first",
                        1,
                        requirement_id="req_aa_process_id_first",
                    ),
                ],
                resources=[
                    _resource("res_alex"),
                    _resource("res_blair"),
                ],
            )
        )
    )

    rows = _rows_by_id(data)
    assert "aa_process_id_first" < "zz_topo_first"
    assert _iso(_value(rows["zz_topo_first"], "ends_at")) == _iso(
        _value(rows["aa_process_id_first"], "ends_at")
    )
    assert data["critical_path_process_ids"] == ["zz_topo_first"]


def test_resource_critical_path_terminal_tie_uses_tolerance_window():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process("topo_first"),
                    _process("topo_second"),
                ],
                role_requirements=[
                    _requirement(
                        "topo_first",
                        8,
                        requirement_id="req_topo_first",
                        role_id="role_first",
                    ),
                    _requirement(
                        "topo_second",
                        8.5,
                        requirement_id="req_topo_second",
                        role_id="role_second",
                    ),
                ],
                roles=[
                    _role("role_first", "First"),
                    _role("role_second", "Second"),
                ],
                resources=[
                    _resource(
                        "res_first",
                        role_ids=["role_first"],
                        calendar_id="cal_long",
                    ),
                    _resource(
                        "res_second",
                        role_ids=["role_second"],
                        calendar_id="cal_long",
                    ),
                ],
                calendars=[
                    _calendar(
                        "cal_long",
                        windows=[
                            WeeklyWindow(weekday, "09:00:00", "18:00:00", 9)
                            for weekday in range(5)
                        ],
                    ),
                ],
                options={"convergence_tolerance_hours": 1},
            )
        )
    )

    rows = _rows_by_id(data)
    assert _iso(_value(rows["topo_first"], "ends_at")) == "2026-05-13T17:00:00+00:00"
    assert _iso(_value(rows["topo_second"], "ends_at")) == "2026-05-13T18:00:00+00:00"
    assert data["critical_path_process_ids"] == ["topo_first"]


def test_resource_critical_path_gating_predecessor_ties_are_canonical():
    slack_tie_breaker = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process("aa_loose_topo_first", duration_business_days=0),
                    _process("zz_tight_topo_later", duration_business_days=1),
                    _process(
                        "join_by_slack",
                        dependencies=[
                            "aa_loose_topo_first",
                            "zz_tight_topo_later",
                        ],
                    ),
                ],
                role_requirements=[
                    _requirement(
                        "aa_loose_topo_first",
                        1,
                        requirement_id="req_aa_loose_topo_first",
                    ),
                    _requirement(
                        "zz_tight_topo_later",
                        1,
                        requirement_id="req_zz_tight_topo_later",
                    ),
                    _requirement(
                        "join_by_slack",
                        1,
                        requirement_id="req_join_by_slack",
                    ),
                ],
                resources=[
                    _resource("res_alex"),
                    _resource("res_blair"),
                ],
            )
        )
    )
    topo_tie_breaker = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process("zz_topo_first", duration_business_days=0),
                    _process("aa_process_id_first", duration_business_days=0),
                    _process(
                        "join_by_topology",
                        dependencies=[
                            "zz_topo_first",
                            "aa_process_id_first",
                        ],
                    ),
                ],
                role_requirements=[
                    _requirement(
                        "zz_topo_first",
                        1,
                        requirement_id="req_zz_topo_first",
                    ),
                    _requirement(
                        "aa_process_id_first",
                        1,
                        requirement_id="req_aa_process_id_first",
                    ),
                    _requirement(
                        "join_by_topology",
                        1,
                        requirement_id="req_join_by_topology",
                    ),
                ],
                resources=[
                    _resource("res_alex"),
                    _resource("res_blair"),
                ],
            )
        )
    )

    slack_rows = _rows_by_id(slack_tie_breaker)
    topo_rows = _rows_by_id(topo_tie_breaker)
    assert _iso(_value(slack_rows["aa_loose_topo_first"], "ends_at")) == _iso(
        _value(slack_rows["zz_tight_topo_later"], "ends_at")
    )
    assert "aa_loose_topo_first" < "zz_tight_topo_later"
    assert slack_tie_breaker["critical_path_process_ids"] == [
        "aa_loose_topo_first",
        "join_by_slack",
    ]
    assert _iso(_value(topo_rows["zz_topo_first"], "ends_at")) == _iso(
        _value(topo_rows["aa_process_id_first"], "ends_at")
    )
    assert "aa_process_id_first" < "zz_topo_first"
    assert topo_tie_breaker["critical_path_process_ids"] == [
        "zz_topo_first",
        "join_by_topology",
    ]


def test_root_resource_delayed_process_has_no_virtual_allocation_graph_nodes():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("solo")],
                role_requirements=[_requirement("solo", 1)],
                resources=[
                    _resource("res_alex", available_from_at=_at(14)),
                ],
                horizon_ends_at=_at(15, 17),
            )
        )
    )

    rows = _rows_by_id(data)
    process_ids = set(rows)
    slice_ids = {
        str(_value(allocation, "slice_id"))
        for allocation in data["allocation_slices"]
    }
    assert data["critical_path_process_ids"] == ["solo"]
    assert _value(rows["solo"], "allocation_state") == "complete"
    assert _as_datetime(_value(rows["solo"], "starts_at")) > _as_datetime(
        _value(rows["solo"], "ready_at")
    )
    assert process_ids == {"solo"}
    assert slice_ids.isdisjoint(process_ids)
    assert slice_ids.isdisjoint(set(data["critical_path_process_ids"]))


def test_resource_schedule_returns_collapsed_process_rows_only():
    input_process_ids = {"alpha", "beta", "ship"}
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[
                    _process("alpha"),
                    _process("beta"),
                    _process("ship", dependencies=["alpha", "beta"]),
                ],
                role_requirements=[
                    _requirement("alpha", 1, requirement_id="req_alpha"),
                    _requirement("beta", 1, requirement_id="req_beta"),
                    _requirement("ship", 1, requirement_id="req_ship"),
                ],
            )
        )
    )

    row_ids = {str(_value(row, "process_id")) for row in data["processes"]}
    allocation_process_ids = {
        str(_value(allocation, "process_id"))
        for allocation in data["allocation_slices"]
    }
    assert row_ids == input_process_ids
    assert allocation_process_ids <= input_process_ids
    assert set(data["critical_path_process_ids"]) <= input_process_ids
    assert all(
        str(_value(allocation, "slice_id")) not in row_ids
        for allocation in data["allocation_slices"]
    )


def test_scheduler_handles_resources_in_different_timezones_and_work_schedules():
    data = _data(
        _compute_resource_schedule(
            _base_input(
                processes=[_process("handoff")],
                role_requirements=[
                    _requirement("handoff", 8, required_resource_count=2),
                ],
                resources=[
                    _resource("res_new_york", calendar_id="cal_new_york"),
                    _resource("res_tokyo", calendar_id="cal_tokyo"),
                ],
                calendars=[
                    _calendar(
                        "cal_new_york",
                        timezone="America/New_York",
                        windows=[
                            WeeklyWindow(2, "09:00:00", "13:00:00", 4),
                        ],
                    ),
                    _calendar(
                        "cal_tokyo",
                        timezone="Asia/Tokyo",
                        windows=[
                            WeeklyWindow(3, "09:00:00", "13:00:00", 4),
                        ],
                    ),
                ],
                horizon_ends_at=dt.datetime(2026, 5, 14, 5, tzinfo=UTC),
            )
        )
    )

    rows = _rows_by_id(data)
    assert data["converged"] is True
    assert _value(rows["handoff"], "allocation_state") == "complete"
    assert _iso(_value(rows["handoff"], "starts_at")) == "2026-05-13T13:00:00+00:00"
    assert _iso(_value(rows["handoff"], "ends_at")) == "2026-05-14T04:00:00+00:00"
    assert _allocation_effort_by_resource(data) == {
        "res_new_york": 4.0,
        "res_tokyo": 4.0,
    }
    assert _allocated_effort_in_window(
        data,
        resource_id="res_new_york",
        starts_at=dt.datetime(2026, 5, 13, 13, tzinfo=UTC),
        ends_at=dt.datetime(2026, 5, 13, 17, tzinfo=UTC),
    ) == pytest.approx(4.0)
    assert _allocated_effort_in_window(
        data,
        resource_id="res_tokyo",
        starts_at=dt.datetime(2026, 5, 14, 0, tzinfo=UTC),
        ends_at=dt.datetime(2026, 5, 14, 4, tzinfo=UTC),
    ) == pytest.approx(4.0)
    _assert_resource_utilization_never_exceeds_capacity(data)

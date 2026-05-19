"""Pure resource-constrained scheduling engine."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from projdash.engine.calendar import (
    add_business_days,
    count_business_days,
    expand_resource_calendar,
    next_business_day,
    require_aware,
    subtract_business_days,
)

UTC = dt.UTC
EPSILON = 0.0001


@dataclass(frozen=True, slots=True)
class _ProcessCpm:
    process_id: str
    name: str
    description: str
    dependencies: tuple[str, ...]
    topo_index: int
    earliest_start_at: dt.datetime
    earliest_finish_at: dt.datetime
    latest_start_at: dt.datetime
    latest_finish_at: dt.datetime
    slack_business_days: int


@dataclass(slots=True)
class _LedgerBucket:
    resource_id: str
    calendar_id: str
    starts_at: dt.datetime
    ends_at: dt.datetime
    capacity_hours: float
    remaining_hours: float
    role_ids: tuple[str, ...]
    local_date: str
    local_week: str
    window_id: str | None


@dataclass(slots=True)
class _RequirementState:
    requirement: Mapping[str, object]
    allocated_hours: float = 0
    starts_at: dt.datetime | None = None
    ends_at: dt.datetime | None = None
    reason: str | None = None
    eligible_resource_ids: tuple[str, ...] = ()
    first_feasible_starts_at: dt.datetime | None = None
    ready_at: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class _RequirementCandidate:
    requirement: Mapping[str, object]
    state: _RequirementState
    bucket: _LedgerBucket
    resource: Mapping[str, object]
    ready_at: dt.datetime
    headroom_hours: float
    role_availability_hours: float
    sort_key: tuple[object, ...]


def compute_resource_schedule(input_data: Mapping[str, object]) -> dict[str, object]:
    """Compute a deterministic resource-constrained schedule.

    Args:
        input_data: Engine read model containing processes, dependencies,
            requirements, roles, resources, calendars, blockers, and options.

    Returns:
        Resource schedule data with process rows, optional allocation slices,
        critical path, and convergence metadata.
    """
    options = dict(_mapping(input_data.get("options", {}), "options"))
    project_id = str(input_data["project_id"])
    project_start_at = _as_utc(input_data["project_start_at"])
    as_of = _as_utc(input_data["as_of"])
    now = _as_utc(input_data["now"])
    horizon_starts_at = _as_utc(options["horizon_starts_at"])
    horizon_ends_at = _as_utc(options["horizon_ends_at"])
    capacity_search_attempt = int(options.get("_capacity_search_attempt", 0))
    max_capacity_search_days = int(options.get("capacity_search_max_days", 36500))
    planning_granularity = str(options.get("planning_granularity", "hour"))
    max_iterations = int(options.get("max_iterations", 20))
    tolerance_hours = float(options.get("convergence_tolerance_hours", 0))
    blocked_policy = "include_normally"
    include_slices = bool(options.get("include_allocation_slices", False))

    processes = [_mapping(item, "process") for item in _sequence(input_data["processes"])]
    requirements = [
        _mapping(item, "role requirement")
        for item in _sequence(input_data.get("role_requirements", ()))
    ]
    _validate_integral_effort_hours(requirements)
    roles = [_mapping(item, "role") for item in _sequence(input_data.get("roles", ()))]
    resources = [
        _mapping(item, "resource") for item in _sequence(input_data.get("resources", ()))
    ]
    calendars = [
        _mapping(item, "calendar") for item in _sequence(input_data.get("calendars", ()))
    ]
    blockers = [
        _mapping(item, "blocker") for item in _sequence(input_data.get("blockers", ()))
    ]

    dependencies = _collect_dependencies(input_data, processes)
    topo_order = _topological_order(processes, dependencies)
    cpm_by_id = _compute_cpm(processes, dependencies, project_start_at, topo_order)
    requirements_by_process = _requirements_by_process(requirements)
    active_role_ids = {
        str(role["role_id"]) for role in roles if bool(role.get("active", True))
    }
    _validate_recurring_capacity_sources(
        requirements=requirements,
        processes_by_id={str(process["process_id"]): process for process in processes},
        roles=roles,
        resources=resources,
        calendars=calendars,
    )
    active_blocked_process_ids = _blocked_process_ids(blockers, as_of)
    resource_by_id = {str(resource["resource_id"]): resource for resource in resources}
    expanded_buckets = _expand_capacity_buckets(
        resources=resources,
        calendars=calendars,
        horizon_starts_at=horizon_starts_at,
        horizon_ends_at=horizon_ends_at,
        planning_granularity=planning_granularity,
    )

    previous_state = _initial_iteration_state(
        processes=processes,
        cpm_by_id=cpm_by_id,
        requirements_by_process=requirements_by_process,
    )
    final_iteration: dict[str, object] | None = None
    final_comparison: dict[str, object] | None = None

    for iteration in range(1, max_iterations + 1):
        current = _run_allocation_iteration(
            project_id=project_id,
            project_start_at=project_start_at,
            processes=processes,
            dependencies=dependencies,
            topo_order=topo_order,
            cpm_by_id=cpm_by_id,
            requirements_by_process=requirements_by_process,
            active_role_ids=active_role_ids,
            resources=resources,
            resource_by_id=resource_by_id,
            expanded_buckets=expanded_buckets,
            blocked_process_ids=active_blocked_process_ids,
            blocked_policy=blocked_policy,
            horizon_ends_at=horizon_ends_at,
            iteration=iteration,
        )
        comparison = compare_resource_schedule_iterations(
            previous_state,
            current,
            tolerance_hours=tolerance_hours,
        )
        final_iteration = current
        final_comparison = comparison
        if bool(comparison["converged"]):
            break
        previous_state = current

    assert final_iteration is not None
    assert final_comparison is not None

    converged = bool(final_comparison["converged"])
    iteration_count = int(final_iteration["iteration_count"])
    unallocated = list(final_iteration["unallocated_requirements"])
    if unallocated:
        _raise_for_permanent_capacity_failures(unallocated)
        next_horizon_ends_at = _next_capacity_search_end(
            horizon_starts_at,
            horizon_ends_at,
        )
        if (next_horizon_ends_at - horizon_starts_at).days > max_capacity_search_days:
            raise ValueError(
                "resource schedule did not finish after expanding recurring "
                "calendar capacity over the internal safety limit"
            )
        extended_input = dict(input_data)
        extended_options = dict(options)
        extended_options["horizon_ends_at"] = next_horizon_ends_at
        extended_options["_capacity_search_attempt"] = capacity_search_attempt + 1
        extended_input["options"] = extended_options
        return compute_resource_schedule(extended_input)

    warnings: list[dict[str, object]] = []
    if not converged:
        warnings.append(
            {
                "code": "max_iterations_reached",
                "message": "Resource schedule reached max_iterations before convergence.",
                "severity": "warning",
                "details": {"max_iterations": max_iterations},
            }
        )
        unallocated = _add_iteration_not_converged_reasons(
            project_id=project_id,
            unallocated=unallocated,
            requirements_by_process=requirements_by_process,
            dependencies=dependencies,
            changed_process_ids=final_comparison["changed_process_ids"],
        )

    all_slices = _with_slice_ids(
        slices=final_iteration["allocation_slices"],
        project_id=project_id,
        as_of=as_of,
        horizon_starts_at=horizon_starts_at,
        horizon_ends_at=horizon_ends_at,
        options=options,
    )
    _validate_resource_bucket_process_focus(
        slices=all_slices,
    )
    output_slices = all_slices if include_slices else []
    processes_out = list(final_iteration["processes"])
    _attach_allocation_diagnostics(
        rows=processes_out,
        unallocated=unallocated,
    )
    _attach_resource_schedule_windows(
        rows=processes_out,
        allocation_slices=all_slices,
        processes=processes,
        dependencies=dependencies,
        topo_order=topo_order,
    )
    critical_path = _resource_critical_path(
        processes=processes_out,
        dependencies=dependencies,
        cpm_by_id=cpm_by_id,
        tolerance_hours=tolerance_hours,
    )

    return {
        "project_id": project_id,
        "as_of": as_of,
        "now": now,
        "horizon_starts_at": horizon_starts_at,
        "horizon_ends_at": horizon_ends_at,
        "planning_granularity": planning_granularity,
        "processes": processes_out,
        "allocation_slices": output_slices,
        "critical_path_process_ids": critical_path,
        "converged": converged,
        "iteration_count": iteration_count,
        "convergence": {
            "converged": converged,
            "iteration_count": iteration_count,
            "max_iterations": max_iterations,
            "tolerance_hours": tolerance_hours,
            "changed_process_ids": final_comparison["changed_process_ids"],
            "reason_changes": final_comparison["reason_changes"],
            "allocation_fingerprint_changed": final_comparison[
                "allocation_fingerprint_changed"
            ],
        },
        "warnings": warnings,
    }


def allocation_slice_fingerprint(slices: list[dict[str, object]]) -> tuple[tuple[Any, ...], ...]:
    """Return a convergence fingerprint for allocation slices.

    The fingerprint intentionally excludes computed `slice_id`, final
    `iteration`, and non-authoritative `cost_amount` values.
    """
    rows = []
    for allocation in slices:
        rows.append(
            (
                str(allocation["process_id"]),
                str(allocation["requirement_id"]),
                str(allocation["role_id"]),
                str(allocation["resource_id"]),
                _fingerprint_value(allocation["starts_at"]),
                _fingerprint_value(allocation["ends_at"]),
                round(float(allocation["effort_hours"]), 6),
                round(float(allocation["capacity_hours"]), 6),
                None
                if allocation.get("cost_currency") is None
                else str(allocation.get("cost_currency")),
            )
        )
    return tuple(sorted(rows))


def compare_resource_schedule_iterations(
    previous: Mapping[str, object],
    current: Mapping[str, object],
    *,
    tolerance_hours: float = 0,
) -> dict[str, object]:
    """Compare two normalized resource schedule iterations.

    Args:
        previous: Prior iteration state with process rows, unallocated reasons,
            and allocation slices.
        current: Current iteration state in the same shape.
        tolerance_hours: Allowed datetime movement before a process is changed.

    Returns:
        Convergence evidence with changed process ids, reason changes, and
        allocation fingerprint stability.
    """
    previous_rows = _rows_by_process(previous)
    current_rows = _rows_by_process(current)
    previous_reasons = _reason_map(previous)
    current_reasons = _reason_map(current)

    changed_process_ids: set[str] = set()
    for process_id in sorted(set(previous_rows) | set(current_rows)):
        before = previous_rows.get(process_id)
        after = current_rows.get(process_id)
        if before is None or after is None:
            changed_process_ids.add(process_id)
            continue
        for field in ("ready_at", "starts_at", "ends_at"):
            if _datetime_changed(before.get(field), after.get(field), tolerance_hours):
                changed_process_ids.add(process_id)
        if (
            "requirement_ids" not in before
            and "requirement_ids" not in after
            and before.get("ready_at") is not None
            and before.get("ready_at") == after.get("ready_at")
            and before.get("starts_at") is not None
            and before.get("starts_at") != before.get("ready_at")
        ):
            changed_process_ids.add(process_id)
        if before.get("allocation_state") != after.get("allocation_state"):
            changed_process_ids.add(process_id)

    reason_changes = []
    for key in sorted(set(previous_reasons) | set(current_reasons)):
        before_reason = previous_reasons.get(key)
        after_reason = current_reasons.get(key)
        if before_reason == after_reason:
            continue
        process_id, requirement_id = key
        changed_process_ids.add(process_id)
        reason_changes.append(
            {
                "process_id": process_id,
                "requirement_id": requirement_id,
                "before_reason": before_reason,
                "after_reason": after_reason,
            }
        )

    allocation_changed = allocation_slice_fingerprint(
        list(previous.get("allocation_slices", []))
    ) != allocation_slice_fingerprint(list(current.get("allocation_slices", [])))

    return {
        "converged": not changed_process_ids and not allocation_changed,
        "changed_process_ids": sorted(changed_process_ids),
        "reason_changes": reason_changes,
        "allocation_fingerprint_changed": allocation_changed,
    }


def _run_allocation_iteration(
    *,
    project_id: str,
    project_start_at: dt.datetime,
    processes: list[Mapping[str, object]],
    dependencies: dict[str, tuple[str, ...]],
    topo_order: tuple[str, ...],
    cpm_by_id: dict[str, _ProcessCpm],
    requirements_by_process: dict[str, list[Mapping[str, object]]],
    active_role_ids: set[str],
    resources: list[Mapping[str, object]],
    resource_by_id: dict[str, Mapping[str, object]],
    expanded_buckets: tuple[_LedgerBucket, ...],
    blocked_process_ids: set[str],
    blocked_policy: str,
    horizon_ends_at: dt.datetime,
    iteration: int,
) -> dict[str, object]:
    ledger = _fresh_ledger(expanded_buckets)
    daily_allocated: dict[tuple[str, str, str, str], float] = defaultdict(float)
    bucket_focus: dict[tuple[str, dt.datetime, dt.datetime], str] = {}
    process_rows: dict[str, dict[str, object]] = {}
    requirement_states = {
        _requirement_state_key(requirement): _RequirementState(requirement=requirement)
        for requirement in requirements_by_process.values()
        for requirement in requirement
    }
    allocation_slices: list[dict[str, object]] = []
    unallocated: list[dict[str, object]] = []
    completed_process_ids: set[str] = set()
    closed_process_ids: set[str] = set()
    processes_by_id = {str(item["process_id"]): item for item in processes}

    _initialize_requirement_eligibility(
        requirement_states=requirement_states,
        active_role_ids=active_role_ids,
        resources=resources,
        expanded_buckets=expanded_buckets,
    )

    bucket_intervals = sorted({(bucket.starts_at, bucket.ends_at) for bucket in ledger.values()})
    if not bucket_intervals:
        bucket_intervals = [(project_start_at, horizon_ends_at)]

    for _starts_at, bucket_ends_at in bucket_intervals:
        _settle_ready_processes(
            project_id=project_id,
            until_at=bucket_ends_at,
            topo_order=topo_order,
            dependencies=dependencies,
            process_rows=process_rows,
            completed_process_ids=completed_process_ids,
            closed_process_ids=closed_process_ids,
            cpm_by_id=cpm_by_id,
            requirements_by_process=requirements_by_process,
            requirement_states=requirement_states,
            blocked_process_ids=blocked_process_ids,
            blocked_policy=blocked_policy,
            project_start_at=project_start_at,
            processes_by_id=processes_by_id,
            unallocated=unallocated,
        )
        resources_used_by_requirement: dict[
            tuple[str, str, dt.datetime, dt.datetime],
            set[str],
        ]
        resources_used_by_requirement = defaultdict(set)
        for bucket in sorted(
            (
                item
                for item in ledger.values()
                if item.starts_at == _starts_at and item.ends_at == bucket_ends_at
            ),
            key=lambda item: _bucket_resource_sort_key(item, resource_by_id),
        ):
            if bucket.remaining_hours <= EPSILON:
                continue
            candidates = _ready_split_candidates_for_bucket(
                bucket=bucket,
                active_role_ids=active_role_ids,
                resources=resources,
                resource_by_id=resource_by_id,
                daily_allocated=daily_allocated,
                dependencies=dependencies,
                process_rows=process_rows,
                completed_process_ids=completed_process_ids,
                cpm_by_id=cpm_by_id,
                requirements_by_process=requirements_by_process,
                requirement_states=requirement_states,
                closed_process_ids=closed_process_ids,
                project_start_at=project_start_at,
                processes_by_id=processes_by_id,
                ledger=ledger,
                resources_used_by_requirement=resources_used_by_requirement,
                bucket_focus=bucket_focus,
            )
            assignments = _water_fill_requirement_candidates(
                candidates,
                bucket.remaining_hours,
            )
            for candidate, amount in assignments:
                if amount <= EPSILON:
                    continue
                _consume_bucket(
                    candidate.bucket,
                    amount,
                    daily_allocated,
                    candidate.requirement,
                )
                process_id, requirement_id = _requirement_state_key(
                    candidate.requirement
                )
                bucket_focus[_bucket_focus_key(bucket)] = process_id
                resources_used_by_requirement[
                    (
                        process_id,
                        requirement_id,
                        bucket.starts_at,
                        bucket.ends_at,
                    )
                ].add(bucket.resource_id)
                allocation = _allocation_row(
                    project_id=project_id,
                    requirement=candidate.requirement,
                    resource=candidate.resource,
                    bucket=candidate.bucket,
                    effort_hours=amount,
                    ready_at=candidate.ready_at,
                    iteration=iteration,
                )
                allocation_slices.append(allocation)
                _apply_allocation_to_requirement_state(
                    candidate.state,
                    allocation,
                    amount,
                )
            if bucket.remaining_hours > EPSILON and _allocate_contiguous_ready_requirements(
                project_id=project_id,
                bucket=bucket,
                active_role_ids=active_role_ids,
                resources=resources,
                resource_by_id=resource_by_id,
                ledger=ledger,
                daily_allocated=daily_allocated,
                dependencies=dependencies,
                process_rows=process_rows,
                completed_process_ids=completed_process_ids,
                cpm_by_id=cpm_by_id,
                requirements_by_process=requirements_by_process,
                requirement_states=requirement_states,
                project_start_at=project_start_at,
                processes_by_id=processes_by_id,
                horizon_ends_at=horizon_ends_at,
                bucket_focus=bucket_focus,
                iteration=iteration,
                allocation_slices=allocation_slices,
            ):
                _complete_fulfilled_processes(
                    topo_order=topo_order,
                    completed_process_ids=completed_process_ids,
                    closed_process_ids=closed_process_ids,
                    requirements_by_process=requirements_by_process,
                    requirement_states=requirement_states,
                    cpm_by_id=cpm_by_id,
                    process_rows=process_rows,
                )
        _complete_fulfilled_processes(
            topo_order=topo_order,
            completed_process_ids=completed_process_ids,
            closed_process_ids=closed_process_ids,
            requirements_by_process=requirements_by_process,
            requirement_states=requirement_states,
            cpm_by_id=cpm_by_id,
            process_rows=process_rows,
        )

    _settle_ready_processes(
        project_id=project_id,
        until_at=horizon_ends_at,
        topo_order=topo_order,
        dependencies=dependencies,
        process_rows=process_rows,
        completed_process_ids=completed_process_ids,
        closed_process_ids=closed_process_ids,
        cpm_by_id=cpm_by_id,
        requirements_by_process=requirements_by_process,
        requirement_states=requirement_states,
        blocked_process_ids=blocked_process_ids,
        blocked_policy=blocked_policy,
        project_start_at=project_start_at,
        processes_by_id=processes_by_id,
        unallocated=unallocated,
    )
    _complete_fulfilled_processes(
        topo_order=topo_order,
        completed_process_ids=completed_process_ids,
        closed_process_ids=closed_process_ids,
        requirements_by_process=requirements_by_process,
        requirement_states=requirement_states,
        cpm_by_id=cpm_by_id,
        process_rows=process_rows,
    )
    _finalize_open_processes(
        project_id=project_id,
        topo_order=topo_order,
        dependencies=dependencies,
        process_rows=process_rows,
        completed_process_ids=completed_process_ids,
        closed_process_ids=closed_process_ids,
        cpm_by_id=cpm_by_id,
        requirements_by_process=requirements_by_process,
        requirement_states=requirement_states,
        project_start_at=project_start_at,
        processes_by_id=processes_by_id,
        ledger=ledger,
        horizon_ends_at=horizon_ends_at,
        unallocated=unallocated,
    )

    rows = [process_rows[process_id] for process_id in topo_order]
    return {
        "processes": rows,
        "allocation_slices": _coalesced_slices(allocation_slices),
        "unallocated_requirements": sorted(
            unallocated,
            key=lambda item: (item["process_id"], item["requirement_id"], item["reason"]),
        ),
        "iteration_count": iteration,
    }


def _initialize_requirement_eligibility(
    *,
    requirement_states: dict[tuple[str, str], _RequirementState],
    active_role_ids: set[str],
    resources: list[Mapping[str, object]],
    expanded_buckets: tuple[_LedgerBucket, ...],
) -> None:
    buckets_by_resource = defaultdict(list)
    for bucket in expanded_buckets:
        if bucket.capacity_hours > EPSILON:
            buckets_by_resource[bucket.resource_id].append(bucket)

    for state in requirement_states.values():
        requirement = state.requirement
        role_id = str(requirement["role_id"])
        if role_id not in active_role_ids:
            state.reason = "missing_role"
            state.eligible_resource_ids = ()
            continue
        eligible = _eligible_resources(requirement, active_role_ids, resources)
        eligible_ids = tuple(str(resource["resource_id"]) for resource in eligible)
        state.eligible_resource_ids = eligible_ids
        if not eligible_ids:
            state.reason = "no_eligible_resource"
            continue
        if not any(buckets_by_resource[resource_id] for resource_id in eligible_ids):
            state.reason = "no_calendar_capacity"


def _settle_ready_processes(
    *,
    project_id: str,
    until_at: dt.datetime,
    topo_order: tuple[str, ...],
    dependencies: dict[str, tuple[str, ...]],
    process_rows: dict[str, dict[str, object]],
    completed_process_ids: set[str],
    closed_process_ids: set[str],
    cpm_by_id: dict[str, _ProcessCpm],
    requirements_by_process: dict[str, list[Mapping[str, object]]],
    requirement_states: dict[tuple[str, str], _RequirementState],
    blocked_process_ids: set[str],
    blocked_policy: str,
    project_start_at: dt.datetime,
    processes_by_id: dict[str, Mapping[str, object]],
    unallocated: list[dict[str, object]],
) -> None:
    made_progress = True
    while made_progress:
        made_progress = False
        for process_id in topo_order:
            if process_id in closed_process_ids:
                continue
            ready_at = _process_ready_at(
                process_id=process_id,
                dependencies=dependencies,
                process_rows=process_rows,
                completed_process_ids=completed_process_ids,
                cpm_by_id=cpm_by_id,
                project_start_at=project_start_at,
                processes_by_id=processes_by_id,
            )
            if ready_at is None or ready_at >= until_at:
                continue

            process_requirements = requirements_by_process.get(process_id, [])
            process = processes_by_id[process_id]
            if (
                str(process.get("explicit_status", "")) == "done"
                or process.get("finished_at") is not None
            ):
                _finalize_done_process(
                    process_id=process_id,
                    ready_at=ready_at,
                    process_rows=process_rows,
                    cpm_by_id=cpm_by_id,
                    processes_by_id=processes_by_id,
                    requirement_ids=[
                        str(requirement["requirement_id"])
                        for requirement in process_requirements
                    ],
                )
                completed_process_ids.add(process_id)
                closed_process_ids.add(process_id)
                made_progress = True
                continue
            if process_id in blocked_process_ids and blocked_policy != "include_normally":
                state = (
                    "blocked_zero_capacity"
                    if blocked_policy == "include_as_zero_capacity"
                    else "unallocated"
                )
                _finalize_blocked(
                    project_id=project_id,
                    process_id=process_id,
                    ready_at=ready_at,
                    state=state,
                    cpm_by_id=cpm_by_id,
                    requirements=process_requirements,
                    process_rows=process_rows,
                    unallocated=unallocated,
                    emit_reason=blocked_policy == "exclude",
                )
                closed_process_ids.add(process_id)
                made_progress = True
                continue

            if not process_requirements:
                _finalize_no_requirement_process(
                    process_id=process_id,
                    ready_at=ready_at,
                    process_rows=process_rows,
                    cpm_by_id=cpm_by_id,
                    processes_by_id=processes_by_id,
                )
                completed_process_ids.add(process_id)
                closed_process_ids.add(process_id)
                made_progress = True
                continue

            for requirement in process_requirements:
                state = requirement_states[_requirement_state_key(requirement)]
                state.ready_at = ready_at


def _ready_split_candidates_for_bucket(
    *,
    bucket: _LedgerBucket,
    active_role_ids: set[str],
    resources: list[Mapping[str, object]],
    resource_by_id: dict[str, Mapping[str, object]],
    daily_allocated: dict[tuple[str, str, str, str], float],
    dependencies: dict[str, tuple[str, ...]],
    process_rows: dict[str, dict[str, object]],
    completed_process_ids: set[str],
    cpm_by_id: dict[str, _ProcessCpm],
    requirements_by_process: dict[str, list[Mapping[str, object]]],
    requirement_states: dict[tuple[str, str], _RequirementState],
    closed_process_ids: set[str],
    project_start_at: dt.datetime,
    processes_by_id: dict[str, Mapping[str, object]],
    ledger: dict[tuple[str, dt.datetime, dt.datetime], _LedgerBucket],
    resources_used_by_requirement: dict[
        tuple[str, str, dt.datetime, dt.datetime],
        set[str],
    ],
    bucket_focus: dict[tuple[str, dt.datetime, dt.datetime], str],
) -> list[_RequirementCandidate]:
    candidates = []
    role_availability_by_key: dict[tuple[str, str, dt.datetime], float] = {}
    focused_process_id = bucket_focus.get(_bucket_focus_key(bucket))
    for process_id in cpm_by_id:
        if focused_process_id is not None and process_id != focused_process_id:
            continue
        if process_id in completed_process_ids or process_id in closed_process_ids:
            continue
        ready_at = _process_ready_at(
            process_id=process_id,
            dependencies=dependencies,
            process_rows=process_rows,
            completed_process_ids=completed_process_ids,
            cpm_by_id=cpm_by_id,
            project_start_at=project_start_at,
            processes_by_id=processes_by_id,
        )
        if ready_at is None or ready_at >= bucket.ends_at:
            continue
        for requirement in requirements_by_process.get(process_id, []):
            if str(requirement.get("allocation_policy", "split_allowed")) != "split_allowed":
                continue
            requirement_key = _requirement_state_key(requirement)
            state = requirement_states[requirement_key]
            if state.reason is not None:
                continue
            remaining = float(requirement["effort_hours"]) - state.allocated_hours
            if remaining <= EPSILON:
                continue
            role_id = str(requirement["role_id"])
            if role_id not in bucket.role_ids or role_id not in active_role_ids:
                continue
            eligible_resource_ids = set(state.eligible_resource_ids)
            if bucket.resource_id not in eligible_resource_ids:
                continue
            concurrency_key = (
                requirement_key[0],
                requirement_key[1],
                bucket.starts_at,
                bucket.ends_at,
            )
            used_resources = resources_used_by_requirement[concurrency_key]
            required_count = max(1, int(requirement.get("required_resource_count", 1)))
            if (
                bucket.resource_id not in used_resources
                and len(used_resources) >= required_count
            ):
                continue
            headroom = _candidate_headroom(
                requirement=requirement,
                bucket=bucket,
                daily_allocated=daily_allocated,
                remaining=remaining,
                ready_at=ready_at,
            )
            if headroom <= EPSILON:
                continue
            minimum = float(requirement.get("min_allocation_hours_per_day", 0) or 0)
            if minimum:
                block_headroom = _block_headroom(
                    requirement=requirement,
                    bucket=bucket,
                    ledger=ledger,
                    daily_allocated=daily_allocated,
                    remaining=remaining,
                    ready_at=ready_at,
                )
                daily_key = _daily_allocation_key(
                    requirement,
                    bucket.resource_id,
                    bucket.local_date,
                )
                if (
                    daily_allocated[daily_key] <= EPSILON
                    and block_headroom + EPSILON < minimum
                    and remaining - block_headroom > EPSILON
                ):
                    continue
            feasible_start = max(bucket.starts_at, ready_at)
            if (
                state.first_feasible_starts_at is None
                or feasible_start < state.first_feasible_starts_at
            ):
                state.first_feasible_starts_at = feasible_start
            state.ready_at = ready_at
            cpm = cpm_by_id[process_id]
            availability_key = (process_id, role_id, feasible_start)
            if availability_key not in role_availability_by_key:
                role_availability_by_key[availability_key] = (
                    _role_remaining_capacity_hours(
                        role_id=role_id,
                        process_id=process_id,
                        resources=resources,
                        ledger=ledger,
                        bucket_focus=bucket_focus,
                        starts_at=feasible_start,
                    )
                )
            role_availability = role_availability_by_key[availability_key]
            candidates.append(
                _RequirementCandidate(
                    requirement=requirement,
                    state=state,
                    bucket=bucket,
                    resource=resource_by_id[bucket.resource_id],
                    ready_at=ready_at,
                    headroom_hours=headroom,
                    role_availability_hours=role_availability,
                    sort_key=(
                        role_availability,
                        ready_at,
                        cpm.topo_index,
                        process_id,
                        str(requirement["requirement_id"]),
                        bucket.resource_id,
                    ),
                )
            )
    return _focused_process_candidates(candidates)


def _focused_process_candidates(
    candidates: list[_RequirementCandidate],
) -> list[_RequirementCandidate]:
    if not candidates:
        return []
    ordered = sorted(candidates, key=lambda item: item.sort_key)
    focus_process_id = str(ordered[0].requirement["process_id"])
    return [
        candidate
        for candidate in ordered
        if str(candidate.requirement["process_id"]) == focus_process_id
    ]


def _water_fill_requirement_candidates(
    candidates: list[_RequirementCandidate],
    capacity_hours: float,
) -> list[tuple[_RequirementCandidate, float]]:
    if not candidates or capacity_hours + EPSILON < 1:
        return []
    for tier in _role_availability_tiers(candidates):
        for candidate in tier:
            if candidate.headroom_hours + EPSILON >= 1:
                return [(candidate, 1.0)]
    return []


def _role_availability_tiers(
    candidates: list[_RequirementCandidate],
) -> list[list[_RequirementCandidate]]:
    """Group candidates by increasing remaining eligible role capacity."""
    tiers: list[list[_RequirementCandidate]] = []
    for candidate in sorted(candidates, key=lambda item: item.sort_key):
        if (
            tiers
            and abs(
                tiers[-1][0].role_availability_hours
                - candidate.role_availability_hours
            )
            <= EPSILON
        ):
            tiers[-1].append(candidate)
        else:
            tiers.append([candidate])
    return tiers


def _water_fill_candidate_tier(
    candidates: list[_RequirementCandidate],
    capacity_hours: float,
) -> list[tuple[_RequirementCandidate, float]]:
    return [
        (candidate, 1.0)
        for candidate in candidates[: int(capacity_hours)]
        if candidate.headroom_hours + EPSILON >= 1
    ]


def _apply_allocation_to_requirement_state(
    state: _RequirementState,
    allocation: dict[str, object],
    amount: float,
) -> None:
    state.reason = None
    state.allocated_hours += amount
    starts_at = _as_utc(allocation["starts_at"])
    ends_at = _as_utc(allocation["ends_at"])
    if state.starts_at is None or starts_at < state.starts_at:
        state.starts_at = starts_at
    if state.ends_at is None or ends_at > state.ends_at:
        state.ends_at = ends_at
    if (
        state.first_feasible_starts_at is None
        or starts_at < state.first_feasible_starts_at
    ):
        state.first_feasible_starts_at = starts_at


def _allocate_contiguous_ready_requirements(
    *,
    project_id: str,
    bucket: _LedgerBucket,
    active_role_ids: set[str],
    resources: list[Mapping[str, object]],
    resource_by_id: dict[str, Mapping[str, object]],
    ledger: dict[tuple[str, dt.datetime, dt.datetime], _LedgerBucket],
    daily_allocated: dict[tuple[str, str, str, str], float],
    dependencies: dict[str, tuple[str, ...]],
    process_rows: dict[str, dict[str, object]],
    completed_process_ids: set[str],
    cpm_by_id: dict[str, _ProcessCpm],
    requirements_by_process: dict[str, list[Mapping[str, object]]],
    requirement_states: dict[tuple[str, str], _RequirementState],
    project_start_at: dt.datetime,
    processes_by_id: dict[str, Mapping[str, object]],
    horizon_ends_at: dt.datetime,
    bucket_focus: dict[tuple[str, dt.datetime, dt.datetime], str],
    iteration: int,
    allocation_slices: list[dict[str, object]],
) -> bool:
    del bucket, horizon_ends_at
    changed = False
    for process_id in cpm_by_id:
        if process_id in completed_process_ids or process_id in process_rows:
            continue
        ready_at = _process_ready_at(
            process_id=process_id,
            dependencies=dependencies,
            process_rows=process_rows,
            completed_process_ids=completed_process_ids,
            cpm_by_id=cpm_by_id,
            project_start_at=project_start_at,
            processes_by_id=processes_by_id,
        )
        if ready_at is None:
            continue
        for requirement in requirements_by_process.get(process_id, []):
            if str(requirement.get("allocation_policy", "split_allowed")) != "contiguous":
                continue
            state = requirement_states[_requirement_state_key(requirement)]
            if state.reason is not None:
                continue
            if state.allocated_hours + EPSILON >= float(requirement["effort_hours"]):
                continue
            eligible = _eligible_resources(requirement, active_role_ids, resources)
            new_slices = _allocate_contiguous(
                project_id=project_id,
                requirement=requirement,
                ready_at=ready_at,
                eligible=eligible,
                resource_by_id=resource_by_id,
                ledger=ledger,
                daily_allocated=daily_allocated,
                bucket_focus=bucket_focus,
                iteration=iteration,
            )
            for allocation in new_slices:
                allocation_slices.append(allocation)
                allocation_bucket = _allocation_bucket_key(allocation)
                bucket_focus[allocation_bucket] = str(allocation["process_id"])
                _apply_allocation_to_requirement_state(
                    state,
                    allocation,
                    float(allocation["effort_hours"]),
                )
                changed = True
            if not new_slices:
                state.ready_at = ready_at
    return changed


def _complete_fulfilled_processes(
    *,
    topo_order: tuple[str, ...],
    completed_process_ids: set[str],
    closed_process_ids: set[str],
    requirements_by_process: dict[str, list[Mapping[str, object]]],
    requirement_states: dict[tuple[str, str], _RequirementState],
    cpm_by_id: dict[str, _ProcessCpm],
    process_rows: dict[str, dict[str, object]],
) -> None:
    for process_id in topo_order:
        if process_id in closed_process_ids:
            continue
        requirements = requirements_by_process.get(process_id, [])
        if not requirements:
            continue
        if not all(
            requirement_states[_requirement_state_key(requirement)].allocated_hours
            + EPSILON
            >= float(requirement["effort_hours"])
            for requirement in requirements
        ):
            continue
        _finalize_process_with_requirements(
            process_id=process_id,
            requirements=requirements,
            states=requirement_states,
            cpm_by_id=cpm_by_id,
            process_rows=process_rows,
        )
        if process_rows[process_id]["allocation_state"] == "complete":
            completed_process_ids.add(process_id)
            closed_process_ids.add(process_id)


def _finalize_open_processes(
    *,
    project_id: str,
    topo_order: tuple[str, ...],
    dependencies: dict[str, tuple[str, ...]],
    process_rows: dict[str, dict[str, object]],
    completed_process_ids: set[str],
    closed_process_ids: set[str],
    cpm_by_id: dict[str, _ProcessCpm],
    requirements_by_process: dict[str, list[Mapping[str, object]]],
    requirement_states: dict[tuple[str, str], _RequirementState],
    project_start_at: dt.datetime,
    processes_by_id: dict[str, Mapping[str, object]],
    ledger: dict[tuple[str, dt.datetime, dt.datetime], _LedgerBucket],
    horizon_ends_at: dt.datetime,
    unallocated: list[dict[str, object]],
) -> None:
    for process_id in topo_order:
        if process_id in closed_process_ids:
            continue
        requirements = requirements_by_process.get(process_id, [])
        if not all(
            dependency in completed_process_ids
            for dependency in dependencies.get(process_id, ())
        ):
            _finalize_predecessor_unallocated(
                project_id=project_id,
                process_id=process_id,
                cpm_by_id=cpm_by_id,
                requirements=requirements,
                process_rows=process_rows,
                unallocated=unallocated,
            )
            closed_process_ids.add(process_id)
            continue
        ready_at = _process_ready_at(
            process_id=process_id,
            dependencies=dependencies,
            process_rows=process_rows,
            completed_process_ids=completed_process_ids,
            cpm_by_id=cpm_by_id,
            project_start_at=project_start_at,
            processes_by_id=processes_by_id,
        )
        if ready_at is None:
            continue
        process = processes_by_id[process_id]
        if (
            str(process.get("explicit_status", "")) == "done"
            or process.get("finished_at") is not None
        ):
            _finalize_done_process(
                process_id=process_id,
                ready_at=ready_at,
                process_rows=process_rows,
                cpm_by_id=cpm_by_id,
                processes_by_id=processes_by_id,
                requirement_ids=[
                    str(requirement["requirement_id"]) for requirement in requirements
                ],
            )
            completed_process_ids.add(process_id)
            closed_process_ids.add(process_id)
            continue
        if not requirements:
            _finalize_no_requirement_process(
                process_id=process_id,
                ready_at=ready_at,
                process_rows=process_rows,
                cpm_by_id=cpm_by_id,
                processes_by_id=processes_by_id,
            )
            completed_process_ids.add(process_id)
            closed_process_ids.add(process_id)
            continue
        for requirement in requirements:
            state = requirement_states[_requirement_state_key(requirement)]
            if state.ready_at is None and ready_at is not None:
                state.ready_at = ready_at
            if state.allocated_hours + EPSILON >= float(requirement["effort_hours"]):
                continue
            if state.reason is None:
                state.reason = _incomplete_reason(
                    requirement,
                    state,
                    ledger=ledger,
                    horizon_ends_at=horizon_ends_at,
                )
        _finalize_process_with_requirements(
            process_id=process_id,
            requirements=requirements,
            states=requirement_states,
            cpm_by_id=cpm_by_id,
            process_rows=process_rows,
        )
        for requirement in requirements:
            state = requirement_states[_requirement_state_key(requirement)]
            if state.allocated_hours + EPSILON < float(requirement["effort_hours"]):
                unallocated.append(
                    _unallocated_row(
                        project_id=project_id,
                        requirement=requirement,
                        state=state,
                        ledger=ledger,
                        horizon_ends_at=horizon_ends_at,
                    )
                )
        closed_process_ids.add(process_id)


def _allocate_requirement(
    *,
    project_id: str,
    requirement: Mapping[str, object],
    ready_at: dt.datetime,
    active_role_ids: set[str],
    resources: list[Mapping[str, object]],
    resource_by_id: dict[str, Mapping[str, object]],
    ledger: dict[tuple[str, dt.datetime, dt.datetime], _LedgerBucket],
    daily_allocated: dict[tuple[str, str, str, str], float],
    horizon_ends_at: dt.datetime,
    iteration: int,
) -> list[dict[str, object]]:
    role_id = str(requirement["role_id"])
    state = _current_requirement_state(requirement)
    state.ready_at = ready_at
    eligible = _eligible_resources(requirement, active_role_ids, resources)
    state.eligible_resource_ids = tuple(str(resource["resource_id"]) for resource in eligible)
    if role_id not in active_role_ids:
        state.reason = "missing_role"
        requirement["_state"] = state  # type: ignore[index]
        return []
    if not eligible:
        state.reason = "no_eligible_resource"
        requirement["_state"] = state  # type: ignore[index]
        return []

    eligible_ids = {str(resource["resource_id"]) for resource in eligible}
    buckets = [
        bucket
        for bucket in sorted(ledger.values(), key=_bucket_sort_key)
        if bucket.resource_id in eligible_ids
        and bucket.ends_at > ready_at
        and bucket.starts_at < horizon_ends_at
        and bucket.capacity_hours > EPSILON
    ]
    if not buckets:
        state.reason = "no_calendar_capacity"
        requirement["_state"] = state  # type: ignore[index]
        return []

    state.first_feasible_starts_at = min(max(bucket.starts_at, ready_at) for bucket in buckets)
    if str(requirement.get("allocation_policy", "split_allowed")) == "contiguous":
        slices = _allocate_contiguous(
            project_id=project_id,
            requirement=requirement,
            ready_at=ready_at,
            eligible=eligible,
            resource_by_id=resource_by_id,
            ledger=ledger,
            daily_allocated=daily_allocated,
            bucket_focus={},
            iteration=iteration,
        )
    else:
        slices = _allocate_split(
            project_id=project_id,
            requirement=requirement,
            ready_at=ready_at,
            eligible=eligible,
            resource_by_id=resource_by_id,
            ledger=ledger,
            daily_allocated=daily_allocated,
            iteration=iteration,
        )
    if slices:
        state.reason = None
    else:
        state.reason = "contiguous_window_unavailable" if str(
            requirement.get("allocation_policy", "split_allowed")
        ) == "contiguous" else "horizon_exhausted"
    requirement["_state"] = state  # type: ignore[index]
    return slices


def _allocate_split(
    *,
    project_id: str,
    requirement: Mapping[str, object],
    ready_at: dt.datetime,
    eligible: list[Mapping[str, object]],
    resource_by_id: dict[str, Mapping[str, object]],
    ledger: dict[tuple[str, dt.datetime, dt.datetime], _LedgerBucket],
    daily_allocated: dict[tuple[str, str, str, str], float],
    iteration: int,
) -> list[dict[str, object]]:
    remaining = float(requirement["effort_hours"])
    required_count = max(1, int(requirement.get("required_resource_count", 1)))
    slices: list[dict[str, object]] = []
    eligible_ids = {str(resource["resource_id"]) for resource in eligible}

    bucket_times = sorted(
        {
            (bucket.starts_at, bucket.ends_at)
            for bucket in ledger.values()
            if bucket.resource_id in eligible_ids and bucket.ends_at > ready_at
        }
    )
    for starts_at, ends_at in bucket_times:
        if remaining <= EPSILON:
            break
        candidates = []
        for bucket in ledger.values():
            if (
                bucket.starts_at != starts_at
                or bucket.ends_at != ends_at
                or bucket.resource_id not in eligible_ids
                or bucket.remaining_hours <= EPSILON
                or bucket.ends_at <= ready_at
            ):
                continue
            headroom = _candidate_headroom(
                requirement=requirement,
                bucket=bucket,
                daily_allocated=daily_allocated,
                remaining=remaining,
                ready_at=ready_at,
            )
            if headroom <= EPSILON:
                continue
            minimum = float(requirement.get("min_allocation_hours_per_day", 0) or 0)
            block_headroom = _block_headroom(
                requirement=requirement,
                bucket=bucket,
                ledger=ledger,
                daily_allocated=daily_allocated,
                remaining=remaining,
                ready_at=ready_at,
            )
            if (
                minimum
                and daily_allocated[
                    _daily_allocation_key(
                        requirement,
                        bucket.resource_id,
                        bucket.local_date,
                    )
                ]
                <= EPSILON
                and block_headroom + EPSILON < minimum
                and remaining - block_headroom > EPSILON
            ):
                continue
            resource = resource_by_id[bucket.resource_id]
            candidates.append(
                (_resource_sort_key(resource, bucket, ready_at), bucket, headroom)
            )

        selected = [
            (bucket, headroom)
            for _, bucket, headroom in sorted(candidates)[:required_count]
        ]
        if not selected:
            continue
        assignments = _water_fill(selected, remaining)
        for bucket, amount in assignments:
            if amount <= EPSILON:
                continue
            _consume_bucket(bucket, amount, daily_allocated, requirement)
            remaining -= amount
            slices.append(
                _allocation_row(
                    project_id=project_id,
                    requirement=requirement,
                    resource=resource_by_id[bucket.resource_id],
                    bucket=bucket,
                    effort_hours=amount,
                    ready_at=ready_at,
                    iteration=iteration,
                )
            )
    return slices


def _allocate_contiguous(
    *,
    project_id: str,
    requirement: Mapping[str, object],
    ready_at: dt.datetime,
    eligible: list[Mapping[str, object]],
    resource_by_id: dict[str, Mapping[str, object]],
    ledger: dict[tuple[str, dt.datetime, dt.datetime], _LedgerBucket],
    daily_allocated: dict[tuple[str, str, str, str], float],
    bucket_focus: dict[tuple[str, dt.datetime, dt.datetime], str],
    iteration: int,
) -> list[dict[str, object]]:
    effort = float(requirement["effort_hours"])
    process_id = str(requirement["process_id"])
    sorted_resources = sorted(
        eligible,
        key=lambda resource: _first_resource_bucket_key(resource, ledger, ready_at),
    )
    for resource in sorted_resources:
        resource_id = str(resource["resource_id"])
        resource_working_buckets = [
            bucket
            for bucket in sorted(ledger.values(), key=_bucket_sort_key)
            if bucket.resource_id == resource_id
            and bucket.ends_at > ready_at
            and bucket.capacity_hours > EPSILON
        ]
        buckets = [
            bucket
            for bucket in resource_working_buckets
            if bucket.remaining_hours > EPSILON
            and bucket_focus.get(_bucket_focus_key(bucket), process_id) == process_id
        ]
        for index, _first in enumerate(buckets):
            total = 0.0
            sequence: list[tuple[_LedgerBucket, float]] = []
            previous_ends_at: dt.datetime | None = None
            for bucket in buckets[index:]:
                if previous_ends_at is not None and _has_working_gap(
                    resource_id=resource_id,
                    buckets=resource_working_buckets,
                    previous_end=previous_ends_at,
                    current_start=bucket.starts_at,
                ):
                    break
                headroom = _candidate_headroom(
                    requirement=requirement,
                    bucket=bucket,
                    daily_allocated=daily_allocated,
                    remaining=effort - total,
                    ready_at=ready_at,
                )
                if headroom <= EPSILON:
                    break
                amount = min(headroom, effort - total)
                sequence.append((bucket, amount))
                total += amount
                previous_ends_at = bucket.ends_at
                if total + EPSILON >= effort:
                    slices = []
                    for selected_bucket, selected_amount in sequence:
                        _consume_bucket(
                            selected_bucket,
                            selected_amount,
                            daily_allocated,
                            requirement,
                        )
                        bucket_focus[_bucket_focus_key(selected_bucket)] = process_id
                        slices.append(
                            _allocation_row(
                                project_id=project_id,
                                requirement=requirement,
                                resource=resource_by_id[selected_bucket.resource_id],
                                bucket=selected_bucket,
                                effort_hours=selected_amount,
                                ready_at=ready_at,
                                iteration=iteration,
                            )
                        )
                    return slices
    return []


def _has_working_gap(
    *,
    resource_id: str,
    buckets: list[_LedgerBucket],
    previous_end: dt.datetime,
    current_start: dt.datetime,
) -> bool:
    return any(
        bucket.resource_id == resource_id
        and bucket.capacity_hours > EPSILON
        and bucket.starts_at < current_start
        and bucket.ends_at > previous_end
        for bucket in buckets
    )


def _current_requirement_state(requirement: Mapping[str, object]) -> _RequirementState:
    state = requirement.get("_state")
    if isinstance(state, _RequirementState):
        return state
    return _RequirementState(requirement=requirement)


def _water_fill(
    selected: list[tuple[_LedgerBucket, float]],
    remaining: float,
) -> list[tuple[_LedgerBucket, float]]:
    demand = int(remaining + EPSILON)
    assignments = []
    for bucket, headroom in selected:
        if demand <= 0:
            break
        if headroom + EPSILON < 1:
            continue
        assignments.append((bucket, 1.0))
        demand -= 1
    return assignments


def _candidate_headroom(
    *,
    requirement: Mapping[str, object],
    bucket: _LedgerBucket,
    daily_allocated: dict[tuple[str, str, str, str], float],
    remaining: float,
    ready_at: dt.datetime,
) -> float:
    cap = requirement.get("max_allocation_hours_per_day")
    daily_residual = math.inf
    if cap is not None:
        key = _daily_allocation_key(requirement, bucket.resource_id, bucket.local_date)
        daily_residual = max(0.0, float(cap) - daily_allocated[key])
    available_after_ready = _bucket_capacity_after(bucket, ready_at)
    if (
        bucket.remaining_hours + EPSILON < 1
        or available_after_ready + EPSILON < 1
        or daily_residual + EPSILON < 1
        or remaining + EPSILON < 1
    ):
        return 0.0
    return 1.0


def _block_headroom(
    *,
    requirement: Mapping[str, object],
    bucket: _LedgerBucket,
    ledger: dict[tuple[str, dt.datetime, dt.datetime], _LedgerBucket],
    daily_allocated: dict[tuple[str, str, str, str], float],
    remaining: float,
    ready_at: dt.datetime,
) -> float:
    total = 0.0
    for candidate in sorted(ledger.values(), key=_bucket_sort_key):
        if (
            candidate.resource_id != bucket.resource_id
            or candidate.local_date != bucket.local_date
            or candidate.window_id != bucket.window_id
            or candidate.starts_at < bucket.starts_at
        ):
            continue
        total += _candidate_headroom(
            requirement=requirement,
            bucket=candidate,
            daily_allocated=daily_allocated,
            remaining=max(0.0, remaining - total),
            ready_at=ready_at,
        )
        if total + EPSILON >= remaining:
            break
    return total


def _consume_bucket(
    bucket: _LedgerBucket,
    amount: float,
    daily_allocated: dict[tuple[str, str, str, str], float],
    requirement: Mapping[str, object],
) -> None:
    bucket.remaining_hours = 0.0
    key = _daily_allocation_key(requirement, bucket.resource_id, bucket.local_date)
    daily_allocated[key] += amount


def _allocation_row(
    *,
    project_id: str,
    requirement: Mapping[str, object],
    resource: Mapping[str, object],
    bucket: _LedgerBucket,
    effort_hours: float,
    ready_at: dt.datetime,
    iteration: int,
) -> dict[str, object]:
    starts_at = max(bucket.starts_at, ready_at)
    return {
        "slice_id": "",
        "project_id": project_id,
        "process_id": str(requirement["process_id"]),
        "requirement_id": str(requirement["requirement_id"]),
        "role_id": str(requirement["role_id"]),
        "resource_id": bucket.resource_id,
        "starts_at": starts_at,
        "ends_at": bucket.ends_at,
        "effort_hours": round(effort_hours, 6),
        "capacity_hours": round(effort_hours, 6),
        "cost_amount": None,
        "cost_currency": None
        if resource.get("cost_currency") is None
        else str(resource.get("cost_currency")),
        "iteration": iteration,
        "_local_date": bucket.local_date,
        "_full_capacity_rate": abs(
            _bucket_capacity_between(bucket, starts_at, bucket.ends_at)
            - round(effort_hours, 6)
        )
        <= EPSILON,
    }


def _coalesced_slices(slices: list[dict[str, object]]) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for item in sorted(
        slices,
        key=lambda row: (
            row["process_id"],
            row["requirement_id"],
            row["role_id"],
            row["resource_id"],
            row["starts_at"],
            row["ends_at"],
        ),
    ):
        if output:
            previous = output[-1]
            if (
                previous["process_id"] == item["process_id"]
                and previous["requirement_id"] == item["requirement_id"]
                and previous["role_id"] == item["role_id"]
                and previous["resource_id"] == item["resource_id"]
                and previous["iteration"] == item["iteration"]
                and previous["cost_currency"] == item["cost_currency"]
                and previous.get("_local_date") == item.get("_local_date")
                and previous["ends_at"] == item["starts_at"]
                and previous.get("_full_capacity_rate")
                and item.get("_full_capacity_rate")
            ):
                previous["ends_at"] = item["ends_at"]
                previous["effort_hours"] = round(
                    float(previous["effort_hours"]) + float(item["effort_hours"]),
                    6,
                )
                previous["capacity_hours"] = round(
                    float(previous["capacity_hours"]) + float(item["capacity_hours"]),
                    6,
                )
                continue
        output.append(dict(item))
    for item in output:
        item.pop("_local_date", None)
        item.pop("_full_capacity_rate", None)
    return sorted(
        output,
        key=lambda row: (
            row["starts_at"],
            row["ends_at"],
            row["process_id"],
            row["requirement_id"],
            row["resource_id"],
        ),
    )


def _with_slice_ids(
    *,
    slices: list[dict[str, object]],
    project_id: str,
    as_of: dt.datetime,
    horizon_starts_at: dt.datetime,
    horizon_ends_at: dt.datetime,
    options: Mapping[str, object],
) -> list[dict[str, object]]:
    del horizon_starts_at, horizon_ends_at
    stable_options = {
        key: value
        for key, value in options.items()
        if key
        not in {
            "horizon_starts_at",
            "horizon_ends_at",
            "_capacity_search_attempt",
            "capacity_search_max_days",
        }
    }
    ordinal_by_material_key: dict[tuple[object, ...], int] = defaultdict(int)
    output = []
    for item in slices:
        material_key = (
            item["process_id"],
            item["requirement_id"],
            item["role_id"],
            item["resource_id"],
            item["starts_at"],
            item["ends_at"],
        )
        ordinal = ordinal_by_material_key[material_key]
        ordinal_by_material_key[material_key] += 1
        payload = {
            "action": "query_resource_schedule",
            "schema": 1,
            "project_id": project_id,
            "as_of": as_of.isoformat(),
            "options": _json_safe(stable_options),
            "iteration": item["iteration"],
            "process_id": item["process_id"],
            "requirement_id": item["requirement_id"],
            "role_id": item["role_id"],
            "resource_id": item["resource_id"],
            "starts_at": item["starts_at"].isoformat(),
            "ends_at": item["ends_at"].isoformat(),
            "ordinal": ordinal,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
        row = dict(item)
        row["slice_id"] = f"slice_{digest}"
        output.append(row)
    return output


def _validate_resource_bucket_process_focus(
    *,
    slices: list[dict[str, object]],
) -> None:
    """Ensure each resource works on at most one process per hour bucket."""
    slices_by_resource: dict[str, list[dict[str, object]]] = defaultdict(list)
    for allocation in slices:
        slices_by_resource[str(allocation["resource_id"])].append(allocation)

    for resource_id, resource_slices in slices_by_resource.items():
        boundaries = sorted(
            {
                boundary
                for allocation in resource_slices
                for boundary in (
                    _as_utc(allocation["starts_at"]),
                    _as_utc(allocation["ends_at"]),
                )
            }
        )
        for starts_at, ends_at in zip(boundaries, boundaries[1:], strict=False):
            if starts_at == ends_at:
                continue
            process_ids = {
                str(allocation["process_id"])
                for allocation in resource_slices
                if _as_utc(allocation["starts_at"]) < ends_at
                and _as_utc(allocation["ends_at"]) > starts_at
            }
            if len(process_ids) <= 1:
                continue
            raise ValueError(
                "resource schedule violates resource focus: "
                f"{resource_id} has allocations for processes "
                f"{sorted(process_ids)} between {starts_at.isoformat()} and "
                f"{ends_at.isoformat()}"
            )


def _collect_dependencies(
    input_data: Mapping[str, object],
    processes: list[Mapping[str, object]],
) -> dict[str, tuple[str, ...]]:
    dependencies: dict[str, list[str]] = {
        str(process["process_id"]): [
            str(dependency) for dependency in process.get("dependencies", ())
        ]
        for process in processes
    }
    for edge in _sequence(input_data.get("dependencies", ())):
        item = _mapping(edge, "dependency")
        predecessor = str(item["predecessor_process_id"])
        successor = str(item["successor_process_id"])
        dependencies.setdefault(successor, [])
        if predecessor not in dependencies[successor]:
            dependencies[successor].append(predecessor)
    return {process_id: tuple(values) for process_id, values in dependencies.items()}


def _topological_order(
    processes: list[Mapping[str, object]],
    dependencies: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    process_ids = [str(process["process_id"]) for process in processes]
    successors: dict[str, list[str]] = {process_id: [] for process_id in process_ids}
    indegree = {process_id: 0 for process_id in process_ids}
    for successor, predecessors in dependencies.items():
        for predecessor in predecessors:
            successors.setdefault(predecessor, []).append(successor)
            indegree[successor] = indegree.get(successor, 0) + 1
    order_index = {process_id: index for index, process_id in enumerate(process_ids)}
    ready = sorted(
        [process_id for process_id in process_ids if indegree.get(process_id, 0) == 0],
        key=order_index.get,
    )
    output = []
    while ready:
        process_id = ready.pop(0)
        output.append(process_id)
        for successor in sorted(successors.get(process_id, ()), key=order_index.get):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)
                ready.sort(key=order_index.get)
    if len(output) != len(process_ids):
        raise ValueError("process dependencies must form a directed acyclic graph")
    return tuple(output)


def _compute_cpm(
    processes: list[Mapping[str, object]],
    dependencies: dict[str, tuple[str, ...]],
    project_start_at: dt.datetime,
    topo_order: tuple[str, ...],
) -> dict[str, _ProcessCpm]:
    process_by_id = {str(process["process_id"]): process for process in processes}
    project_start = project_start_at
    earliest_start: dict[str, dt.datetime] = {}
    earliest_finish: dict[str, dt.datetime] = {}
    for process_id in topo_order:
        process = process_by_id[process_id]
        dependency_finish = max(
            (earliest_finish[dependency] for dependency in dependencies.get(process_id, ())),
            default=project_start,
        )
        if process.get("started_at") is not None:
            start = _as_utc(process["started_at"])
        else:
            constraints = [dependency_finish]
            if process.get("earliest_start_at") is not None:
                constraints.append(next_business_day(_as_utc(process["earliest_start_at"])))
            delay_days = int(process.get("delay_after_dependencies_business_days", 0) or 0)
            if delay_days:
                constraints.append(add_business_days(dependency_finish, delay_days))
            start = max(constraints)
        earliest_start[process_id] = start
        if process.get("finished_at") is not None:
            earliest_finish[process_id] = _as_utc(process["finished_at"])
        else:
            earliest_finish[process_id] = add_business_days(
                start,
                int(process.get("duration_business_days", 0) or 0),
            )

    completion_at = max(earliest_finish.values(), default=project_start)
    successors: dict[str, list[str]] = {process_id: [] for process_id in topo_order}
    for successor, predecessors in dependencies.items():
        for predecessor in predecessors:
            successors.setdefault(predecessor, []).append(successor)

    latest_start: dict[str, dt.datetime] = {}
    latest_finish: dict[str, dt.datetime] = {}
    slack: dict[str, int] = {}
    for process_id in reversed(topo_order):
        process = process_by_id[process_id]
        if process.get("finished_at") is not None:
            latest_start[process_id] = earliest_start[process_id]
            latest_finish[process_id] = earliest_finish[process_id]
            slack[process_id] = 0
            continue
        if process.get("started_at") is not None:
            latest_start[process_id] = _as_utc(process["started_at"])
            latest_finish[process_id] = earliest_finish[process_id]
            slack[process_id] = 0
            continue
        finish = min(
            (latest_start[successor] for successor in successors.get(process_id, ())),
            default=completion_at,
        )
        duration_days = int(process.get("duration_business_days", 0) or 0)
        start = subtract_business_days(finish, duration_days)
        latest_start[process_id] = start
        latest_finish[process_id] = finish
        slack[process_id] = count_business_days(earliest_start[process_id], finish) - duration_days

    return {
        process_id: _ProcessCpm(
            process_id=process_id,
            name=str(process_by_id[process_id].get("name", process_id)),
            description=str(process_by_id[process_id].get("description", "")),
            dependencies=dependencies.get(process_id, ()),
            topo_index=index,
            earliest_start_at=earliest_start[process_id],
            earliest_finish_at=earliest_finish[process_id],
            latest_start_at=latest_start[process_id],
            latest_finish_at=latest_finish[process_id],
            slack_business_days=slack[process_id],
        )
        for index, process_id in enumerate(topo_order)
    }


def _process_ready_at(
    *,
    process_id: str,
    dependencies: dict[str, tuple[str, ...]],
    process_rows: dict[str, dict[str, object]],
    completed_process_ids: set[str],
    cpm_by_id: dict[str, _ProcessCpm],
    project_start_at: dt.datetime,
    processes_by_id: dict[str, Mapping[str, object]],
) -> dt.datetime | None:
    dependency_finishes = []
    for dependency in dependencies.get(process_id, ()):
        if dependency not in completed_process_ids:
            if dependency in process_rows:
                return None
            return None
        ends_at = process_rows[dependency].get("ends_at")
        if ends_at is None:
            return None
        dependency_finishes.append(_as_utc(ends_at))
    dependency_finish = max(dependency_finishes, default=project_start_at)
    process = processes_by_id[process_id]
    if process.get("started_at") is not None:
        return _as_utc(process["started_at"])
    constraints = [project_start_at, dependency_finish]
    if process.get("earliest_start_at") is not None:
        constraints.append(_as_utc(process["earliest_start_at"]))
    delay_days = int(process.get("delay_after_dependencies_business_days", 0) or 0)
    if delay_days:
        constraints.append(dependency_finish + dt.timedelta(days=delay_days))
    return max(constraints)


def _requirements_by_process(
    requirements: list[Mapping[str, object]],
) -> dict[str, list[Mapping[str, object]]]:
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for requirement in requirements:
        grouped[str(requirement["process_id"])].append(dict(requirement))
    for process_requirements in grouped.values():
        process_requirements.sort(key=lambda item: str(item["requirement_id"]))
    return grouped


def _requirement_state_key(requirement: Mapping[str, object]) -> tuple[str, str]:
    return (
        str(requirement["process_id"]),
        str(requirement["requirement_id"]),
    )


def _daily_allocation_key(
    requirement: Mapping[str, object],
    resource_id: str,
    local_date: str,
) -> tuple[str, str, str, str]:
    process_id, requirement_id = _requirement_state_key(requirement)
    return process_id, requirement_id, resource_id, local_date


def _bucket_focus_key(
    bucket: _LedgerBucket,
) -> tuple[str, dt.datetime, dt.datetime]:
    return bucket.resource_id, bucket.starts_at, bucket.ends_at


def _allocation_bucket_key(
    allocation: Mapping[str, object],
) -> tuple[str, dt.datetime, dt.datetime]:
    return (
        str(allocation["resource_id"]),
        _as_utc(allocation["starts_at"]),
        _as_utc(allocation["ends_at"]),
    )


def _validate_recurring_capacity_sources(
    *,
    requirements: list[Mapping[str, object]],
    processes_by_id: Mapping[str, Mapping[str, object]],
    roles: list[Mapping[str, object]],
    resources: list[Mapping[str, object]],
    calendars: list[Mapping[str, object]],
) -> None:
    """Fail fast when a requirement has no possible recurring capacity source."""
    active_role_ids = {
        str(role["role_id"]) for role in roles if bool(role.get("active", True))
    }
    calendar_by_id = {
        str(calendar["calendar_id"]): calendar
        for calendar in calendars
        if bool(calendar.get("active", True))
    }
    failures: list[str] = []
    for requirement in requirements:
        process = processes_by_id.get(str(requirement["process_id"]), {})
        if _is_done_process(process):
            continue
        role_id = str(requirement["role_id"])
        key = (
            f"{requirement.get('process_id')}:{requirement.get('requirement_id')}"
        )
        if role_id not in active_role_ids:
            failures.append(f"{key} missing_role")
            continue
        eligible = _eligible_resources(requirement, active_role_ids, resources)
        if not eligible:
            failures.append(f"{key} no_eligible_resource")
            continue
        recurring_sources = [
            resource
            for resource in eligible
            if _resource_has_recurring_capacity(resource, calendar_by_id)
        ]
        if not recurring_sources:
            failures.append(f"{key} no_calendar_capacity")
    if failures:
        raise ValueError(
            "resource schedule cannot be solved: " + "; ".join(failures)
        )


def _resource_has_recurring_capacity(
    resource: Mapping[str, object],
    calendar_by_id: dict[str, Mapping[str, object]],
) -> bool:
    for calendar_id in _resource_calendar_ids(resource):
        calendar = calendar_by_id.get(calendar_id)
        if calendar is None:
            continue
        for window in _sequence(calendar.get("weekly_windows", ())):
            if not isinstance(window, Mapping):
                continue
            if float(window.get("capacity_hours", 0) or 0) > EPSILON:
                return True
    return False


def _resource_calendar_ids(resource: Mapping[str, object]) -> tuple[str, ...]:
    calendar_ids = [str(resource.get("calendar_id"))]
    for override in _sequence(resource.get("calendar_overrides", ())):
        if not isinstance(override, Mapping):
            continue
        calendar_id = override.get("calendar_id")
        if calendar_id is not None:
            calendar_ids.append(str(calendar_id))
    return tuple(dict.fromkeys(calendar_ids))


def _is_done_process(process: Mapping[str, object]) -> bool:
    return (
        str(process.get("explicit_status", "")) == "done"
        or process.get("finished_at") is not None
    )


def _blocked_process_ids(
    blockers: list[Mapping[str, object]],
    as_of: dt.datetime,
) -> set[str]:
    blocked = set()
    for blocker in blockers:
        severity = str(blocker.get("severity", "blocking"))
        if severity != "blocking":
            continue
        created_at = _as_utc(blocker.get("created_at", as_of))
        resolved_value = blocker.get("resolved_at")
        resolved_at = None if resolved_value is None else _as_utc(resolved_value)
        if created_at <= as_of and (resolved_at is None or resolved_at > as_of):
            blocked.add(str(blocker["process_id"]))
    return blocked


def _expand_capacity_buckets(
    *,
    resources: list[Mapping[str, object]],
    calendars: list[Mapping[str, object]],
    horizon_starts_at: dt.datetime,
    horizon_ends_at: dt.datetime,
    planning_granularity: str,
) -> tuple[_LedgerBucket, ...]:
    calendar_by_id = {str(calendar["calendar_id"]): calendar for calendar in calendars}
    output = []
    for resource in resources:
        if not bool(resource.get("active", True)):
            continue
        for calendar, segment_starts_at, segment_ends_at in (
            _resource_calendar_segments(
                resource,
                calendar_by_id,
                horizon_starts_at,
                horizon_ends_at,
            )
        ):
            if calendar is None or not bool(calendar.get("active", True)):
                continue
            timezone = ZoneInfo(str(calendar.get("timezone", "UTC")))
            for bucket in expand_resource_calendar(
                calendar=calendar,
                resource=resource,
                horizon_starts_at=segment_starts_at,
                horizon_ends_at=segment_ends_at,
                planning_granularity=planning_granularity,
            ):
                output.append(
                    _LedgerBucket(
                        resource_id=bucket.resource_id,
                        calendar_id=bucket.calendar_id,
                        starts_at=bucket.starts_at,
                        ends_at=bucket.ends_at,
                        capacity_hours=float(bucket.capacity_hours),
                        remaining_hours=float(bucket.capacity_hours),
                        role_ids=tuple(bucket.role_ids),
                        local_date=bucket.local_date,
                        local_week=bucket.local_week,
                        window_id=_window_id_for_bucket(bucket, calendar, timezone),
                    )
                )
    return tuple(sorted(output, key=_bucket_sort_key))


def _resource_calendar_segments(
    resource: Mapping[str, object],
    calendar_by_id: dict[str, Mapping[str, object]],
    starts_at: dt.datetime,
    ends_at: dt.datetime,
) -> list[tuple[Mapping[str, object] | None, dt.datetime, dt.datetime]]:
    default_calendar = calendar_by_id.get(str(resource.get("calendar_id")))
    segments: list[tuple[Mapping[str, object] | None, dt.datetime, dt.datetime]] = []
    cursor = starts_at
    previous_override_end: dt.datetime | None = None
    for override in sorted(
        (
            _mapping(item, "calendar override")
            for item in _sequence(resource.get("calendar_overrides", ()))
        ),
        key=lambda item: _as_utc(item["starts_at"]),
    ):
        override_starts_at = _as_utc(override["starts_at"])
        override_ends_value = override.get("ends_at")
        override_ends_at = (
            _as_utc(override_ends_value)
            if override_ends_value is not None
            else ends_at
        )
        if override_ends_at <= override_starts_at:
            raise ValueError("resource calendar override ends_at must be after starts_at")
        if previous_override_end is None:
            if segments and override_starts_at < cursor:
                raise ValueError("resource calendar overrides must not overlap")
        elif override_starts_at < previous_override_end:
            raise ValueError("resource calendar overrides must not overlap")
        previous_override_end = override_ends_at
        segment_starts_at = max(override_starts_at, starts_at)
        segment_ends_at = min(override_ends_at, ends_at)
        if segment_ends_at <= starts_at or segment_starts_at >= ends_at:
            continue
        if segment_starts_at > cursor:
            segments.append((default_calendar, cursor, segment_starts_at))
        calendar = calendar_by_id.get(str(override.get("calendar_id")))
        segments.append((calendar, max(cursor, segment_starts_at), segment_ends_at))
        cursor = max(cursor, segment_ends_at)
    if cursor < ends_at:
        segments.append((default_calendar, cursor, ends_at))
    return [
        (calendar, segment_starts_at, segment_ends_at)
        for calendar, segment_starts_at, segment_ends_at in segments
        if segment_ends_at > segment_starts_at
    ]


def _fresh_ledger(
    buckets: tuple[_LedgerBucket, ...],
) -> dict[tuple[str, dt.datetime, dt.datetime], _LedgerBucket]:
    return {
        (bucket.resource_id, bucket.starts_at, bucket.ends_at): _LedgerBucket(
            resource_id=bucket.resource_id,
            calendar_id=bucket.calendar_id,
            starts_at=bucket.starts_at,
            ends_at=bucket.ends_at,
            capacity_hours=bucket.capacity_hours,
            remaining_hours=bucket.capacity_hours,
            role_ids=bucket.role_ids,
            local_date=bucket.local_date,
            local_week=bucket.local_week,
            window_id=bucket.window_id,
        )
        for bucket in buckets
    }


def _eligible_resources(
    requirement: Mapping[str, object],
    active_role_ids: set[str],
    resources: list[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    role_id = str(requirement["role_id"])
    if role_id not in active_role_ids:
        return []
    return [
        resource
        for resource in resources
        if bool(resource.get("active", True))
        and role_id in {str(value) for value in resource.get("role_ids", ())}
    ]


def _window_id_for_bucket(
    bucket: object,
    calendar: Mapping[str, object],
    timezone: ZoneInfo,
) -> str | None:
    local_start = bucket.starts_at.astimezone(timezone).time()
    local_end = bucket.ends_at.astimezone(timezone).time()
    local_date = dt.date.fromisoformat(bucket.local_date)
    for window in _sequence(calendar.get("weekly_windows", ())):
        item = _mapping(window, "weekly window")
        if int(item["weekday"]) != local_date.weekday():
            continue
        starts_at = dt.time.fromisoformat(str(item["start_local_time"]))
        ends_at = dt.time.fromisoformat(str(item["end_local_time"]))
        if starts_at <= local_start and local_end <= ends_at:
            return str(item.get("window_id"))
    return None


def _first_resource_bucket_key(
    resource: Mapping[str, object],
    ledger: dict[tuple[str, dt.datetime, dt.datetime], _LedgerBucket],
    ready_at: dt.datetime,
) -> tuple[object, ...]:
    resource_id = str(resource["resource_id"])
    buckets = [
        bucket
        for bucket in ledger.values()
        if bucket.resource_id == resource_id
        and bucket.ends_at > ready_at
        and bucket.remaining_hours > EPSILON
    ]
    first = min(
        (max(bucket.starts_at, ready_at) for bucket in buckets),
        default=dt.datetime.max.replace(tzinfo=UTC),
    )
    return (first, _resource_cost(resource), resource_id)


def _resource_sort_key(
    resource: Mapping[str, object],
    bucket: _LedgerBucket,
    ready_at: dt.datetime,
) -> tuple[object, ...]:
    return (
        max(bucket.starts_at, ready_at),
        _resource_cost(resource),
        str(resource["resource_id"]),
    )


def _role_remaining_capacity_hours(
    *,
    role_id: str,
    process_id: str,
    resources: list[Mapping[str, object]],
    ledger: dict[tuple[str, dt.datetime, dt.datetime], _LedgerBucket],
    bucket_focus: dict[tuple[str, dt.datetime, dt.datetime], str],
    starts_at: dt.datetime,
) -> float:
    """Measure remaining schedulable capacity for a role from a point in time."""
    eligible_resource_ids = {
        str(resource["resource_id"])
        for resource in resources
        if bool(resource.get("active", True))
        and role_id in {str(value) for value in resource.get("role_ids", ())}
    }
    total = 0.0
    for bucket in ledger.values():
        if (
            bucket.resource_id not in eligible_resource_ids
            or role_id not in bucket.role_ids
            or bucket.ends_at <= starts_at
        ):
            continue
        focused_process_id = bucket_focus.get(_bucket_focus_key(bucket))
        if focused_process_id is not None and focused_process_id != process_id:
            continue
        capacity = _bucket_capacity_between(bucket, starts_at, bucket.ends_at)
        total += min(bucket.remaining_hours, capacity)
    return round(total, 6)


def _resource_cost(resource: Mapping[str, object]) -> Decimal:
    return Decimal(str(resource.get("cost_rate", "0")))


def _bucket_sort_key(bucket: _LedgerBucket) -> tuple[object, ...]:
    return (bucket.starts_at, bucket.ends_at, bucket.resource_id)


def _bucket_resource_sort_key(
    bucket: _LedgerBucket,
    resource_by_id: dict[str, Mapping[str, object]],
) -> tuple[object, ...]:
    return (
        bucket.starts_at,
        bucket.ends_at,
        _resource_cost(resource_by_id[bucket.resource_id]),
        bucket.resource_id,
    )


def _bucket_capacity_after(bucket: _LedgerBucket, ready_at: dt.datetime) -> float:
    return _bucket_capacity_between(bucket, max(bucket.starts_at, ready_at), bucket.ends_at)


def _bucket_capacity_between(
    bucket: _LedgerBucket,
    starts_at: dt.datetime,
    ends_at: dt.datetime,
) -> float:
    if ends_at <= starts_at:
        return 0.0
    bucket_hours = _hours_between(bucket.starts_at, bucket.ends_at)
    if bucket_hours <= 0:
        return 0.0
    overlap_starts_at = max(bucket.starts_at, starts_at)
    overlap_ends_at = min(bucket.ends_at, ends_at)
    if overlap_ends_at <= overlap_starts_at:
        return 0.0
    overlap_hours = _hours_between(overlap_starts_at, overlap_ends_at)
    return bucket.capacity_hours * overlap_hours / bucket_hours


def _finalize_predecessor_unallocated(
    *,
    project_id: str,
    process_id: str,
    cpm_by_id: dict[str, _ProcessCpm],
    requirements: list[Mapping[str, object]],
    process_rows: dict[str, dict[str, object]],
    unallocated: list[dict[str, object]],
) -> None:
    cpm = cpm_by_id[process_id]
    process_rows[process_id] = _process_row(
        cpm=cpm,
        ready_at=None,
        starts_at=None,
        ends_at=None,
        allocation_state="unallocated",
        requirement_ids=[str(item["requirement_id"]) for item in requirements],
    )
    for requirement in requirements:
        unallocated.append(
            {
                "project_id": project_id,
                "process_id": process_id,
                "requirement_id": str(requirement["requirement_id"]),
                "role_id": str(requirement["role_id"]),
                "reason": "predecessor_unallocated",
                "message": "A dependency predecessor did not receive a schedule finish.",
                "diagnostic_message": (
                    "This requirement was not attempted because at least one "
                    "dependency predecessor did not finish in the computed schedule."
                ),
                "required_effort_hours": float(requirement["effort_hours"]),
                "remaining_effort_hours": float(requirement["effort_hours"]),
                "allocated_effort_hours": 0.0,
                "eligible_resource_ids": [],
                "first_feasible_starts_at": None,
                "diagnostics": {
                    "process_ready_at": None,
                    "blocking_reason": "predecessor_unallocated",
                },
            }
        )


def _finalize_blocked(
    *,
    project_id: str,
    process_id: str,
    ready_at: dt.datetime,
    state: str,
    cpm_by_id: dict[str, _ProcessCpm],
    requirements: list[Mapping[str, object]],
    process_rows: dict[str, dict[str, object]],
    unallocated: list[dict[str, object]],
    emit_reason: bool,
) -> None:
    cpm = cpm_by_id[process_id]
    process_rows[process_id] = _process_row(
        cpm=cpm,
        ready_at=ready_at,
        starts_at=None,
        ends_at=None,
        allocation_state=state,
        requirement_ids=[str(item["requirement_id"]) for item in requirements],
    )
    if emit_reason:
        for requirement in requirements:
            unallocated.append(
                {
                    "project_id": project_id,
                    "process_id": process_id,
                    "requirement_id": str(requirement["requirement_id"]),
                    "role_id": str(requirement["role_id"]),
                    "reason": "blocked",
                    "message": "The process is blocked by unresolved blockers.",
                    "diagnostic_message": (
                        "The query used blocked_policy=exclude, so unresolved "
                        "blockers prevented this requirement from being scheduled."
                    ),
                    "required_effort_hours": float(requirement["effort_hours"]),
                    "remaining_effort_hours": float(requirement["effort_hours"]),
                    "allocated_effort_hours": 0.0,
                    "eligible_resource_ids": [],
                    "first_feasible_starts_at": None,
                    "diagnostics": {
                        "process_ready_at": ready_at,
                        "blocking_reason": "blocked_policy_exclude",
                    },
                }
            )


def _finalize_no_requirement_process(
    *,
    process_id: str,
    ready_at: dt.datetime,
    process_rows: dict[str, dict[str, object]],
    cpm_by_id: dict[str, _ProcessCpm],
    processes_by_id: dict[str, Mapping[str, object]],
) -> None:
    del processes_by_id
    ends_at = ready_at
    process_rows[process_id] = _process_row(
        cpm=cpm_by_id[process_id],
        ready_at=ready_at,
        starts_at=ready_at,
        ends_at=ends_at,
        allocation_state="complete",
        requirement_ids=[],
    )


def _finalize_done_process(
    *,
    process_id: str,
    ready_at: dt.datetime,
    process_rows: dict[str, dict[str, object]],
    cpm_by_id: dict[str, _ProcessCpm],
    processes_by_id: dict[str, Mapping[str, object]],
    requirement_ids: list[str],
) -> None:
    process = processes_by_id[process_id]
    starts_at = _as_utc(process["started_at"]) if process.get("started_at") else ready_at
    ends_at = (
        _as_utc(process["finished_at"])
        if process.get("finished_at") is not None
        else max(starts_at, ready_at)
    )
    process_rows[process_id] = _process_row(
        cpm=cpm_by_id[process_id],
        ready_at=ready_at,
        starts_at=starts_at,
        ends_at=ends_at,
        allocation_state="complete",
        requirement_ids=requirement_ids,
    )


def _finalize_process_with_requirements(
    *,
    process_id: str,
    requirements: list[Mapping[str, object]],
    states: dict[tuple[str, str], _RequirementState],
    cpm_by_id: dict[str, _ProcessCpm],
    process_rows: dict[str, dict[str, object]],
) -> None:
    process_states = [
        states[_requirement_state_key(requirement)] for requirement in requirements
    ]
    starts = [state.starts_at for state in process_states if state.starts_at is not None]
    ready_values = [state.ready_at for state in process_states if state.ready_at is not None]
    complete = all(
        state.allocated_hours + EPSILON >= float(state.requirement["effort_hours"])
        for state in process_states
    )
    any_allocated = any(state.allocated_hours > EPSILON for state in process_states)
    ends_at = max((state.ends_at for state in process_states if state.ends_at), default=None)
    if complete:
        allocation_state = "complete"
    elif any_allocated:
        allocation_state = "partial"
        ends_at = None
    else:
        allocation_state = "unallocated"
        ends_at = None
    cpm = cpm_by_id[process_id]
    process_rows[process_id] = _process_row(
        cpm=cpm,
        ready_at=min(ready_values) if ready_values else cpm.earliest_start_at,
        starts_at=min(starts) if starts else None,
        ends_at=ends_at,
        allocation_state=allocation_state,
        requirement_ids=[str(item["requirement_id"]) for item in requirements],
    )


def _process_row(
    *,
    cpm: _ProcessCpm,
    ready_at: dt.datetime | None,
    starts_at: dt.datetime | None,
    ends_at: dt.datetime | None,
    allocation_state: str,
    requirement_ids: list[str],
) -> dict[str, object]:
    delay = 0.0
    if ready_at is not None and starts_at is not None:
        delay = max(0.0, (starts_at - ready_at).total_seconds() / 3600)
    return {
        "process_id": cpm.process_id,
        "name": cpm.name,
        "description": cpm.description,
        "ready_at": ready_at,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "dependency_only_starts_at": cpm.earliest_start_at,
        "dependency_only_ends_at": cpm.earliest_finish_at,
        "resource_delay_hours": round(delay, 6),
        "allocation_state": allocation_state,
        "allocation_diagnostic": None,
        "status": "planned",
        "finished_at": None,
        "requirement_ids": requirement_ids,
    }


def _unallocated_row(
    *,
    project_id: str,
    requirement: Mapping[str, object],
    state: _RequirementState,
    ledger: dict[tuple[str, dt.datetime, dt.datetime], _LedgerBucket],
    horizon_ends_at: dt.datetime,
) -> dict[str, object]:
    remaining = max(0.0, float(requirement["effort_hours"]) - state.allocated_hours)
    reason = state.reason or "horizon_exhausted"
    diagnostics = _requirement_diagnostics(
        requirement=requirement,
        state=state,
        ledger=ledger,
        horizon_ends_at=horizon_ends_at,
    )
    return {
        "project_id": project_id,
        "process_id": str(requirement["process_id"]),
        "requirement_id": str(requirement["requirement_id"]),
        "role_id": str(requirement["role_id"]),
        "reason": reason,
        "message": _reason_message(reason),
        "diagnostic_message": _diagnostic_message(reason, diagnostics),
        "required_effort_hours": round(float(requirement["effort_hours"]), 6),
        "remaining_effort_hours": round(remaining, 6),
        "allocated_effort_hours": round(state.allocated_hours, 6),
        "eligible_resource_ids": list(state.eligible_resource_ids),
        "first_feasible_starts_at": state.first_feasible_starts_at,
        "diagnostics": diagnostics,
    }


def _incomplete_reason(
    requirement: Mapping[str, object],
    state: _RequirementState,
    *,
    ledger: dict[tuple[str, dt.datetime, dt.datetime], _LedgerBucket],
    horizon_ends_at: dt.datetime,
) -> str:
    existing = requirement.get("_state")
    if isinstance(existing, _RequirementState) and existing.reason in {
        "missing_role",
        "no_eligible_resource",
        "no_calendar_capacity",
        "contiguous_window_unavailable",
    }:
        return existing.reason
    diagnostics = _requirement_diagnostics(
        requirement=requirement,
        state=state,
        ledger=ledger,
        horizon_ends_at=horizon_ends_at,
    )
    candidate_capacity = float(diagnostics["candidate_capacity_hours"])
    remaining_capacity = float(diagnostics["remaining_candidate_capacity_hours"])
    if candidate_capacity <= EPSILON:
        return "no_calendar_capacity"
    if str(requirement.get("allocation_policy", "split_allowed")) == "contiguous":
        return "contiguous_window_unavailable"
    if remaining_capacity <= EPSILON:
        return "resource_capacity_exhausted"
    return "horizon_exhausted"


def _reason_message(reason: str) -> str:
    return {
        "missing_role": "The requirement references no active role.",
        "no_eligible_resource": "No active resource can fill the required role.",
        "no_calendar_capacity": (
            "Eligible resources have no calendar capacity after the process is ready "
            "and before the horizon ends."
        ),
        "resource_capacity_exhausted": (
            "Eligible resources exist, but their capacity in the horizon was exhausted."
        ),
        "blocked": "The process is blocked by unresolved blockers.",
        "predecessor_unallocated": "A dependency predecessor did not finish.",
        "horizon_exhausted": "The schedule horizon ended before effort was fulfilled.",
        "contiguous_window_unavailable": "No single resource has a contiguous window.",
        "iteration_not_converged": "The final iteration changed before convergence.",
    }.get(reason, reason)


def _raise_for_permanent_capacity_failures(
    unallocated: list[dict[str, object]],
) -> None:
    permanent_reasons = {
        "missing_role",
        "no_eligible_resource",
        "contiguous_window_unavailable",
        "blocked",
    }
    failures = [
        item
        for item in unallocated
        if str(item.get("reason")) in permanent_reasons
    ]
    if not failures:
        return
    details = "; ".join(
        (
            f"{item.get('process_id')}:{item.get('requirement_id')} "
            f"{item.get('reason')}"
        )
        for item in failures
    )
    raise ValueError(f"resource schedule cannot be solved: {details}")


def _next_capacity_search_end(
    horizon_starts_at: dt.datetime,
    horizon_ends_at: dt.datetime,
) -> dt.datetime:
    current_days = max(1, math.ceil((horizon_ends_at - horizon_starts_at).days))
    next_days = max(current_days * 2, current_days + 7)
    return horizon_starts_at + dt.timedelta(days=next_days)


def _requirement_diagnostics(
    *,
    requirement: Mapping[str, object],
    state: _RequirementState,
    ledger: dict[tuple[str, dt.datetime, dt.datetime], _LedgerBucket],
    horizon_ends_at: dt.datetime,
) -> dict[str, object]:
    ready_at = state.ready_at
    role_id = str(requirement["role_id"])
    eligible_resource_ids = tuple(state.eligible_resource_ids)
    required_effort = float(requirement["effort_hours"])
    resource_rows: dict[str, dict[str, object]] = {}
    candidate_capacity = 0.0
    remaining_capacity = 0.0
    first_candidate_start: dt.datetime | None = None
    resources_with_remaining: set[str] = set()

    if ready_at is not None:
        for bucket in sorted(ledger.values(), key=_bucket_sort_key):
            if bucket.resource_id not in eligible_resource_ids:
                continue
            if role_id not in bucket.role_ids:
                continue
            if bucket.ends_at <= ready_at or bucket.starts_at >= horizon_ends_at:
                continue
            starts_at = max(bucket.starts_at, ready_at)
            ends_at = min(bucket.ends_at, horizon_ends_at)
            capacity = _bucket_capacity_between(bucket, starts_at, ends_at)
            if capacity <= EPSILON:
                continue
            remaining = min(bucket.remaining_hours, capacity)
            candidate_capacity += capacity
            remaining_capacity += remaining
            if first_candidate_start is None or starts_at < first_candidate_start:
                first_candidate_start = starts_at
            row = resource_rows.setdefault(
                bucket.resource_id,
                {
                    "resource_id": bucket.resource_id,
                    "candidate_capacity_hours": 0.0,
                    "remaining_capacity_hours": 0.0,
                    "first_candidate_starts_at": starts_at,
                },
            )
            row["candidate_capacity_hours"] = round(
                float(row["candidate_capacity_hours"]) + capacity,
                6,
            )
            row["remaining_capacity_hours"] = round(
                float(row["remaining_capacity_hours"]) + remaining,
                6,
            )
            if starts_at < row["first_candidate_starts_at"]:
                row["first_candidate_starts_at"] = starts_at
            if remaining > EPSILON:
                resources_with_remaining.add(bucket.resource_id)

    remaining_effort = max(0.0, required_effort - state.allocated_hours)
    return {
        "process_ready_at": ready_at,
        "horizon_ends_at": horizon_ends_at,
        "allocation_policy": str(requirement.get("allocation_policy", "split_allowed")),
        "required_effort_hours": round(required_effort, 6),
        "allocated_effort_hours": round(state.allocated_hours, 6),
        "remaining_effort_hours": round(remaining_effort, 6),
        "eligible_resource_count": len(eligible_resource_ids),
        "eligible_resource_ids": list(eligible_resource_ids),
        "candidate_capacity_hours": round(candidate_capacity, 6),
        "remaining_candidate_capacity_hours": round(remaining_capacity, 6),
        "first_candidate_starts_at": first_candidate_start,
        "resources_with_remaining_capacity": sorted(resources_with_remaining),
        "resource_diagnostics": [
            resource_rows[resource_id]
            for resource_id in sorted(resource_rows)
        ],
    }


def _diagnostic_message(reason: str, diagnostics: Mapping[str, object]) -> str:
    ready_at = diagnostics.get("process_ready_at")
    horizon_ends_at = diagnostics.get("horizon_ends_at")
    eligible_count = int(diagnostics.get("eligible_resource_count", 0) or 0)
    candidate_capacity = float(diagnostics.get("candidate_capacity_hours", 0) or 0)
    remaining_capacity = float(
        diagnostics.get("remaining_candidate_capacity_hours", 0) or 0
    )
    remaining_effort = float(diagnostics.get("remaining_effort_hours", 0) or 0)

    if reason == "missing_role":
        return "The required role is missing or inactive, so no resource can be matched."
    if reason == "no_eligible_resource":
        return "The role exists, but no active resource currently carries that role."
    if reason == "no_calendar_capacity":
        return (
            f"{eligible_count} eligible resource(s) exist, but they provide 0 schedulable "
            f"hours after ready_at={_diagnostic_datetime(ready_at)} and before "
            f"horizon_ends_at={_diagnostic_datetime(horizon_ends_at)}."
        )
    if reason == "resource_capacity_exhausted":
        return (
            f"{eligible_count} eligible resource(s) exist with "
            f"{candidate_capacity:g} candidate hour(s), but remaining schedulable "
            f"capacity is {remaining_capacity:g} hour(s) after prior allocations."
        )
    if reason == "horizon_exhausted":
        return (
            f"{remaining_effort:g} effort hour(s) remain unfilled before "
            f"horizon_ends_at={_diagnostic_datetime(horizon_ends_at)}; "
            f"{remaining_capacity:g} eligible capacity hour(s) remain available."
        )
    if reason == "contiguous_window_unavailable":
        return (
            f"{eligible_count} eligible resource(s) exist, but no single resource has "
            "a contiguous remaining window large enough for this requirement."
        )
    return _reason_message(reason)


def _diagnostic_datetime(value: object) -> str:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if value is None:
        return "none"
    return str(value)


def _attach_allocation_diagnostics(
    *,
    rows: list[dict[str, object]],
    unallocated: list[dict[str, object]],
) -> None:
    messages_by_process: dict[str, list[str]] = defaultdict(list)
    for item in unallocated:
        process_id = str(item["process_id"])
        requirement_id = str(item.get("requirement_id", "requirement"))
        message = str(item.get("diagnostic_message") or item.get("message") or "")
        if not message:
            continue
        messages_by_process[process_id].append(f"{requirement_id}: {message}")
    for row in rows:
        messages = messages_by_process.get(str(row["process_id"]), [])
        row["allocation_diagnostic"] = " | ".join(messages) if messages else None


def _initial_iteration_state(
    *,
    processes: list[Mapping[str, object]],
    cpm_by_id: dict[str, _ProcessCpm],
    requirements_by_process: dict[str, list[Mapping[str, object]]],
) -> dict[str, object]:
    rows = []
    for process in processes:
        process_id = str(process["process_id"])
        cpm = cpm_by_id[process_id]
        rows.append(
            _process_row(
                cpm=cpm,
                ready_at=cpm.earliest_start_at,
                starts_at=cpm.earliest_start_at,
                ends_at=cpm.earliest_finish_at,
                allocation_state="complete",
                requirement_ids=[
                    str(item["requirement_id"])
                    for item in requirements_by_process.get(process_id, [])
                ],
            )
        )
    return {"processes": rows, "unallocated_requirements": [], "allocation_slices": []}


def _add_iteration_not_converged_reasons(
    *,
    project_id: str,
    unallocated: list[dict[str, object]],
    requirements_by_process: dict[str, list[Mapping[str, object]]],
    dependencies: dict[str, tuple[str, ...]],
    changed_process_ids: object,
) -> list[dict[str, object]]:
    output = list(unallocated)
    existing = {
        (str(item["process_id"]), str(item["requirement_id"]), str(item["reason"]))
        for item in output
    }
    for process_id in changed_process_ids:
        if not dependencies.get(str(process_id)):
            continue
        for requirement in requirements_by_process.get(str(process_id), []):
            key = (str(process_id), str(requirement["requirement_id"]), "iteration_not_converged")
            if key in existing:
                continue
            output.append(
                {
                    "project_id": project_id,
                    "process_id": str(process_id),
                    "requirement_id": str(requirement["requirement_id"]),
                    "role_id": str(requirement["role_id"]),
                    "reason": "iteration_not_converged",
                    "message": _reason_message("iteration_not_converged"),
                    "diagnostic_message": (
                        "The resource-aware schedule changed in the final iteration, "
                        "so this requirement may need a larger max_iterations value."
                    ),
                    "required_effort_hours": float(requirement["effort_hours"]),
                    "remaining_effort_hours": 0.0,
                    "allocated_effort_hours": float(requirement["effort_hours"]),
                    "eligible_resource_ids": [],
                    "first_feasible_starts_at": None,
                    "diagnostics": {
                        "blocking_reason": "iteration_not_converged",
                    },
                }
            )
    return sorted(
        output,
        key=lambda item: (item["process_id"], item["requirement_id"], item["reason"]),
    )


def _resource_critical_path(
    *,
    processes: list[dict[str, object]],
    dependencies: dict[str, tuple[str, ...]],
    cpm_by_id: dict[str, _ProcessCpm],
    tolerance_hours: float,
) -> list[str]:
    complete_rows = [row for row in processes if row.get("ends_at") is not None]
    if not complete_rows:
        return []
    latest_finish = max(_as_utc(row["ends_at"]) for row in complete_rows)
    tolerance = dt.timedelta(hours=tolerance_hours + EPSILON)
    terminal_candidates = [
        row
        for row in complete_rows
        if latest_finish - _as_utc(row["ends_at"]) <= tolerance
    ]
    terminal = sorted(
        terminal_candidates,
        key=lambda row: (
            cpm_by_id[str(row["process_id"])].topo_index,
            str(row["process_id"]),
        ),
    )[0]
    path = [str(terminal["process_id"])]
    row_by_id = {str(row["process_id"]): row for row in processes}
    current = str(terminal["process_id"])
    while True:
        ready_at = row_by_id[current].get("ready_at")
        if ready_at is None:
            break
        candidates = []
        for predecessor in dependencies.get(current, ()):
            predecessor_row = row_by_id.get(predecessor)
            if predecessor_row is None or predecessor_row.get("ends_at") is None:
                continue
            delta = abs(
                (_as_utc(predecessor_row["ends_at"]) - _as_utc(ready_at)).total_seconds()
                / 3600
            )
            if delta <= tolerance_hours + EPSILON:
                cpm = cpm_by_id[predecessor]
                candidates.append((cpm.topo_index, predecessor))
        if not candidates:
            break
        _, current = sorted(candidates)[0]
        path.append(current)
    return list(reversed(path))


def _attach_resource_schedule_windows(
    *,
    rows: list[dict[str, object]],
    allocation_slices: list[dict[str, object]],
    processes: list[Mapping[str, object]],
    dependencies: dict[str, tuple[str, ...]],
    topo_order: tuple[str, ...],
) -> None:
    """Attach process-level resource-aware ES/EF/LS/LF windows to rows."""
    row_by_id = {str(row["process_id"]): row for row in rows}
    process_by_id = {str(process["process_id"]): process for process in processes}
    active_hours_by_process = _active_allocation_hours_by_process(allocation_slices)
    complete_ids = [
        process_id
        for process_id, row in row_by_id.items()
        if row.get("starts_at") is not None and row.get("ends_at") is not None
    ]
    for row in rows:
        row["resource_es_at"] = row.get("starts_at")
        row["resource_ef_at"] = row.get("ends_at")
        row["resource_ls_at"] = None
        row["resource_lf_at"] = None
        row["resource_slack_hours"] = None
        row["inferred_duration_hours"] = None
    if not complete_ids:
        return

    successors: dict[str, list[str]] = {process_id: [] for process_id in row_by_id}
    for successor_id, predecessor_ids in dependencies.items():
        if successor_id not in row_by_id:
            continue
        for predecessor_id in predecessor_ids:
            if predecessor_id in row_by_id:
                successors.setdefault(predecessor_id, []).append(successor_id)

    completion_at = max(_as_utc(row_by_id[process_id]["ends_at"]) for process_id in complete_ids)
    latest_start: dict[str, dt.datetime] = {}
    for process_id in reversed(topo_order):
        row = row_by_id.get(process_id)
        if row is None or process_id not in complete_ids:
            continue
        starts_at = _as_utc(row["starts_at"])
        ends_at = _as_utc(row["ends_at"])
        elapsed_duration = max(ends_at - starts_at, dt.timedelta())
        active_duration_hours = active_hours_by_process.get(process_id)
        if active_duration_hours is None:
            active_duration_hours = elapsed_duration.total_seconds() / 3600
        row["inferred_duration_hours"] = round(active_duration_hours, 6)
        process = process_by_id.get(process_id, {})
        if process.get("finished_at") is not None:
            ls_at = starts_at
            lf_at = ends_at
        elif process.get("started_at") is not None:
            ls_at = _as_utc(process["started_at"])
            lf_at = ls_at + elapsed_duration
        else:
            successor_starts = [
                latest_start[successor_id]
                for successor_id in successors.get(process_id, ())
                if successor_id in latest_start
            ]
            lf_at = min(successor_starts, default=completion_at)
            ls_at = lf_at - elapsed_duration
        latest_start[process_id] = ls_at
        row["resource_ls_at"] = ls_at
        row["resource_lf_at"] = lf_at
        row["resource_slack_hours"] = round(
            (ls_at - starts_at).total_seconds() / 3600,
            6,
        )


def _active_allocation_hours_by_process(
    allocation_slices: list[dict[str, object]],
) -> dict[str, float]:
    intervals_by_process: dict[str, list[tuple[dt.datetime, dt.datetime]]] = (
        defaultdict(list)
    )
    for allocation in allocation_slices:
        starts_at = _as_utc(allocation["starts_at"])
        ends_at = _as_utc(allocation["ends_at"])
        if ends_at <= starts_at:
            continue
        intervals_by_process[str(allocation["process_id"])].append((starts_at, ends_at))

    output = {}
    for process_id, intervals in intervals_by_process.items():
        merged: list[list[dt.datetime]] = []
        for starts_at, ends_at in sorted(intervals):
            if not merged or starts_at > merged[-1][1]:
                merged.append([starts_at, ends_at])
                continue
            if ends_at > merged[-1][1]:
                merged[-1][1] = ends_at
        output[process_id] = sum(
            (ends_at - starts_at).total_seconds() / 3600
            for starts_at, ends_at in merged
        )
    return output


def _rows_by_process(state: Mapping[str, object]) -> dict[str, dict[str, object]]:
    return {
        str(row["process_id"]): dict(row)
        for row in _sequence(state.get("processes", ()))
        if isinstance(row, Mapping)
    }


def _reason_map(state: Mapping[str, object]) -> dict[tuple[str, str], str]:
    output = {}
    for item in _sequence(state.get("unallocated_requirements", ())):
        if not isinstance(item, Mapping):
            continue
        output[(str(item["process_id"]), str(item.get("requirement_id")))] = str(
            item["reason"]
        )
    return output


def _datetime_changed(before: object, after: object, tolerance_hours: float) -> bool:
    if before is None or after is None:
        return before is not after
    delta = abs((_as_utc(after) - _as_utc(before)).total_seconds()) / 3600
    return delta > tolerance_hours + EPSILON


def _fingerprint_value(value: object) -> object:
    if isinstance(value, dt.datetime):
        return _as_utc(value).isoformat()
    return value


def _hours_between(starts_at: dt.datetime, ends_at: dt.datetime) -> float:
    return (ends_at - starts_at).total_seconds() / 3600


def _json_safe(value: object) -> object:
    if isinstance(value, dt.datetime):
        return _as_utc(value).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return value


def _as_utc(value: object) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return require_aware(value).astimezone(UTC)
    if isinstance(value, str):
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return require_aware(parsed).astimezone(UTC)
    raise ValueError(f"expected timezone-aware datetime, got {value!r}")


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    raise ValueError(f"{name} must be an object")


def _validate_integral_effort_hours(
    requirements: list[Mapping[str, object]],
) -> None:
    for requirement in requirements:
        effort = float(requirement.get("effort_hours", 0))
        if effort <= 0 or not math.isclose(effort, round(effort), abs_tol=EPSILON):
            requirement_id = requirement.get("requirement_id", "<unknown>")
            raise ValueError(
                "role requirement effort_hours must be a positive whole number "
                f"for {requirement_id!r}"
            )


def _sequence(value: object) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, list | tuple):
        return tuple(value)
    raise ValueError("expected a sequence")

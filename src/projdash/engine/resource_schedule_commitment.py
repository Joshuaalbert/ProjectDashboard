"""Commitment-window heuristic and MCTS backend for resource scheduling."""

from __future__ import annotations

import copy
import datetime as dt
import os
import time
from collections import defaultdict
from collections.abc import Mapping
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass

from projdash.engine import resource_schedule as greedy
from rcpsp_mathprog_package.rcpsp_commitment_mcts import (
    CommitmentMCTSOptions,
    CommitmentMCTSPlanner,
)
from rcpsp_mathprog_package.rcpsp_heuristic import (
    HeuristicInfeasibleError,
    HeuristicPlanningProblem,
)

COMMITMENT_ROLLOUTS_PER_ACTION = 10


@dataclass(frozen=True, slots=True)
class _TimeBucket:
    bucket_id: int
    starts_at: dt.datetime
    ends_at: dt.datetime


@dataclass(frozen=True, slots=True)
class _PreparedCommitmentProblem:
    project_id: str
    project_start_at: dt.datetime
    as_of: dt.datetime
    now: dt.datetime
    horizon_starts_at: dt.datetime
    horizon_ends_at: dt.datetime
    planning_granularity: str
    options: Mapping[str, object]
    processes: list[Mapping[str, object]]
    requirements: list[Mapping[str, object]]
    roles: list[Mapping[str, object]]
    resources: list[Mapping[str, object]]
    calendars: list[Mapping[str, object]]
    dependencies: dict[str, tuple[str, ...]]
    topo_order: tuple[str, ...]
    schedulable_topo_order: tuple[str, ...]
    blocked_policy: str
    blocked_process_ids: set[str]
    excluded_process_ids: set[str]
    cpm_by_id: dict[str, greedy._ProcessCpm]
    requirements_by_process: dict[str, list[Mapping[str, object]]]
    resource_by_id: dict[str, Mapping[str, object]]
    expanded_buckets: tuple[greedy._LedgerBucket, ...]
    fixed_allocation_slices: list[dict[str, object]]
    fixed_role_completions: list[dict[str, object]]
    time_buckets: tuple[_TimeBucket, ...]
    bucket_by_id: dict[int, _TimeBucket]
    ledger_by_resource_time: dict[tuple[str, int], greedy._LedgerBucket]
    requirement_by_role_process: dict[tuple[str, str], Mapping[str, object]]
    problem: HeuristicPlanningProblem


def compute_commitment_resource_schedule(input_data: Mapping[str, object]) -> dict[str, object]:
    """Compute a ProjDash schedule with the commitment-window planner."""
    started_at = time.perf_counter()
    options = dict(greedy._mapping(input_data.get("options", {}), "options"))
    backend = str(options.get("resource_schedule_backend", "mcts"))
    if backend != "mcts":
        raise ValueError("commitment scheduler only supports resource_schedule_backend='mcts'")

    unsupported_reason = _unsupported_reason(input_data, options)
    if unsupported_reason is not None:
        raise ValueError(
            "commitment-window MCTS cannot plan this input: "
            f"{unsupported_reason}. Use the greedy backend explicitly for "
            "inputs that require greedy-only scheduling semantics."
        )

    prepared = _prepare_commitment_problem(input_data)
    planner_options = CommitmentMCTSOptions(
        use_mcts=True,
        complete_rollouts_per_action=COMMITMENT_ROLLOUTS_PER_ACTION,
        rollout_transition_limit=int(
            options.get("resource_schedule_mcts_transition_limit", 5000) or 5000
        ),
        random_seed=1,
        allow_idle_when_work_legal=False,
    )
    planner = CommitmentMCTSPlanner(planner_options)
    try:
        result = planner.solve(prepared.problem)
    except HeuristicInfeasibleError as exc:
        next_horizon_ends_at = greedy._next_capacity_search_end(
            prepared.horizon_starts_at,
            prepared.horizon_ends_at,
        )
        max_capacity_search_days = int(
            prepared.options.get("capacity_search_max_days", 36500)
        )
        if (next_horizon_ends_at - prepared.horizon_starts_at).days > max_capacity_search_days:
            raise ValueError(
                "resource schedule did not finish after expanding recurring "
                "calendar capacity over the internal safety limit"
            ) from exc
        extended_input = dict(input_data)
        extended_options = dict(prepared.options)
        extended_options["horizon_ends_at"] = next_horizon_ends_at
        extended_options["_capacity_search_attempt"] = (
            int(extended_options.get("_capacity_search_attempt", 0)) + 1
        )
        extended_input["options"] = extended_options
        return compute_commitment_resource_schedule(extended_input)

    elapsed = round(time.perf_counter() - started_at, 6)
    schedule = _schedule_from_result(
        prepared=prepared,
        result=result,
        stats=planner._stats,
        elapsed_seconds=elapsed,
        use_mcts=True,
    )
    if bool(options.get("include_resource_sensitivity", False)):
        _attach_resource_sensitivity(
            schedule=schedule,
            input_data=input_data,
            baseline_finish_at=_schedule_finish(schedule),
        )
    return schedule


def _unsupported_reason(
    input_data: Mapping[str, object],
    options: Mapping[str, object],
) -> str | None:
    processes = [
        greedy._mapping(item, "process")
        for item in greedy._sequence(input_data["processes"])
    ]
    delayed = [
        str(process["process_id"])
        for process in processes
        if int(process.get("delay_after_dependencies_business_days", 0) or 0)
    ]
    if delayed:
        return "dependency delay constraints are still handled by the greedy backend"

    requirements = [
        greedy._mapping(item, "role requirement")
        for item in greedy._sequence(input_data.get("role_requirements", ()))
    ]
    unsupported_requirements = []
    for requirement in requirements:
        if str(requirement.get("allocation_policy", "split_allowed")) != "split_allowed":
            unsupported_requirements.append(str(requirement["requirement_id"]))
        if requirement.get("min_allocation_hours_per_day") is not None:
            unsupported_requirements.append(str(requirement["requirement_id"]))
        if requirement.get("max_allocation_hours_per_day") is not None:
            unsupported_requirements.append(str(requirement["requirement_id"]))
    if unsupported_requirements:
        return (
            "contiguous and daily min/max allocation constraints are still "
            "handled by the greedy backend: "
            + ", ".join(sorted(set(unsupported_requirements)))
        )
    return None


def _blocked_process_closure(
    *,
    blocked_process_ids: set[str],
    dependencies: Mapping[str, tuple[str, ...]],
) -> set[str]:
    """Return blocked processes plus successors that cannot legally start."""
    excluded = set(blocked_process_ids)
    changed = True
    while changed:
        changed = False
        for process_id, predecessor_ids in dependencies.items():
            if process_id in excluded:
                continue
            if any(predecessor_id in excluded for predecessor_id in predecessor_ids):
                excluded.add(process_id)
                changed = True
    return excluded


def _prepare_commitment_problem(input_data: Mapping[str, object]) -> _PreparedCommitmentProblem:
    options = dict(greedy._mapping(input_data.get("options", {}), "options"))
    project_id = str(input_data["project_id"])
    project_start_at = greedy._as_utc(input_data["project_start_at"])
    as_of = greedy._as_utc(input_data["as_of"])
    now = greedy._as_utc(input_data["now"])
    horizon_starts_at = greedy._as_utc(options["horizon_starts_at"])
    horizon_ends_at = greedy._as_utc(options["horizon_ends_at"])
    planning_granularity = str(options.get("planning_granularity", "hour"))

    processes = [
        greedy._mapping(item, "process")
        for item in greedy._sequence(input_data["processes"])
    ]
    requirements = [
        greedy._mapping(item, "role requirement")
        for item in greedy._sequence(input_data.get("role_requirements", ()))
    ]
    roles = [
        greedy._mapping(item, "role")
        for item in greedy._sequence(input_data.get("roles", ()))
    ]
    resources = [
        greedy._mapping(item, "resource")
        for item in greedy._sequence(input_data.get("resources", ()))
    ]
    calendars = [
        greedy._mapping(item, "calendar")
        for item in greedy._sequence(input_data.get("calendars", ()))
    ]
    dependencies = greedy._collect_dependencies(input_data, processes)
    topo_order = greedy._topological_order(processes, dependencies)
    blockers = [
        greedy._mapping(item, "blocker")
        for item in greedy._sequence(input_data.get("blockers", ()))
    ]
    fixed_allocation_slices = greedy._fixed_allocation_slices(
        input_data.get("fixed_allocation_slices", ()),
    )
    capacity_holds = greedy._capacity_holds(input_data.get("capacity_holds", ()))
    fixed_role_completions = greedy._fixed_role_completions(
        input_data.get("fixed_role_completions", ()),
    )
    # Blocker metadata is not a planning primitive. It is represented to the
    # planner only by normal process nodes and dependency edges.
    _ = blockers
    blocked_policy = "include_normally"
    active_blocked_process_ids: set[str] = set()
    excluded_process_ids = _blocked_process_closure(
        blocked_process_ids=active_blocked_process_ids,
        dependencies=dependencies,
    )
    schedulable_process_ids = {
        str(process["process_id"])
        for process in processes
        if str(process["process_id"]) not in excluded_process_ids
    }
    fixed_requirement_keys = {
        (str(completion["process_id"]), str(completion["requirement_id"]))
        for completion in fixed_role_completions
    }
    fixed_finish_by_process: dict[str, list[dt.datetime]] = defaultdict(list)
    for completion in fixed_role_completions:
        fixed_finish_by_process[str(completion["process_id"])].append(
            greedy._as_utc(completion["finish_at"])
        )
    finished_process_ids = {
        str(process["process_id"])
        for process in processes
        if _process_is_finished(process)
    }
    schedulable_topo_order = tuple(
        process_id for process_id in topo_order if process_id in schedulable_process_ids
    )
    schedulable_requirements = [
        requirement
        for requirement in requirements
        if str(requirement["process_id"]) in schedulable_process_ids
        and str(requirement["process_id"]) not in finished_process_ids
        and (
            str(requirement["process_id"]),
            str(requirement["requirement_id"]),
        )
        not in fixed_requirement_keys
    ]
    cpm_by_id = greedy._compute_cpm(processes, dependencies, project_start_at, topo_order)
    requirements_by_process = greedy._requirements_by_process(requirements)
    active_role_ids = {
        str(role["role_id"]) for role in roles if bool(role.get("active", True))
    }
    greedy._validate_recurring_capacity_sources(
        requirements=requirements,
        processes_by_id={str(process["process_id"]): process for process in processes},
        roles=roles,
        resources=resources,
        calendars=calendars,
    )
    expanded_buckets = greedy._expand_capacity_buckets(
        resources=resources,
        calendars=calendars,
        horizon_starts_at=horizon_starts_at,
        horizon_ends_at=horizon_ends_at,
        planning_granularity=planning_granularity,
    )
    greedy._apply_fixed_allocation_slices_to_buckets(
        expanded_buckets,
        fixed_allocation_slices,
    )
    greedy._apply_capacity_holds_to_buckets(expanded_buckets, capacity_holds)
    time_buckets = _time_buckets_from_ledger(expanded_buckets)
    bucket_by_id = {bucket.bucket_id: bucket for bucket in time_buckets}
    bucket_id_by_interval = {
        (bucket.starts_at, bucket.ends_at): bucket.bucket_id
        for bucket in time_buckets
    }
    ledger_by_resource_time = {
        (bucket.resource_id, bucket_id_by_interval[(bucket.starts_at, bucket.ends_at)]): bucket
        for bucket in expanded_buckets
    }

    use_requirement_role_tokens = _requires_requirement_role_tokens(schedulable_requirements)
    requirement_by_role_process: dict[tuple[str, str], Mapping[str, object]] = {}
    planner_roles = (
        []
        if use_requirement_role_tokens
        else [
            str(role["role_id"])
            for role in roles
            if bool(role.get("active", True))
        ]
    )
    requirements_map: dict[tuple[str, str], float] = {}
    preferred_resources: dict[tuple[str, str], tuple[str, ...]] = {}
    for requirement in schedulable_requirements:
        original_role_id = str(requirement["role_id"])
        if original_role_id not in active_role_ids:
            continue
        process_id = str(requirement["process_id"])
        planner_role = (
            str(requirement["requirement_id"])
            if use_requirement_role_tokens
            else original_role_id
        )
        if planner_role not in planner_roles:
            planner_roles.append(planner_role)
        requirement_by_role_process[(planner_role, process_id)] = requirement
        requirements_map[(planner_role, process_id)] = float(requirement["effort_hours"])
        preferred_resource_ids = tuple(
            str(value) for value in requirement.get("preferred_resource_ids", ()) or ()
        )
        if preferred_resource_ids:
            preferred_resources[(planner_role, process_id)] = preferred_resource_ids

    availability: dict[tuple[str, str, int], float] = {}
    resource_capacity: dict[tuple[str, int], float] = {}
    for (resource_id, bucket_id), bucket in ledger_by_resource_time.items():
        duration_hours = max(
            greedy.EPSILON,
            (bucket.ends_at - bucket.starts_at).total_seconds() / 3600,
        )
        capacity_fraction = float(bucket.remaining_hours) / duration_hours
        resource_capacity[(resource_id, bucket_id)] = capacity_fraction
        for requirement in schedulable_requirements:
            original_role_id = str(requirement["role_id"])
            if original_role_id not in active_role_ids or original_role_id not in bucket.role_ids:
                continue
            planner_role = (
                str(requirement["requirement_id"])
                if use_requirement_role_tokens
                else original_role_id
            )
            if _resource_eligible_for_requirement(resource_id, requirement, resources):
                availability[(resource_id, planner_role, bucket_id)] = capacity_fraction

    base_at = time_buckets[0].starts_at if time_buckets else horizon_starts_at
    bucket_hours = {
        bucket.bucket_id: (bucket.ends_at - bucket.starts_at).total_seconds() / 3600
        for bucket in time_buckets
    }
    bucket_start = {
        bucket.bucket_id: (bucket.starts_at - base_at).total_seconds() / 3600
        for bucket in time_buckets
    }
    bucket_end = {
        bucket.bucket_id: (bucket.ends_at - base_at).total_seconds() / 3600
        for bucket in time_buckets
    }
    earliest_start = {
        str(process["process_id"]): (
            _process_planner_start(project_start_at, process) - base_at
        ).total_seconds()
        / 3600
        for process in processes
        if str(process["process_id"]) in schedulable_process_ids
    }
    earliest_finish = {
        process_id: (max(finishes) - base_at).total_seconds() / 3600
        for process_id, finishes in fixed_finish_by_process.items()
        if process_id in schedulable_process_ids
    }
    problem = HeuristicPlanningProblem(
        roles=tuple(planner_roles),
        resources=tuple(str(resource["resource_id"]) for resource in resources),
        processes=tuple(schedulable_topo_order),
        buckets=tuple(bucket.bucket_id for bucket in time_buckets),
        requirements=requirements_map,
        availability=availability,
        predecessors={
            process_id: {
                dependency
                for dependency in dependencies.get(process_id, ())
                if dependency in schedulable_process_ids
            }
            for process_id in schedulable_topo_order
        },
        bucket_hours=bucket_hours,
        resource_capacity=resource_capacity,
        bucket_start=bucket_start,
        bucket_end=bucket_end,
        earliest_start=earliest_start,
        earliest_finish=earliest_finish,
        allowed_resources=_allowed_resources_by_process(
            requirements=schedulable_requirements,
            resources=resources,
        ),
        preferred_resources=preferred_resources,
    )
    return _PreparedCommitmentProblem(
        project_id=project_id,
        project_start_at=project_start_at,
        as_of=as_of,
        now=now,
        horizon_starts_at=horizon_starts_at,
        horizon_ends_at=horizon_ends_at,
        planning_granularity=planning_granularity,
        options=options,
        processes=processes,
        requirements=requirements,
        roles=roles,
        resources=resources,
        calendars=calendars,
        dependencies=dependencies,
        topo_order=topo_order,
        schedulable_topo_order=schedulable_topo_order,
        blocked_policy=blocked_policy,
        blocked_process_ids=active_blocked_process_ids,
        excluded_process_ids=excluded_process_ids,
        cpm_by_id=cpm_by_id,
        requirements_by_process=requirements_by_process,
        resource_by_id={str(resource["resource_id"]): resource for resource in resources},
        expanded_buckets=expanded_buckets,
        fixed_allocation_slices=fixed_allocation_slices,
        fixed_role_completions=fixed_role_completions,
        time_buckets=time_buckets,
        bucket_by_id=bucket_by_id,
        ledger_by_resource_time=ledger_by_resource_time,
        requirement_by_role_process=requirement_by_role_process,
        problem=problem,
    )


def _time_buckets_from_ledger(
    expanded_buckets: tuple[greedy._LedgerBucket, ...],
) -> tuple[_TimeBucket, ...]:
    intervals = sorted({(bucket.starts_at, bucket.ends_at) for bucket in expanded_buckets})
    return tuple(
        _TimeBucket(bucket_id=index, starts_at=starts_at, ends_at=ends_at)
        for index, (starts_at, ends_at) in enumerate(intervals, start=1)
    )


def _requires_requirement_role_tokens(requirements: list[Mapping[str, object]]) -> bool:
    seen: set[tuple[str, str]] = set()
    for requirement in requirements:
        key = (str(requirement["role_id"]), str(requirement["process_id"]))
        if key in seen:
            return True
        seen.add(key)
        if requirement.get("preferred_resource_ids"):
            return True
    return False


def _resource_eligible_for_requirement(
    resource_id: str,
    requirement: Mapping[str, object],
    resources: list[Mapping[str, object]],
) -> bool:
    role_id = str(requirement["role_id"])
    resource = next(
        (
            item
            for item in resources
            if str(item["resource_id"]) == resource_id and bool(item.get("active", True))
        ),
        None,
    )
    if resource is None or role_id not in {str(value) for value in resource.get("role_ids", ())}:
        return False
    return True


def _allowed_resources_by_process(
    *,
    requirements: list[Mapping[str, object]],
    resources: list[Mapping[str, object]],
) -> dict[str, tuple[str, ...]]:
    all_resource_ids = {
        str(resource["resource_id"])
        for resource in resources
        if bool(resource.get("active", True))
    }
    by_process: dict[str, set[str]] = defaultdict(set)
    for requirement in requirements:
        process_id = str(requirement["process_id"])
        eligible = {
            resource_id
            for resource_id in all_resource_ids
            if _resource_eligible_for_requirement(resource_id, requirement, resources)
        }
        by_process[process_id].update(eligible)
    return {
        process_id: tuple(sorted(resource_ids))
        for process_id, resource_ids in by_process.items()
        if resource_ids and resource_ids != all_resource_ids
    }


def _process_absolute_start(
    project_start_at: dt.datetime,
    process: Mapping[str, object],
) -> dt.datetime:
    values = [project_start_at]
    if process.get("remaining_ready_at") is not None:
        values.append(greedy._as_utc(process["remaining_ready_at"]))
    if process.get("started_at") is not None:
        values.append(greedy._as_utc(process["started_at"]))
    if process.get("earliest_start_at") is not None:
        values.append(greedy._as_utc(process["earliest_start_at"]))
    return max(values)


def _process_planner_start(
    project_start_at: dt.datetime,
    process: Mapping[str, object],
) -> dt.datetime:
    start_at = _process_absolute_start(project_start_at, process)
    if process.get("finished_at") is not None:
        return max(greedy._as_utc(process["finished_at"]), start_at)
    return start_at


def _process_is_finished(process: Mapping[str, object]) -> bool:
    return (
        process.get("finished_at") is not None
        or str(process.get("derived_status", "")) == "finished"
    )


def _schedule_from_result(
    *,
    prepared: _PreparedCommitmentProblem,
    result: object,
    stats: object,
    elapsed_seconds: float,
    use_mcts: bool,
) -> dict[str, object]:
    raw_allocations = _raw_allocations(result)
    process_rows, unallocated = _process_rows_from_allocations(
        prepared,
        raw_allocations,
    )
    allocation_slices = _allocation_slices_from_allocations(
        prepared,
        raw_allocations,
        process_rows,
    )
    all_slices = greedy._with_slice_ids(
        slices=greedy._coalesced_slices(
            [
                *prepared.fixed_allocation_slices,
                *allocation_slices,
            ]
        ),
        project_id=prepared.project_id,
        as_of=prepared.as_of,
        horizon_starts_at=prepared.horizon_starts_at,
        horizon_ends_at=prepared.horizon_ends_at,
        options=prepared.options,
    )
    output_slices = (
        all_slices
        if bool(prepared.options.get("include_allocation_slices", False))
        else []
    )
    greedy._apply_fixed_allocation_bounds(
        process_rows,
        prepared.fixed_allocation_slices,
    )
    greedy._attach_allocation_diagnostics(rows=process_rows, unallocated=unallocated)
    greedy._attach_resource_schedule_windows(
        rows=process_rows,
        allocation_slices=all_slices,
        processes=prepared.processes,
        dependencies=prepared.dependencies,
        topo_order=prepared.topo_order,
    )
    tolerance_hours = float(prepared.options.get("convergence_tolerance_hours", 0))
    return {
        "project_id": prepared.project_id,
        "as_of": prepared.as_of,
        "now": prepared.now,
        "horizon_starts_at": prepared.horizon_starts_at,
        "horizon_ends_at": prepared.horizon_ends_at,
        "planning_granularity": prepared.planning_granularity,
        "processes": process_rows,
        "allocation_slices": output_slices,
        "critical_path_process_ids": [],
        "converged": True,
        "iteration_count": 1,
        "convergence": {
            "converged": True,
            "iteration_count": 1,
            "max_iterations": int(prepared.options.get("max_iterations", 1)),
            "tolerance_hours": tolerance_hours,
            "changed_process_ids": [],
            "reason_changes": [],
            "allocation_fingerprint_changed": False,
        },
        "warnings": [
            {
                "code": "mcts_commitment_scheduler",
                "message": "Resource schedule used the commitment-window MCTS scheduler.",
                "severity": "info",
                "details": {
                    "elapsed_seconds": elapsed_seconds,
                    "method": result.method,
                    "objective_makespan_hours": result.objective_makespan,
                    "mcts_enabled": True,
                    "mcts_rollouts_per_action": COMMITMENT_ROLLOUTS_PER_ACTION,
                    "mcts_complete_rollouts": getattr(stats, "complete_rollouts", 0),
                    "mcts_root_actions_evaluated": getattr(
                        stats,
                        "root_actions_evaluated",
                        0,
                    ),
                    "mcts_searched_decisions": getattr(
                        stats,
                        "searched_decisions",
                        0,
                    ),
                    "rollout_calls": getattr(stats, "rollout_calls", 0),
                    "commitment_actions": getattr(stats, "commitment_actions", 0),
                    "mcts_action_limit_ignored": (
                        prepared.options.get("resource_schedule_mcts_max_actions")
                        is not None
                    ),
                    "terminal_reward": (
                        "max(0, min(1, heuristic_makespan / rollout_makespan - 1))"
                    ),
                    "blocked_policy": prepared.blocked_policy,
                    "blocked_process_ids": sorted(prepared.blocked_process_ids),
                    "excluded_process_ids": sorted(prepared.excluded_process_ids),
                    "notes": list(result.notes),
                },
            }
        ],
    }


def _schedule_finish(schedule: Mapping[str, object]) -> dt.datetime | None:
    finishes = []
    for row in greedy._sequence(schedule.get("processes", ())):
        row_map = greedy._mapping(row, "schedule process")
        if row_map.get("ends_at") is not None:
            finishes.append(greedy._as_utc(row_map["ends_at"]))
    return max(finishes, default=None)


def _attach_resource_sensitivity(
    *,
    schedule: dict[str, object],
    input_data: Mapping[str, object],
    baseline_finish_at: dt.datetime | None,
) -> None:
    rows = [
        greedy._mapping(row, "schedule process")
        for row in greedy._sequence(schedule.get("processes", ()))
    ]
    row_by_process = {str(row["process_id"]): row for row in rows}
    tasks = _sensitivity_tasks(input_data)
    if baseline_finish_at is None or not tasks:
        schedule["resource_sensitivity"] = []
        for row in rows:
            row["role_sensitivity"] = []
            row["max_makespan_sensitivity_hours"] = None
            row["sensitivity_label"] = "unknown"
        return

    options = dict(greedy._mapping(input_data.get("options", {}), "options"))
    worker_count = int(
        options.get("resource_schedule_sensitivity_workers")
        or max(1, min(len(tasks), os.cpu_count() or 1))
    )
    use_process_pool = (
        bool(options.get("resource_schedule_sensitivity_process_pool", True))
        and worker_count > 1
        and len(tasks) > 1
    )
    payloads = [
        {
            "input_data": input_data,
            "task": task,
            "baseline_finish_at": baseline_finish_at,
        }
        for task in tasks
    ]
    if use_process_pool:
        try:
            with ProcessPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(_resource_sensitivity_worker, payload): index
                    for index, payload in enumerate(payloads)
                }
                results_by_index = {}
                for future in as_completed(futures):
                    results_by_index[futures[future]] = future.result()
                results = [
                    results_by_index[index]
                    for index in range(len(payloads))
                ]
        except Exception:
            results = [_resource_sensitivity_worker(payload) for payload in payloads]
    else:
        results = [_resource_sensitivity_worker(payload) for payload in payloads]

    schedule["resource_sensitivity"] = results
    by_process: dict[str, list[dict[str, object]]] = defaultdict(list)
    for result in results:
        by_process[str(result["process_id"])].append(result)
    for process_id, row in row_by_process.items():
        sensitivities = sorted(
            by_process.get(process_id, []),
            key=lambda item: (
                str(item.get("role_id") or ""),
                str(item.get("requirement_id") or ""),
            ),
        )
        row["role_sensitivity"] = sensitivities
        deltas = [
            float(item["makespan_delta_hours"])
            for item in sensitivities
            if item.get("makespan_delta_hours") is not None
        ]
        max_delta = max(deltas, default=None)
        row["max_makespan_sensitivity_hours"] = (
            round(max_delta, 6) if max_delta is not None else None
        )
        row["sensitivity_label"] = (
            "makespan_sensitive"
            if max_delta is not None and max_delta > greedy.EPSILON
            else "buffered"
        )


def _sensitivity_tasks(input_data: Mapping[str, object]) -> list[dict[str, object]]:
    tasks = []
    for index, requirement in enumerate(
        greedy._sequence(input_data.get("role_requirements", ()))
    ):
        requirement_map = greedy._mapping(requirement, "role requirement")
        effort_hours = float(requirement_map.get("effort_hours") or 0)
        if effort_hours <= greedy.EPSILON:
            continue
        tasks.append(
            {
                "requirement_index": index,
                "process_id": str(requirement_map["process_id"]),
                "requirement_id": str(requirement_map["requirement_id"]),
                "role_id": str(requirement_map["role_id"]),
                "baseline_effort_hours": effort_hours,
            }
        )
    return tasks


def _resource_sensitivity_worker(payload: Mapping[str, object]) -> dict[str, object]:
    task = dict(greedy._mapping(payload["task"], "sensitivity task"))
    baseline_finish_at = greedy._as_utc(payload["baseline_finish_at"])
    perturbed_input = copy.deepcopy(payload["input_data"])
    options = dict(greedy._mapping(perturbed_input.get("options", {}), "options"))
    sensitivity_backend = options.get("resource_schedule_sensitivity_backend")
    if sensitivity_backend:
        options["resource_schedule_backend"] = str(sensitivity_backend)
    options["include_resource_sensitivity"] = False
    options["include_allocation_slices"] = False
    perturbed_input["options"] = options
    requirements = list(greedy._sequence(perturbed_input.get("role_requirements", ())))
    requirement_index = int(task["requirement_index"])
    requirement = dict(greedy._mapping(requirements[requirement_index], "role requirement"))
    requirement["effort_hours"] = float(requirement.get("effort_hours") or 0) + 1.0
    requirements[requirement_index] = requirement
    perturbed_input["role_requirements"] = requirements
    try:
        schedule = compute_commitment_resource_schedule(perturbed_input)
        perturbed_finish_at = _schedule_finish(schedule)
    except Exception as exc:
        return {
            "process_id": task["process_id"],
            "requirement_id": task["requirement_id"],
            "role_id": task["role_id"],
            "added_effort_hours": 1.0,
            "baseline_effort_hours": task["baseline_effort_hours"],
            "baseline_makespan_at": baseline_finish_at.isoformat(),
            "perturbed_makespan_at": None,
            "makespan_delta_hours": None,
            "makespan_delta_days": None,
            "status": "failed",
            "error": str(exc),
        }
    if perturbed_finish_at is None:
        delta_hours = None
    else:
        delta_hours = (perturbed_finish_at - baseline_finish_at).total_seconds() / 3600
    return {
        "process_id": task["process_id"],
        "requirement_id": task["requirement_id"],
        "role_id": task["role_id"],
        "added_effort_hours": 1.0,
        "baseline_effort_hours": task["baseline_effort_hours"],
        "baseline_makespan_at": baseline_finish_at.isoformat(),
        "perturbed_makespan_at": (
            perturbed_finish_at.isoformat() if perturbed_finish_at is not None else None
        ),
        "makespan_delta_hours": (
            round(delta_hours, 6) if delta_hours is not None else None
        ),
        "makespan_delta_days": (
            round(delta_hours / 24, 6) if delta_hours is not None else None
        ),
        "status": "ok",
    }


def _raw_allocations(result: object) -> list[tuple[str, str, str, int, float]]:
    allocations = []
    for assignment in result.resource_process_assignments:
        hours = float(assignment.hours)
        if hours <= greedy.EPSILON:
            continue
        allocations.append(
            (
                str(assignment.role),
                str(assignment.process),
                str(assignment.resource),
                int(assignment.bucket),
                round(hours, 6),
            )
        )
    return sorted(allocations, key=lambda item: (item[3], item[2], item[1], item[0]))


def _process_rows_from_allocations(
    prepared: _PreparedCommitmentProblem,
    raw_allocations: list[tuple[str, str, str, int, float]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    allocations_by_process: dict[str, list[tuple[str, str, str, int, float]]] = (
        defaultdict(list)
    )
    for allocation in raw_allocations:
        _role, process_id, _resource_id, _bucket_id, _hours = allocation
        allocations_by_process[process_id].append(allocation)

    fixed_by_process: dict[str, list[dict[str, object]]] = defaultdict(list)
    fixed_requirement_ids_by_process: dict[str, set[str]] = defaultdict(set)
    for completion in prepared.fixed_role_completions:
        process_id = str(completion["process_id"])
        fixed_by_process[process_id].append(completion)
        fixed_requirement_ids_by_process[process_id].add(
            str(completion["requirement_id"])
        )

    rows_by_id: dict[str, dict[str, object]] = {}
    unallocated: list[dict[str, object]] = []
    for process_id in prepared.topo_order:
        process_allocations = allocations_by_process.get(process_id, [])
        fixed_completions = fixed_by_process.get(process_id, [])
        requirement_ids = [
            str(requirement["requirement_id"])
            for requirement in prepared.requirements_by_process.get(process_id, [])
        ]
        fixed_requirement_ids = fixed_requirement_ids_by_process.get(process_id, set())
        if process_id in prepared.excluded_process_ids:
            ready_at = _ready_at_from_rows_or_none(prepared, rows_by_id, process_id)
            if process_id in prepared.blocked_process_ids:
                allocation_state = (
                    "blocked_zero_capacity"
                    if prepared.blocked_policy == "include_as_zero_capacity"
                    else "unallocated"
                )
                rows_by_id[process_id] = greedy._process_row(
                    cpm=prepared.cpm_by_id[process_id],
                    ready_at=ready_at,
                    starts_at=None,
                    ends_at=None,
                    allocation_state=allocation_state,
                    requirement_ids=requirement_ids,
                )
                if prepared.blocked_policy == "exclude":
                    unallocated.extend(
                        _blocked_unallocated_rows(
                            prepared=prepared,
                            process_id=process_id,
                            ready_at=ready_at,
                        )
                    )
            else:
                rows_by_id[process_id] = greedy._process_row(
                    cpm=prepared.cpm_by_id[process_id],
                    ready_at=None,
                    starts_at=None,
                    ends_at=None,
                    allocation_state="unallocated",
                    requirement_ids=requirement_ids,
                )
                unallocated.extend(
                    _predecessor_unallocated_rows(
                        prepared=prepared,
                        process_id=process_id,
                    )
                )
            continue
        process = _process_by_id(prepared)[process_id]
        if _process_is_finished(process):
            ready_at = _ready_at_from_rows(prepared, rows_by_id, process_id)
            starts_at = (
                max(greedy._as_utc(process["started_at"]), ready_at)
                if process.get("started_at") is not None
                else ready_at
            )
            ends_at = (
                max(greedy._as_utc(process["finished_at"]), starts_at)
                if process.get("finished_at") is not None
                else starts_at
            )
            rows_by_id[process_id] = greedy._process_row(
                cpm=prepared.cpm_by_id[process_id],
                ready_at=ready_at,
                starts_at=starts_at,
                ends_at=ends_at,
                allocation_state="complete",
                requirement_ids=requirement_ids,
            )
            continue
        if not process_allocations:
            ready_at = _ready_at_from_rows(prepared, rows_by_id, process_id)
            fixed_complete = bool(requirement_ids) and set(requirement_ids).issubset(
                fixed_requirement_ids
            )
            allocation_state = (
                "complete" if not requirement_ids or fixed_complete else "unallocated"
            )
            fixed_starts = [
                greedy._as_utc(completion["starts_at"])
                for completion in fixed_completions
            ]
            fixed_finishes = [
                greedy._as_utc(completion["finish_at"])
                for completion in fixed_completions
            ]
            starts_at = min(fixed_starts, default=ready_at)
            ends_at = max([ready_at, *fixed_finishes]) if allocation_state == "complete" else None
            rows_by_id[process_id] = greedy._process_row(
                cpm=prepared.cpm_by_id[process_id],
                ready_at=ready_at,
                starts_at=starts_at if allocation_state == "complete" else None,
                ends_at=ends_at,
                allocation_state=allocation_state,
                requirement_ids=requirement_ids,
            )
            continue
        ready_at = _ready_at_from_rows(prepared, rows_by_id, process_id)
        starts_at = min(
            [
                *[
                    prepared.bucket_by_id[bucket_id].starts_at
                    for _role, _process_id, _resource_id, bucket_id, _hours in process_allocations
                ],
                *[
                    greedy._as_utc(completion["starts_at"])
                    for completion in fixed_completions
                ],
            ]
        )
        ends_at = max(
            [
                *[
                    prepared.bucket_by_id[bucket_id].ends_at
                    for _role, _process_id, _resource_id, bucket_id, _hours in process_allocations
                ],
                *[
                    greedy._as_utc(completion["finish_at"])
                    for completion in fixed_completions
                ],
                ready_at,
            ]
        )
        rows_by_id[process_id] = greedy._process_row(
            cpm=prepared.cpm_by_id[process_id],
            ready_at=ready_at,
            starts_at=max(starts_at, ready_at),
            ends_at=ends_at,
            allocation_state="complete",
            requirement_ids=requirement_ids,
        )
    return [rows_by_id[process_id] for process_id in prepared.topo_order], unallocated


def _blocked_unallocated_rows(
    *,
    prepared: _PreparedCommitmentProblem,
    process_id: str,
    ready_at: dt.datetime | None,
) -> list[dict[str, object]]:
    rows = []
    for requirement in prepared.requirements_by_process.get(process_id, []):
        rows.append(
            {
                "project_id": prepared.project_id,
                "process_id": process_id,
                "requirement_id": str(requirement["requirement_id"]),
                "role_id": str(requirement["role_id"]),
                "reason": "blocked",
                "message": "The process is blocked by unresolved blockers.",
                "diagnostic_message": (
                    "Unresolved blockers prevented this requirement from being "
                    "scheduled by the commitment-window backend."
                ),
                "required_effort_hours": float(requirement["effort_hours"]),
                "unallocated_effort_hours": float(requirement["effort_hours"]),
                "allocated_effort_hours": 0.0,
                "eligible_resource_ids": [],
                "first_feasible_starts_at": None,
                "diagnostics": {
                    "process_ready_at": ready_at,
                    "blocking_reason": "blocked",
                },
            }
        )
    return rows


def _predecessor_unallocated_rows(
    *,
    prepared: _PreparedCommitmentProblem,
    process_id: str,
) -> list[dict[str, object]]:
    rows = []
    for requirement in prepared.requirements_by_process.get(process_id, []):
        rows.append(
            {
                "project_id": prepared.project_id,
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
                "unallocated_effort_hours": float(requirement["effort_hours"]),
                "allocated_effort_hours": 0.0,
                "eligible_resource_ids": [],
                "first_feasible_starts_at": None,
                "diagnostics": {
                    "process_ready_at": None,
                    "blocking_reason": "predecessor_unallocated",
                },
            }
        )
    return rows


def _ready_at_from_rows(
    prepared: _PreparedCommitmentProblem,
    rows_by_id: Mapping[str, Mapping[str, object]],
    process_id: str,
) -> dt.datetime:
    ready_at = _ready_at_from_rows_or_none(prepared, rows_by_id, process_id)
    if ready_at is None:
        raise ValueError(f"process dependencies are not scheduled: {process_id}")
    return ready_at


def _ready_at_from_rows_or_none(
    prepared: _PreparedCommitmentProblem,
    rows_by_id: Mapping[str, Mapping[str, object]],
    process_id: str,
) -> dt.datetime | None:
    process = _process_by_id(prepared)[process_id]
    dependency_finishes = []
    for dependency in prepared.dependencies.get(process_id, ()):
        row = rows_by_id.get(dependency)
        if row is None or row.get("ends_at") is None:
            return None
        dependency_finishes.append(greedy._as_utc(row["ends_at"]))
    dependency_finish = max(
        dependency_finishes,
        default=prepared.project_start_at,
    )
    constraints = [prepared.project_start_at, dependency_finish]
    if process.get("remaining_ready_at") is not None:
        constraints.append(greedy._as_utc(process["remaining_ready_at"]))
    if process.get("started_at") is not None:
        constraints.append(greedy._as_utc(process["started_at"]))
    if process.get("earliest_start_at") is not None:
        constraints.append(greedy._as_utc(process["earliest_start_at"]))
    return max(constraints)


def _process_by_id(
    prepared: _PreparedCommitmentProblem,
) -> dict[str, Mapping[str, object]]:
    return {str(item["process_id"]): item for item in prepared.processes}


def _allocation_slices_from_allocations(
    prepared: _PreparedCommitmentProblem,
    raw_allocations: list[tuple[str, str, str, int, float]],
    process_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    row_by_id = {str(row["process_id"]): row for row in process_rows}
    slices = []
    for role, process_id, resource_id, bucket_id, hours in raw_allocations:
        requirement = prepared.requirement_by_role_process[(role, process_id)]
        bucket = prepared.ledger_by_resource_time[(resource_id, bucket_id)]
        ready_at = greedy._as_utc(row_by_id[process_id]["ready_at"])
        slices.append(
            greedy._allocation_row(
                project_id=prepared.project_id,
                requirement=requirement,
                resource=prepared.resource_by_id[resource_id],
                bucket=bucket,
                effort_hours=hours,
                ready_at=ready_at,
                iteration=1,
            )
        )
    return slices


def commitment_problem_from_schedule_input(
    input_data: Mapping[str, object],
) -> HeuristicPlanningProblem:
    """Return the commitment planning problem for tests and diagnostics."""
    return _prepare_commitment_problem(input_data).problem

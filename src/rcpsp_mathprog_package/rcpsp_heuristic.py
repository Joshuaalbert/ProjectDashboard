"""
Fast forward/backward heuristic planner for resource-constrained CPM/RCPSP.

This module is intended as the production-scale counterpart to the MathProg/MILP
model in rcpsp_mathprog.py.  It keeps the same modeling primitives -- role-hour
requirements, resource calendars, and finish-to-start precedence -- but avoids
branch-and-bound.  The planner is deterministic by default and returns CPM-like
schedule diagnostics.

Main entry points:
    load_project_json(...)
    ForwardBackwardHeuristicPlanner().solve(problem)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import heapq
import json
import math
import random
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

EPS = 1e-9

Key2 = Tuple[str, str]
Key3 = Tuple[str, str, int]
KeyRT = Tuple[str, int]


@dataclass(frozen=True)
class HeuristicPlanningProblem:
    roles: Sequence[str]
    resources: Sequence[str]
    processes: Sequence[str]
    buckets: Sequence[int]
    requirements: Mapping[Key2, float]
    availability: Mapping[Key3, float]
    predecessors: Mapping[str, Iterable[str]] = field(default_factory=dict)
    bucket_hours: Mapping[int, float] = field(default_factory=dict)
    resource_capacity: Mapping[KeyRT, float] = field(default_factory=dict)
    bucket_start: Mapping[int, float] = field(default_factory=dict)
    bucket_end: Mapping[int, float] = field(default_factory=dict)
    earliest_start: Mapping[str, float] = field(default_factory=dict)
    earliest_finish: Mapping[str, float] = field(default_factory=dict)
    allowed_resources: Mapping[str, Iterable[str]] = field(default_factory=dict)
    preferred_resources: Mapping[Key2, Iterable[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectMetadata:
    process_names: Mapping[str, str] = field(default_factory=dict)
    role_names: Mapping[str, str] = field(default_factory=dict)
    resource_names: Mapping[str, str] = field(default_factory=dict)
    bucket_start_dt: Mapping[int, datetime] = field(default_factory=dict)
    bucket_end_dt: Mapping[int, datetime] = field(default_factory=dict)
    display_timezone: str = "Europe/Amsterdam"

    def format_bucket(self, bucket: Optional[int]) -> Optional[str]:
        if bucket is None:
            return None
        dt = self.bucket_start_dt.get(bucket)
        if dt is None:
            return None
        return dt.astimezone(ZoneInfo(self.display_timezone)).isoformat()

    def format_time(self, value: Optional[float]) -> Optional[str]:
        if value is None or not self.bucket_start_dt:
            return None
        # Values are hours since the first bucket start.  Find nearest bucket boundary.
        first_bucket = min(self.bucket_start_dt)
        base = self.bucket_start_dt[first_bucket]
        dt = base + timedelta(hours=float(value))
        return dt.astimezone(ZoneInfo(self.display_timezone)).isoformat()


@dataclass(frozen=True)
class LoadedProject:
    problem: HeuristicPlanningProblem
    metadata: ProjectMetadata


@dataclass(frozen=True)
class RoleAssignment:
    role: str
    process: str
    bucket: int
    bucket_start: float
    bucket_end: float
    hours: float


@dataclass(frozen=True)
class ResourceRoleAssignment:
    resource: str
    role: str
    bucket: int
    bucket_start: float
    bucket_end: float
    hours: float


@dataclass(frozen=True)
class ResourceProcessAssignment:
    resource: str
    role: str
    process: str
    bucket: int
    bucket_start: float
    bucket_end: float
    hours: float


@dataclass(frozen=True)
class ProcessTiming:
    process: str
    es: Optional[float]
    ef: Optional[float]
    ls: Optional[float]
    lf: Optional[float]
    slack: Optional[float]
    finish_slack: Optional[float]
    start_bucket: Optional[int]
    finish_bucket: Optional[int]
    latest_start_bucket: Optional[int]
    latest_finish_bucket: Optional[int]


@dataclass(frozen=True)
class BindingResourceTime:
    resource: str
    bucket: int
    bucket_start: float
    bucket_end: float
    used_hours: float
    capacity_hours: float


@dataclass(frozen=True)
class BindingRoleTime:
    role: str
    bucket: int
    bucket_start: float
    bucket_end: float
    used_hours: float
    available_hours: float


@dataclass(frozen=True)
class HeuristicSolveResult:
    status: str
    objective_makespan: float
    role_assignments: List[RoleAssignment]
    resource_role_assignments: List[ResourceRoleAssignment]
    resource_process_assignments: List[ResourceProcessAssignment]
    process_timings: Dict[str, ProcessTiming]
    critical_processes: List[str]
    critical_edges: List[Tuple[str, str]]
    critical_path: List[str]
    binding_resource_times: List[BindingResourceTime]
    binding_role_times: List[BindingRoleTime]
    raw_work_h: Dict[Tuple[str, str, int], float]
    raw_resource_role_u: Dict[Tuple[str, str, int], float]
    method: str = "forward-backward-heuristic"
    iterations: int = 1
    notes: Tuple[str, ...] = ()


class HeuristicInfeasibleError(RuntimeError):
    pass


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"datetime must be timezone-aware: {value!r}")
    return dt


def _floor_dt(dt: datetime, minutes: int) -> datetime:
    # Floor in UTC to make the global grid stable across time zones.
    dt = dt.astimezone(timezone.utc)
    total_minutes = dt.hour * 60 + dt.minute
    floored = (total_minutes // minutes) * minutes
    return dt.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)


def _ceil_dt(dt: datetime, minutes: int) -> datetime:
    f = _floor_dt(dt, minutes)
    if f == dt.astimezone(timezone.utc):
        return f
    return f + timedelta(minutes=minutes)


def _daterange(start: date, end: date) -> Iterable[date]:
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def _time_from_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def _overlap_hours(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    lo = max(a0, b0)
    hi = min(a1, b1)
    if hi <= lo:
        return 0.0
    return (hi - lo).total_seconds() / 3600.0


def load_project_json(
    path: str | Path,
    *,
    start: Optional[datetime] = None,
    horizon_days: int = 140,
    bucket_minutes: int = 60,
    display_timezone: str = "Europe/Amsterdam",
) -> LoadedProject:
    """Load the uploaded project JSON format into a heuristic planning problem.

    The input schema expected by this loader is intentionally permissive:
      * tasks contain id, name, roles[{id,hours}], optional depends_on,
        optional earliest_start, optional preferred_resources.
      * edges contain {from,to} precedence arcs.
      * resources contain id, name, roles[{id}], available_from, and a weekly
        calendar with local timezone/windows.

    Calendar windows are converted into global time buckets.  If a window has an
    `hours` field lower than its clock duration, the capacity is scaled by
    hours / duration.
    """
    raw = json.loads(Path(path).read_text())

    roles = [r["id"] for r in raw.get("roles", [])]
    role_names = {r["id"]: r.get("name", r["id"]) for r in raw.get("roles", [])}
    resources = [r["id"] for r in raw.get("resources", [])]
    resource_names = {r["id"]: r.get("name", r["id"]) for r in raw.get("resources", [])}
    processes = [t["id"] for t in raw.get("tasks", [])]
    process_names = {t["id"]: t.get("name", t["id"]) for t in raw.get("tasks", [])}

    if not roles:
        # Some files may omit a top-level role list.
        role_set = []
        seen = set()
        for task in raw.get("tasks", []):
            for rr in task.get("roles", []):
                rid = rr["id"]
                if rid not in seen:
                    seen.add(rid)
                    role_set.append(rid)
                    role_names[rid] = rr.get("name", rid)
        roles = role_set

    requirements: Dict[Key2, float] = {}
    predecessors: Dict[str, Set[str]] = {p: set() for p in processes}
    earliest_start_dt: Dict[str, datetime] = {}
    allowed_resources: Dict[str, Set[str]] = {}

    for task in raw.get("tasks", []):
        p = task["id"]
        for rr in task.get("roles", []):
            requirements[(rr["id"], p)] = requirements.get((rr["id"], p), 0.0) + float(rr.get("hours", 0.0))
        for q in task.get("depends_on", []) or []:
            predecessors.setdefault(p, set()).add(q)
        if task.get("earliest_start"):
            earliest_start_dt[p] = _parse_dt(task["earliest_start"])
        if task.get("preferred_resources"):
            allowed_resources[p] = set(task["preferred_resources"])

    for edge in raw.get("edges", []) or []:
        q = edge["from"]
        p = edge["to"]
        predecessors.setdefault(p, set()).add(q)

    available_from_values = []
    for resource in raw.get("resources", []):
        if resource.get("available_from"):
            available_from_values.append(_parse_dt(resource["available_from"]))
    if earliest_start_dt:
        available_from_values.extend(earliest_start_dt.values())
    if start is None:
        if not available_from_values:
            start = datetime.now(timezone.utc)
        else:
            start = min(available_from_values)
    start_utc = _floor_dt(start, bucket_minutes)
    end_utc = start_utc + timedelta(days=horizon_days)

    bucket_delta = timedelta(minutes=bucket_minutes)
    bucket_hours_value = bucket_minutes / 60.0
    n_buckets = int(math.ceil((end_utc - start_utc) / bucket_delta))
    buckets = list(range(1, n_buckets + 1))
    bucket_start_dt: Dict[int, datetime] = {}
    bucket_end_dt: Dict[int, datetime] = {}
    bucket_hours: Dict[int, float] = {}
    bucket_start: Dict[int, float] = {}
    bucket_end: Dict[int, float] = {}
    for idx, b in enumerate(buckets):
        s = start_utc + idx * bucket_delta
        e = s + bucket_delta
        bucket_start_dt[b] = s
        bucket_end_dt[b] = e
        bucket_hours[b] = bucket_hours_value
        bucket_start[b] = idx * bucket_hours_value
        bucket_end[b] = (idx + 1) * bucket_hours_value

    resource_role_ids: Dict[str, Set[str]] = {}
    for resource in raw.get("resources", []):
        resource_role_ids[resource["id"]] = {rr["id"] for rr in resource.get("roles", [])}

    # Compute per-resource bucket capacity first, then copy it to eligible roles.
    resource_cap_hours: Dict[KeyRT, float] = {}
    for resource in raw.get("resources", []):
        j = resource["id"]
        cal = resource.get("calendar", {}) or {}
        tz = ZoneInfo(cal.get("timezone", display_timezone))
        available_from = _parse_dt(resource.get("available_from")) if resource.get("available_from") else start_utc
        windows = cal.get("weekly_windows", []) or []
        if not windows:
            continue

        # Iterate local dates covering the UTC horizon with a guard day.
        local_start = start_utc.astimezone(tz).date() - timedelta(days=1)
        local_end = end_utc.astimezone(tz).date() + timedelta(days=1)
        for d in _daterange(local_start, local_end):
            weekday = d.weekday()
            for w in windows:
                if int(w.get("weekday")) != weekday:
                    continue
                local_s = datetime.combine(d, _time_from_hhmm(w["start"]), tz)
                local_e = datetime.combine(d, _time_from_hhmm(w["end"]), tz)
                if local_e <= local_s:
                    local_e += timedelta(days=1)
                clock_hours = (local_e - local_s).total_seconds() / 3600.0
                if clock_hours <= 0:
                    continue
                allowed_hours = float(w.get("hours", clock_hours))
                rate = max(0.0, min(1.0, allowed_hours / clock_hours))
                win_s = max(local_s.astimezone(timezone.utc), available_from.astimezone(timezone.utc), start_utc)
                win_e = min(local_e.astimezone(timezone.utc), end_utc)
                if win_e <= win_s:
                    continue
                first = max(1, int(math.floor((win_s - start_utc) / bucket_delta)) + 1)
                last = min(n_buckets, int(math.ceil((win_e - start_utc) / bucket_delta)))
                for b in range(first, last + 1):
                    ov = _overlap_hours(bucket_start_dt[b], bucket_end_dt[b], win_s, win_e) * rate
                    if ov > EPS:
                        resource_cap_hours[(j, b)] = resource_cap_hours.get((j, b), 0.0) + ov

    availability: Dict[Key3, float] = {}
    resource_capacity: Dict[KeyRT, float] = {}
    for j in resources:
        for b in buckets:
            cap_h = min(bucket_hours[b], resource_cap_hours.get((j, b), 0.0))
            if cap_h <= EPS:
                continue
            frac = cap_h / bucket_hours[b]
            resource_capacity[(j, b)] = frac
            for i in resource_role_ids.get(j, set()):
                availability[(j, i, b)] = frac

    earliest_start: Dict[str, float] = {}
    for p, dt in earliest_start_dt.items():
        earliest_start[p] = max(0.0, (_ceil_dt(dt, bucket_minutes) - start_utc).total_seconds() / 3600.0)

    problem = HeuristicPlanningProblem(
        roles=roles,
        resources=resources,
        processes=processes,
        buckets=buckets,
        requirements=requirements,
        availability=availability,
        predecessors=predecessors,
        bucket_hours=bucket_hours,
        resource_capacity=resource_capacity,
        bucket_start=bucket_start,
        bucket_end=bucket_end,
        earliest_start=earliest_start,
        allowed_resources=allowed_resources,
    )
    metadata = ProjectMetadata(
        process_names=process_names,
        role_names=role_names,
        resource_names=resource_names,
        bucket_start_dt=bucket_start_dt,
        bucket_end_dt=bucket_end_dt,
        display_timezone=display_timezone,
    )
    return LoadedProject(problem=problem, metadata=metadata)


def _topological_order(processes: Sequence[str], predecessors: Mapping[str, Iterable[str]]) -> List[str]:
    pred = {p: set(predecessors.get(p, [])) for p in processes}
    for p in processes:
        for q in list(pred[p]):
            if q not in pred:
                raise ValueError(f"predecessor {q!r} is not in process set")
    succ = {p: set() for p in processes}
    for p, qs in pred.items():
        for q in qs:
            succ[q].add(p)
    indeg = {p: len(pred[p]) for p in processes}
    heap = [p for p in processes if indeg[p] == 0]
    heapq.heapify(heap)
    out: List[str] = []
    while heap:
        p = heapq.heappop(heap)
        out.append(p)
        for s in sorted(succ[p]):
            indeg[s] -= 1
            if indeg[s] == 0:
                heapq.heappush(heap, s)
    if len(out) != len(processes):
        raise ValueError("precedence graph contains a cycle")
    return out


def _successors(processes: Sequence[str], predecessors: Mapping[str, Iterable[str]]) -> Dict[str, Set[str]]:
    succ = {p: set() for p in processes}
    for p in processes:
        for q in predecessors.get(p, []):
            succ.setdefault(q, set()).add(p)
    return succ


def _bucket_hours(problem: HeuristicPlanningProblem, t: int) -> float:
    return float(problem.bucket_hours.get(t, 1.0))


def _bucket_start(problem: HeuristicPlanningProblem, t: int) -> float:
    if t in problem.bucket_start:
        return float(problem.bucket_start[t])
    # fallback cumulative for dense integer buckets
    return sum(_bucket_hours(problem, b) for b in problem.buckets if b < t)


def _bucket_end(problem: HeuristicPlanningProblem, t: int) -> float:
    if t in problem.bucket_end:
        return float(problem.bucket_end[t])
    return _bucket_start(problem, t) + _bucket_hours(problem, t)


def _req_roles(problem: HeuristicPlanningProblem, p: str) -> List[str]:
    return [i for i in problem.roles if float(problem.requirements.get((i, p), 0.0)) > EPS]


class ForwardBackwardHeuristicPlanner:
    """Fast serial forward/backward scheduler.

    This is not an exact optimizer.  It produces a feasible schedule quickly and
    returns CPM-like diagnostics from a forward pass and a latest-placement
    backward pass.
    """

    def __init__(
        self,
        *,
        epsilon: float = 1e-7,
        random_seed: Optional[int] = None,
        perturbation: float = 0.0,
        saturation_tolerance: float = 1e-6,
    ) -> None:
        self.epsilon = epsilon
        self.random = random.Random(random_seed)
        self.perturbation = perturbation
        self.saturation_tolerance = saturation_tolerance

    def solve(self, problem: HeuristicPlanningProblem) -> HeuristicSolveResult:
        forward = self._forward(problem)
        backward, back_notes = self._backward(problem, forward)
        timings = self._make_timings(problem, forward, backward)
        role_assignments, resource_role_assignments, resource_process_assignments, h, u = self._decode_assignments(problem, forward["assignments"])
        binding_resources, binding_roles = self._binding_times(problem, h, u)
        critical_processes, critical_edges, critical_path = self._criticality(problem, timings)
        return HeuristicSolveResult(
            status="feasible",
            objective_makespan=float(forward["makespan"]),
            role_assignments=role_assignments,
            resource_role_assignments=resource_role_assignments,
            resource_process_assignments=resource_process_assignments,
            process_timings=timings,
            critical_processes=critical_processes,
            critical_edges=critical_edges,
            critical_path=critical_path,
            binding_resource_times=binding_resources,
            binding_role_times=binding_roles,
            raw_work_h=h,
            raw_resource_role_u=u,
            notes=tuple(back_notes),
        )

    def multi_start(self, problem: HeuristicPlanningProblem, *, runs: int = 20, base_seed: int = 1, perturbation: float = 0.05) -> HeuristicSolveResult:
        best: Optional[HeuristicSolveResult] = None
        for r in range(runs):
            planner = ForwardBackwardHeuristicPlanner(
                epsilon=self.epsilon,
                random_seed=base_seed + r,
                perturbation=perturbation,
                saturation_tolerance=self.saturation_tolerance,
            )
            res = planner.solve(problem)
            if best is None or (res.objective_makespan, len(res.critical_processes)) < (best.objective_makespan, len(best.critical_processes)):
                best = res
        assert best is not None
        return best

    def _static_weights(self, problem: HeuristicPlanningProblem) -> Tuple[Dict[str, float], Dict[str, float]]:
        role_demand = {i: sum(float(problem.requirements.get((i, p), 0.0)) for p in problem.processes) for i in problem.roles}
        role_cap = {i: 0.0 for i in problem.roles}
        for j in problem.resources:
            for i in problem.roles:
                for t in problem.buckets:
                    role_cap[i] += float(problem.availability.get((j, i, t), 0.0)) * _bucket_hours(problem, t)
        scarcity = {i: role_demand[i] / max(role_cap[i], self.epsilon) for i in problem.roles}
        task_weight = {
            p: sum(float(problem.requirements.get((i, p), 0.0)) * (1.0 + scarcity[i]) for i in problem.roles)
            for p in problem.processes
        }
        succ = _successors(problem.processes, problem.predecessors)
        topo = _topological_order(problem.processes, problem.predecessors)
        downstream = {p: task_weight[p] for p in problem.processes}
        for p in reversed(topo):
            if succ[p]:
                downstream[p] = task_weight[p] + max(downstream[s] for s in succ[p])
        return scarcity, downstream

    def _future_role_capacity(self, problem: HeuristicPlanningProblem) -> Dict[Tuple[str, int], float]:
        # Suffix capacities by role and bucket, approximate because total capacity of multiskilled resources overlaps.
        suffix: Dict[Tuple[str, int], float] = {}
        running = {i: 0.0 for i in problem.roles}
        for t in reversed(problem.buckets):
            for i in problem.roles:
                cap = 0.0
                for j in problem.resources:
                    cap += float(problem.availability.get((j, i, t), 0.0)) * _bucket_hours(problem, t)
                running[i] += cap
                suffix[(i, t)] = running[i]
        return suffix

    def _allowed(self, problem: HeuristicPlanningProblem, p: str, j: str) -> bool:
        allowed = problem.allowed_resources.get(p)
        if not allowed:
            return True
        return j in set(allowed)

    def _preferred(self, problem: HeuristicPlanningProblem, i: str, p: str, j: str) -> bool:
        preferred = problem.preferred_resources.get((i, p))
        return bool(preferred and j in set(preferred))

    def _forward(self, problem: HeuristicPlanningProblem) -> Dict[str, object]:
        pred = {p: set(problem.predecessors.get(p, [])) for p in problem.processes}
        scarcity0, downstream = self._static_weights(problem)
        future_cap = self._future_role_capacity(problem)
        rem: Dict[Tuple[str, str], float] = {
            (i, p): float(problem.requirements.get((i, p), 0.0)) for i in problem.roles for p in problem.processes
        }
        completed: Set[str] = {p for p in problem.processes if all(rem[(i, p)] <= self.epsilon for i in problem.roles)}
        start_bucket: Dict[str, Optional[int]] = {p: None for p in problem.processes}
        finish_bucket: Dict[str, Optional[int]] = {p: None for p in problem.processes}
        assignments: Dict[Tuple[str, str, str, int], float] = {}

        for t in problem.buckets:
            free_res: Dict[str, float] = {
                j: float(problem.resource_capacity.get((j, t), 0.0)) * _bucket_hours(problem, t)
                for j in problem.resources
            }
            free_role: Dict[Tuple[str, str], float] = {
                (j, i): float(problem.availability.get((j, i, t), 0.0)) * _bucket_hours(problem, t)
                for j in problem.resources for i in problem.roles
            }
            if sum(free_res.values()) <= self.epsilon:
                continue

            ready = {
                p for p in problem.processes
                if p not in completed
                and pred[p].issubset(completed)
                and _bucket_start(problem, t) + self.epsilon >= float(problem.earliest_start.get(p, 0.0))
            }
            if not ready:
                continue

            no_progress_rounds = 0
            while ready and sum(free_res.values()) > self.epsilon:
                role_remaining_total = {
                    i: sum(rem[(i, p)] for p in problem.processes if p not in completed)
                    for i in problem.roles
                }
                scarcity = {
                    i: role_remaining_total[i] / max(future_cap.get((i, t), 0.0), self.epsilon)
                    for i in problem.roles
                }
                best: Optional[Tuple[float, str, str, str, float]] = None
                for p in sorted(ready):
                    reqs = [(i, float(problem.requirements.get((i, p), 0.0))) for i in problem.roles if float(problem.requirements.get((i, p), 0.0)) > self.epsilon]
                    if not reqs:
                        continue
                    progresses = [1.0 - rem[(i, p)] / r for i, r in reqs]
                    min_prog = min(progresses) if progresses else 1.0
                    role_scarcity_for_p = sum(rem[(i, p)] * (1.0 + scarcity.get(i, scarcity0.get(i, 0.0))) for i, _ in reqs)
                    wait_hours = max(0.0, _bucket_start(problem, t) - float(problem.earliest_start.get(p, 0.0)))
                    wait = wait_hours / 24.0
                    for i, r in reqs:
                        if rem[(i, p)] <= self.epsilon:
                            continue
                        progress_i = 1.0 - rem[(i, p)] / r
                        lag_bonus = max(0.0, min_prog + 0.25 - progress_i)
                        for j in problem.resources:
                            if not self._allowed(problem, p, j):
                                continue
                            avail = min(free_res[j], free_role[(j, i)], rem[(i, p)])
                            if avail <= self.epsilon:
                                continue
                            # Preserve flexible resources: lower flexibility score is better.
                            flex = 0.0
                            for ii in problem.roles:
                                if free_role[(j, ii)] > self.epsilon:
                                    flex += scarcity.get(ii, scarcity0.get(ii, 0.0))
                            noise = 1.0 + (self.random.uniform(-self.perturbation, self.perturbation) if self.perturbation else 0.0)
                            score = noise * (
                                1000.0 * scarcity.get(i, scarcity0.get(i, 0.0))
                                + 10.0 * downstream[p]
                                + 5.0 * role_scarcity_for_p
                                + 25.0 * lag_bonus
                                + (75.0 if self._preferred(problem, i, p, j) else 0.0)
                                + 0.1 * wait
                                + 3.0 * wait_hours
                                - 2.0 * flex
                            )
                            candidate = (score, j, i, p, avail)
                            if best is None or candidate[0] > best[0]:
                                best = candidate
                if best is None:
                    break
                _, j, i, p, avail = best
                amount = min(avail, free_res[j], free_role[(j, i)], rem[(i, p)])
                if amount <= self.epsilon:
                    no_progress_rounds += 1
                    if no_progress_rounds > 10:
                        break
                    continue
                assignments[(j, i, p, t)] = assignments.get((j, i, p, t), 0.0) + amount
                free_res[j] -= amount
                free_role[(j, i)] -= amount
                rem[(i, p)] -= amount
                if start_bucket[p] is None:
                    start_bucket[p] = t
                if all(rem[(ii, p)] <= self.epsilon for ii in problem.roles):
                    completed.add(p)
                    finish_bucket[p] = t
                    ready.remove(p)

            # Some ready tasks may have completed after the last assignment but not removed due to eps.
            for p in list(ready):
                if all(rem[(ii, p)] <= self.epsilon for ii in problem.roles):
                    completed.add(p)
                    finish_bucket[p] = t

            if len(completed) == len(problem.processes):
                makespan = max(_bucket_end(problem, finish_bucket[p]) for p in problem.processes if finish_bucket[p] is not None) if problem.processes else 0.0
                return {
                    "assignments": assignments,
                    "start_bucket": start_bucket,
                    "finish_bucket": finish_bucket,
                    "makespan": makespan,
                    "completed": completed,
                }

        missing = sorted(set(problem.processes) - completed)
        role_left = {f"{i}/{p}": v for (i, p), v in rem.items() if p in missing and v > self.epsilon}
        raise HeuristicInfeasibleError(f"horizon exhausted before all processes completed; missing={missing}, remaining={role_left}")

    def _backward(self, problem: HeuristicPlanningProblem, forward: Mapping[str, object]) -> Tuple[Dict[str, object], List[str]]:
        notes: List[str] = []
        makespan = float(forward["makespan"])
        forward_start = forward["start_bucket"]  # type: ignore[assignment]
        forward_finish = forward["finish_bucket"]  # type: ignore[assignment]
        succ = _successors(problem.processes, problem.predecessors)
        topo = _topological_order(problem.processes, problem.predecessors)
        deadline: Dict[str, float] = {p: makespan for p in problem.processes}
        rem: Dict[Tuple[str, str], float] = {
            (i, p): float(problem.requirements.get((i, p), 0.0)) for i in problem.roles for p in problem.processes
        }
        start_bucket: Dict[str, Optional[int]] = {p: None for p in problem.processes}
        finish_bucket: Dict[str, Optional[int]] = {p: None for p in problem.processes}
        assignments: Dict[Tuple[str, str, str, int], float] = {}
        free_res: Dict[Tuple[str, int], float] = {}
        free_role: Dict[Tuple[str, str, int], float] = {}
        for t in problem.buckets:
            for j in problem.resources:
                free_res[(j, t)] = float(problem.resource_capacity.get((j, t), 0.0)) * _bucket_hours(problem, t)
                for i in problem.roles:
                    free_role[(j, i, t)] = float(problem.availability.get((j, i, t), 0.0)) * _bucket_hours(problem, t)

        # Use the forward predecessor finish times as release bounds in the
        # backward diagnostic.  Without this guard, a successor may consume early
        # capacity that the forward feasible schedule needed for its predecessor,
        # causing artificial negative slack or a spurious backward failure.
        release: Dict[str, float] = {}
        for p0 in problem.processes:
            rel = float(problem.earliest_start.get(p0, 0.0))
            for q0 in problem.predecessors.get(p0, []):
                ff0 = forward_finish.get(q0) if isinstance(forward_finish, dict) else None
                if ff0 is not None:
                    rel = max(rel, _bucket_end(problem, ff0))
            release[p0] = rel

        # Reverse topological, late-placement per process.  If the greedy latest
        # placement cannot fit, fall back to the forward placement windows for the
        # timing diagnostic rather than failing the whole planner.
        for p in reversed(topo):
            if succ[p]:
                deadline[p] = min(
                    _bucket_start(problem, start_bucket[s]) if start_bucket.get(s) is not None else makespan
                    for s in succ[p]
                )
            else:
                deadline[p] = makespan

            # If process has no work, place at deadline.
            if all(rem[(i, p)] <= self.epsilon for i in problem.roles):
                finish_bucket[p] = None
                start_bucket[p] = None
                continue

            for t in reversed(problem.buckets):
                if _bucket_end(problem, t) - self.epsilon > deadline[p]:
                    continue
                if _bucket_start(problem, t) + self.epsilon < release[p]:
                    continue
                # Greedily fill scarce remaining roles for this process in this bucket.
                made = True
                while made:
                    made = False
                    best: Optional[Tuple[float, str, str, float]] = None
                    for i in problem.roles:
                        if rem[(i, p)] <= self.epsilon:
                            continue
                        for j in problem.resources:
                            if not self._allowed(problem, p, j):
                                continue
                            avail = min(free_res[(j, t)], free_role[(j, i, t)], rem[(i, p)])
                            if avail <= self.epsilon:
                                continue
                            # Least flexible resource first; scarce role first.
                            flex = sum(1.0 for ii in problem.roles if free_role[(j, ii, t)] > self.epsilon)
                            score = (
                                1000.0 / max(1.0, flex)
                                + rem[(i, p)]
                                + (75.0 if self._preferred(problem, i, p, j) else 0.0)
                            )
                            if best is None or score > best[0]:
                                best = (score, j, i, avail)
                    if best is None:
                        break
                    _, j, i, avail = best
                    amount = min(avail, free_res[(j, t)], free_role[(j, i, t)], rem[(i, p)])
                    if amount <= self.epsilon:
                        break
                    assignments[(j, i, p, t)] = assignments.get((j, i, p, t), 0.0) + amount
                    free_res[(j, t)] -= amount
                    free_role[(j, i, t)] -= amount
                    rem[(i, p)] -= amount
                    made = True
                    if finish_bucket[p] is None:
                        finish_bucket[p] = t
                    start_bucket[p] = t
                if all(rem[(i, p)] <= self.epsilon for i in problem.roles):
                    break

            if any(rem[(i, p)] > self.epsilon for i in problem.roles):
                notes.append(
                    f"Backward latest-placement could not fit process {p!r} before deadline {deadline[p]:.3f}; "
                    "using forward timing as conservative LS/LF fallback for that process."
                )
                fs = forward_start.get(p) if isinstance(forward_start, dict) else None
                ff = forward_finish.get(p) if isinstance(forward_finish, dict) else None
                start_bucket[p] = fs
                finish_bucket[p] = ff
                # Clear remaining so predecessors can receive a deadline.  This
                # does not alter the forward feasible schedule.
                for i in problem.roles:
                    rem[(i, p)] = 0.0

        return {"assignments": assignments, "start_bucket": start_bucket, "finish_bucket": finish_bucket}, notes

    def _decode_assignments(self, problem: HeuristicPlanningProblem, assignments: Mapping[Tuple[str, str, str, int], float]):
        h: Dict[Tuple[str, str, int], float] = {}
        u: Dict[Tuple[str, str, int], float] = {}
        role_assignments: List[RoleAssignment] = []
        resource_role_assignments: List[ResourceRoleAssignment] = []
        resource_process_assignments: List[ResourceProcessAssignment] = []
        for (j, i, p, t), hours in sorted(assignments.items(), key=lambda kv: (kv[0][3], kv[0][2], kv[0][1], kv[0][0])):
            if hours <= self.epsilon:
                continue
            h[(i, p, t)] = h.get((i, p, t), 0.0) + hours
            u[(j, i, t)] = u.get((j, i, t), 0.0) + hours
            resource_process_assignments.append(ResourceProcessAssignment(j, i, p, t, _bucket_start(problem, t), _bucket_end(problem, t), hours))
        for (i, p, t), hours in sorted(h.items(), key=lambda kv: (kv[0][2], kv[0][1], kv[0][0])):
            role_assignments.append(RoleAssignment(i, p, t, _bucket_start(problem, t), _bucket_end(problem, t), hours))
        for (j, i, t), hours in sorted(u.items(), key=lambda kv: (kv[0][2], kv[0][1], kv[0][0])):
            resource_role_assignments.append(ResourceRoleAssignment(j, i, t, _bucket_start(problem, t), _bucket_end(problem, t), hours))
        return role_assignments, resource_role_assignments, resource_process_assignments, h, u

    def _make_timings(self, problem: HeuristicPlanningProblem, forward: Mapping[str, object], backward: Mapping[str, object]) -> Dict[str, ProcessTiming]:
        fs: Dict[str, Optional[int]] = forward["start_bucket"]  # type: ignore[assignment]
        ff: Dict[str, Optional[int]] = forward["finish_bucket"]  # type: ignore[assignment]
        bs: Dict[str, Optional[int]] = backward["start_bucket"]  # type: ignore[assignment]
        bf: Dict[str, Optional[int]] = backward["finish_bucket"]  # type: ignore[assignment]
        out: Dict[str, ProcessTiming] = {}
        for p in problem.processes:
            es = _bucket_start(problem, fs[p]) if fs.get(p) is not None else None
            ef = _bucket_end(problem, ff[p]) if ff.get(p) is not None else None
            ls = _bucket_start(problem, bs[p]) if bs.get(p) is not None else es
            lf = _bucket_end(problem, bf[p]) if bf.get(p) is not None else ef
            # The backward pass is a heuristic latest-placement schedule, not an
            # exact auxiliary optimization.  It can occasionally construct an
            # alternative schedule where a process starts earlier than the
            # forward scheduled ES.  For operational slack diagnostics, report
            # conservative nonnegative slack by clamping LS/LF to ES/EF.
            if es is not None and ls is not None and ls < es:
                ls = es
            if ef is not None and lf is not None and lf < ef:
                lf = ef
            slack = (ls - es) if es is not None and ls is not None else None
            finish_slack = (lf - ef) if ef is not None and lf is not None else None
            out[p] = ProcessTiming(p, es, ef, ls, lf, slack, finish_slack, fs.get(p), ff.get(p), bs.get(p), bf.get(p))
        return out

    def _binding_times(self, problem: HeuristicPlanningProblem, h: Mapping[Tuple[str, str, int], float], u: Mapping[Tuple[str, str, int], float]):
        binding_resources: List[BindingResourceTime] = []
        for j in problem.resources:
            for t in problem.buckets:
                used = sum(u.get((j, i, t), 0.0) for i in problem.roles)
                cap = float(problem.resource_capacity.get((j, t), 0.0)) * _bucket_hours(problem, t)
                if cap > self.epsilon and used >= cap - self.saturation_tolerance:
                    binding_resources.append(BindingResourceTime(j, t, _bucket_start(problem, t), _bucket_end(problem, t), used, cap))
        binding_roles: List[BindingRoleTime] = []
        for i in problem.roles:
            for t in problem.buckets:
                used = sum(h.get((i, p, t), 0.0) for p in problem.processes)
                cap = sum(float(problem.availability.get((j, i, t), 0.0)) * _bucket_hours(problem, t) for j in problem.resources)
                if cap > self.epsilon and used >= cap - self.saturation_tolerance:
                    binding_roles.append(BindingRoleTime(i, t, _bucket_start(problem, t), _bucket_end(problem, t), used, cap))
        return binding_resources, binding_roles

    def _criticality(self, problem: HeuristicPlanningProblem, timings: Mapping[str, ProcessTiming]):
        tol = max(self.saturation_tolerance, 1e-6)
        critical_processes = [
            p for p, tm in timings.items()
            if (tm.slack is not None and tm.slack <= tol) or (tm.finish_slack is not None and tm.finish_slack <= tol)
        ]
        crit_set = set(critical_processes)
        critical_edges: List[Tuple[str, str]] = []
        for p in problem.processes:
            for q in problem.predecessors.get(p, []):
                tq = timings[q]
                tp = timings[p]
                if q in crit_set and p in crit_set and tq.ef is not None and tp.es is not None and abs(tq.ef - tp.es) <= max(tol, 1.0):
                    critical_edges.append((q, p))
        critical_path = self._longest_path_on_edges(problem.processes, critical_edges, timings)
        return critical_processes, critical_edges, critical_path

    def _longest_path_on_edges(self, processes: Sequence[str], edges: Sequence[Tuple[str, str]], timings: Mapping[str, ProcessTiming]) -> List[str]:
        succ = {p: [] for p in processes}
        pred = {p: [] for p in processes}
        for q, p in edges:
            succ.setdefault(q, []).append(p)
            pred.setdefault(p, []).append(q)
        try:
            topo = _topological_order(processes, {p: pred.get(p, []) for p in processes})
        except ValueError:
            return []
        best_len = {p: 1 if p in timings else 0 for p in processes}
        parent: Dict[str, Optional[str]] = {p: None for p in processes}
        for p in topo:
            for s in succ.get(p, []):
                if best_len[p] + 1 > best_len[s]:
                    best_len[s] = best_len[p] + 1
                    parent[s] = p
        if not best_len:
            return []
        end = max(best_len, key=lambda p: (best_len[p], timings[p].ef or -1))
        if best_len[end] <= 1 and end not in set(sum(([a, b] for a, b in edges), [])):
            return []
        path: List[str] = []
        cur: Optional[str] = end
        while cur is not None:
            path.append(cur)
            cur = parent[cur]
        return list(reversed(path))


def summarize_result(result: HeuristicSolveResult, metadata: Optional[ProjectMetadata] = None, *, max_rows: int = 200) -> str:
    """Create a compact human-readable text report."""
    def pname(p: str) -> str:
        if metadata:
            return metadata.process_names.get(p, p)
        return p
    def rname(r: str) -> str:
        if metadata:
            return metadata.resource_names.get(r, r)
        return r
    def role_name(i: str) -> str:
        if metadata:
            return metadata.role_names.get(i, i)
        return i
    def fmt(v: Optional[float]) -> str:
        if v is None:
            return "-"
        if metadata:
            s = metadata.format_time(v)
            if s:
                return s
        return f"{v:.2f}"

    lines: List[str] = []
    lines.append(f"Status: {result.status}")
    lines.append(f"Makespan: {result.objective_makespan:.2f} h ({fmt(result.objective_makespan)})")
    if result.notes:
        lines.append("Notes:")
        for n in result.notes[:10]:
            lines.append(f"  - {n}")
    lines.append("")
    lines.append("Process timings:")
    header = f"{'process':45s} {'ES':25s} {'EF':25s} {'LS':25s} {'LF':25s} {'slack h':>8s}"
    lines.append(header)
    lines.append("-" * len(header))
    for p, tm in sorted(result.process_timings.items(), key=lambda kv: (kv[1].ef if kv[1].ef is not None else math.inf, kv[0])):
        slack = "-" if tm.slack is None else f"{tm.slack:.1f}"
        lines.append(f"{pname(p)[:45]:45s} {fmt(tm.es)[:25]:25s} {fmt(tm.ef)[:25]:25s} {fmt(tm.ls)[:25]:25s} {fmt(tm.lf)[:25]:25s} {slack:>8s}")
    lines.append("")
    lines.append("Critical path diagnostic:")
    if result.critical_path:
        lines.append("  " + " -> ".join(pname(p) for p in result.critical_path))
    else:
        lines.append("  No pure precedence critical chain identified; bottleneck is likely resource/calendar constrained.")
    lines.append(f"Critical processes: {len(result.critical_processes)}")
    if result.critical_processes:
        lines.append("  " + ", ".join(pname(p) for p in result.critical_processes[:20]))
    lines.append("")
    lines.append("Top binding resource times:")
    for br in result.binding_resource_times[:max_rows]:
        lines.append(f"  {rname(br.resource)} t={br.bucket} used={br.used_hours:.2f}/{br.capacity_hours:.2f}")
    lines.append("")
    lines.append("Top binding role times:")
    for bt in result.binding_role_times[:max_rows]:
        lines.append(f"  {role_name(bt.role)} t={bt.bucket} used={bt.used_hours:.2f}/{bt.available_hours:.2f}")
    return "\n".join(lines)

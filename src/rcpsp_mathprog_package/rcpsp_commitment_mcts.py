"""Commitment-window MDP/MCTS planner for resource-constrained project scheduling.

This module changes the MDP transition semantics from hourly actor choices to
calendar-window commitment choices.

Instead of asking a resource what to do in every bucket, each resource owns a
cursor on its own calendar.  An action is:

    assign resource j to (process p, role i) from j's cursor forward

The transition fills a contiguous availability window for that resource-role,
stopping at the earliest of:

* the process-role requirement becoming complete;
* the end of the current consecutive availability window;
* the project horizon.

This models the project-management idea that humans are usually assigned to a
focus block (for example a working day), not micromanaged hour by hour.  It also
makes rollouts much shorter: rollout length is proportional to the number of
commitment blocks plus idle jumps, not to the number of hourly buckets.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
import csv
import math
import time
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

try:  # package import
    from .rcpsp_heuristic import (
        EPS,
        BindingResourceTime,
        BindingRoleTime,
        ForwardBackwardHeuristicPlanner,
        HeuristicInfeasibleError,
        HeuristicPlanningProblem,
        HeuristicSolveResult,
        ProcessTiming,
        ProjectMetadata,
        ResourceProcessAssignment,
        ResourceRoleAssignment,
        RoleAssignment,
        _bucket_end,
        _bucket_hours,
        _bucket_start,
        _successors,
        _topological_order,
        summarize_result,
    )
except ImportError:  # direct script import with PYTHONPATH set to this folder
    from rcpsp_heuristic import (  # type: ignore
        EPS,
        BindingResourceTime,
        BindingRoleTime,
        ForwardBackwardHeuristicPlanner,
        HeuristicInfeasibleError,
        HeuristicPlanningProblem,
        HeuristicSolveResult,
        ProcessTiming,
        ProjectMetadata,
        ResourceProcessAssignment,
        ResourceRoleAssignment,
        RoleAssignment,
        _bucket_end,
        _bucket_hours,
        _bucket_start,
        _successors,
        _topological_order,
        summarize_result,
    )

IDLE_ACTION = "__IDLE__"
Action = Union[str, Tuple[str, str]]  # IDLE_ACTION or (process, role)
Key2 = Tuple[str, str]
Key4 = Tuple[str, str, str, int]


@dataclass(frozen=True)
class CommitmentMCTSOptions:
    """Options for the commitment-window planner."""

    use_mcts: bool = True
    complete_rollouts_per_action: int = 10
    rollout_transition_limit: int = 5000
    reward_clip_max: float = 1.0
    random_seed: int = 1
    epsilon: float = 1e-7
    allow_idle_when_work_legal: bool = False
    search_only_when_choices_at_least: int = 2
    # Heuristic weights.  The downstream weight intentionally makes the raw
    # heuristic CPM-like; the counterexample shows this can be wrong when a
    # flexible resource should be reserved for a non-successor role.
    downstream_weight: float = 100.0
    scarcity_weight: float = 8.0
    remaining_weight: float = 1.0
    continuity_bonus: float = 25.0
    flexible_resource_penalty: float = 2.0


@dataclass
class CommitmentMCTSStats:
    searched_decisions: int = 0
    root_actions_evaluated: int = 0
    complete_rollouts: int = 0
    rollout_calls: int = 0
    greedy_decisions: int = 0
    idle_decisions: int = 0
    commitment_actions: int = 0
    runtime_seconds: float = 0.0


@dataclass
class _CommitmentState:
    cursor: Dict[str, float]
    rem: Dict[Key2, float]
    start_time: Dict[str, Optional[float]]
    finish_time: Dict[str, Optional[float]]
    last_work_end: Dict[str, float]
    assignments: Optional[Dict[Key4, float]] = None
    last_commitment: Dict[str, Optional[Tuple[str, str]]] = field(default_factory=dict)
    done_time: Optional[float] = None

    def clone(self, *, keep_assignments: bool = False) -> "_CommitmentState":
        return _CommitmentState(
            cursor=dict(self.cursor),
            rem=dict(self.rem),
            start_time=dict(self.start_time),
            finish_time=dict(self.finish_time),
            last_work_end=dict(self.last_work_end),
            assignments=dict(self.assignments) if keep_assignments and self.assignments is not None else None,
            last_commitment=dict(self.last_commitment),
            done_time=self.done_time,
        )


class _CalendarIndex:
    def __init__(self, problem: HeuristicPlanningProblem, epsilon: float) -> None:
        self.problem = problem
        self.epsilon = epsilon
        self.buckets = list(problem.buckets)
        self.starts = [_bucket_start(problem, t) for t in self.buckets]
        self.ends = [_bucket_end(problem, t) for t in self.buckets]
        self.index_by_bucket = {t: index for index, t in enumerate(self.buckets)}
        self.horizon_start = min(self.starts) if self.starts else 0.0
        self.horizon_end = max(self.ends) if self.ends else 0.0
        self._any_rate: Dict[Tuple[str, int], float] = {}
        self._available_roles: Dict[Tuple[str, int], Tuple[str, ...]] = {}
        for j in problem.resources:
            for t in self.buckets:
                cap = float(problem.resource_capacity.get((j, t), 0.0))
                if cap <= epsilon:
                    self._any_rate[(j, t)] = 0.0
                    self._available_roles[(j, t)] = ()
                    continue
                role_rates = tuple(
                    (
                        i,
                        min(cap, float(problem.availability.get((j, i, t), 0.0))),
                    )
                    for i in problem.roles
                )
                available = tuple(i for i, rate in role_rates if rate > epsilon)
                self._available_roles[(j, t)] = available
                self._any_rate[(j, t)] = max((rate for _i, rate in role_rates), default=0.0)

    def bucket_at(self, value: float) -> Optional[int]:
        """Return bucket id containing value, treating exact boundaries as next bucket."""
        eps = self.epsilon
        idx = bisect.bisect_right(self.starts, value + eps) - 1
        if idx < 0:
            return self.buckets[0] if abs(value - self.starts[0]) <= eps else None
        if idx >= len(self.buckets):
            idx = len(self.buckets) - 1
        if self.starts[idx] - eps <= value < self.ends[idx] - eps:
            return self.buckets[idx]
        return None

    def previous_bucket_for_boundary(self, value: float) -> Optional[int]:
        eps = self.epsilon
        prev: Optional[int] = None
        for t, s, e in zip(self.buckets, self.starts, self.ends):
            if s - eps <= value < e - eps:
                return t
            if abs(value - e) <= eps:
                return t
            if value >= e - eps:
                prev = t
        return prev

    def rate(self, j: str, i: str, t: int) -> float:
        p = self.problem
        return min(
            float(p.resource_capacity.get((j, t), 0.0)),
            float(p.availability.get((j, i, t), 0.0)),
        )

    def any_rate(self, j: str, t: int) -> float:
        return self._any_rate.get((j, t), 0.0)

    def is_role_available_now(self, j: str, i: str, value: float) -> bool:
        t = self.bucket_at(value)
        return t is not None and self.rate(j, i, t) > self.epsilon

    def available_roles_now(self, j: str, value: float) -> Tuple[str, ...]:
        t = self.bucket_at(value)
        if t is None:
            return ()
        return self._available_roles.get((j, t), ())

    def is_any_available_now(self, j: str, value: float) -> bool:
        t = self.bucket_at(value)
        return t is not None and self.any_rate(j, t) > self.epsilon

    def next_any_availability(self, j: str, value: float) -> Optional[float]:
        eps = self.epsilon
        if self.is_any_available_now(j, value):
            return value
        for t, s, e in zip(self.buckets, self.starts, self.ends):
            if e <= value + eps:
                continue
            if self.any_rate(j, t) <= eps:
                continue
            return max(value, s)
        return None

    def current_any_window_end(self, j: str, value: float) -> float:
        """End of the current consecutive availability window for resource j."""
        eps = self.epsilon
        t0 = self.bucket_at(value)
        if t0 is None or self.any_rate(j, t0) <= eps:
            nxt = self.next_any_availability(j, value)
            return self.horizon_end if nxt is None else nxt
        idx = self.index_by_bucket[t0]
        end = self.ends[idx]
        k = idx + 1
        while k < len(self.buckets):
            if abs(self.starts[k] - end) > eps:
                break
            if self.any_rate(j, self.buckets[k]) <= eps:
                break
            end = self.ends[k]
            k += 1
        return end

    def next_bucket_start_after(self, value: float) -> Optional[float]:
        eps = self.epsilon
        for s in self.starts:
            if s > value + eps:
                return s
        return None


class CommitmentMCTSPlanner(ForwardBackwardHeuristicPlanner):
    """Planner using resource-cursor commitment actions and root rollout search."""

    def __init__(self, options: Optional[CommitmentMCTSOptions] = None) -> None:
        self.options = options or CommitmentMCTSOptions()
        super().__init__(epsilon=self.options.epsilon, random_seed=self.options.random_seed)
        self._stats = CommitmentMCTSStats()
        self._pred: Dict[str, Set[str]] = {}
        self._succ: Dict[str, Set[str]] = {}
        self._scarcity: Dict[str, float] = {}
        self._downstream: Dict[str, float] = {}
        self._cal: Optional[_CalendarIndex] = None
        self._resource_order: Dict[str, int] = {}
        self._role_capacity_after_cache: Dict[Tuple[str, float], float] = {}
        self._required_roles_by_process: Dict[str, Tuple[str, ...]] = {}
        self._processes_by_role: Dict[str, Tuple[str, ...]] = {}
        self._required_pairs: Tuple[Key2, ...] = ()
        self._rollout_terminal_cache: Dict[Tuple[object, ...], float] = {}

    @property
    def stats(self) -> CommitmentMCTSStats:
        return self._stats

    def solve(self, problem: HeuristicPlanningProblem) -> HeuristicSolveResult:
        started = time.perf_counter()
        self._prepare_commitment(problem)
        state = self._run_policy(problem, use_mcts=self.options.use_mcts, record=True)
        self._stats.runtime_seconds = time.perf_counter() - started
        return self._state_to_result(problem, state)

    def solve_raw_heuristic(self, problem: HeuristicPlanningProblem) -> HeuristicSolveResult:
        started = time.perf_counter()
        self._prepare_commitment(problem)
        state = self._run_policy(problem, use_mcts=False, record=True)
        self._stats.runtime_seconds = time.perf_counter() - started
        return self._state_to_result(problem, state, method="commitment-window-raw-heuristic")

    def exact_optimal_makespan(self, problem: HeuristicPlanningProblem, *, include_idle_when_work_legal: bool = True) -> float:
        """Exhaustively solve a small commitment-window MDP instance.

        This is intended only for small counterexamples/regression tests.  It is
        exponential in the number of commitment decisions.
        """
        self._prepare_commitment(problem)
        initial = self._initial_state(problem, record=False)
        memo: Dict[Tuple[object, ...], float] = {}
        visiting: Set[Tuple[object, ...]] = set()

        def key(st: _CommitmentState) -> Tuple[object, ...]:
            return (
                tuple(round(st.cursor[j], 7) for j in problem.resources),
                tuple(round(st.rem.get((i, p), 0.0), 7) for i, p in self._required_pairs),
                tuple(None if st.finish_time[p] is None else round(st.finish_time[p], 7) for p in problem.processes),
            )

        def dfs(st: _CommitmentState) -> float:
            self._normalize_all_cursors(problem, st)
            self._update_done(problem, st)
            if st.done_time is not None:
                return st.done_time
            actor = self._next_actor(problem, st)
            if actor is None:
                return math.inf
            legal = self._legal_actions(problem, st, actor, include_idle_when_work_legal=include_idle_when_work_legal)
            k = key(st)
            if k in memo:
                return memo[k]
            if k in visiting:
                return math.inf
            visiting.add(k)
            best = math.inf
            for action in legal:
                child = st.clone(keep_assignments=False)
                progressed = self._apply_action(problem, child, actor, action, record=False)
                if not progressed and action == IDLE_ACTION:
                    # Guard against pathological zero-length idles.
                    if abs(child.cursor[actor] - st.cursor[actor]) <= self.epsilon:
                        child.cursor[actor] = self._calendar().horizon_end + 1.0
                value = dfs(child)
                if value < best:
                    best = value
            visiting.remove(k)
            memo[k] = best
            return best

        return dfs(initial)

    # ------------------------------------------------------------------
    # Core environment

    def _prepare_commitment(self, problem: HeuristicPlanningProblem) -> None:
        self._pred = {p: set(problem.predecessors.get(p, [])) for p in problem.processes}
        self._succ = _successors(problem.processes, problem.predecessors)
        self._scarcity, self._downstream = self._static_weights(problem)
        self._cal = _CalendarIndex(problem, self.epsilon)
        self._resource_order = {j: idx for idx, j in enumerate(problem.resources)}
        self._required_roles_by_process = {
            p: tuple(
                i
                for i in problem.roles
                if float(problem.requirements.get((i, p), 0.0)) > self.epsilon
            )
            for p in problem.processes
        }
        processes_by_role: Dict[str, List[str]] = {i: [] for i in problem.roles}
        required_pairs: List[Key2] = []
        for p in problem.processes:
            for i in self._required_roles_by_process[p]:
                processes_by_role.setdefault(i, []).append(p)
                required_pairs.append((i, p))
        self._processes_by_role = {
            i: tuple(processes) for i, processes in processes_by_role.items()
        }
        self._required_pairs = tuple(required_pairs)
        self._role_capacity_after_cache = {}
        self._rollout_terminal_cache = {}
        self._stats = CommitmentMCTSStats()

    def _calendar(self) -> _CalendarIndex:
        assert self._cal is not None
        return self._cal

    def _initial_state(self, problem: HeuristicPlanningProblem, *, record: bool) -> _CommitmentState:
        cal = self._calendar()
        cursor = {j: cal.horizon_start for j in problem.resources}
        rem = {
            (i, p): float(problem.requirements.get((i, p), 0.0))
            for i, p in self._required_pairs
        }
        start_time = {p: None for p in problem.processes}
        finish_time = {p: None for p in problem.processes}
        last_work_end = {p: -math.inf for p in problem.processes}
        state = _CommitmentState(
            cursor=cursor,
            rem=rem,
            start_time=start_time,
            finish_time=finish_time,
            last_work_end=last_work_end,
            assignments={} if record else None,
            last_commitment={j: None for j in problem.resources},
        )
        self._normalize_all_cursors(problem, state)
        self._update_done(problem, state)
        return state

    def _completion_lower_bound(
        self,
        problem: HeuristicPlanningProblem,
        state: _CommitmentState,
        p: str,
    ) -> Optional[float]:
        cal = self._calendar()
        values = [
            cal.horizon_start,
            float(problem.earliest_start.get(p, 0.0)),
            float(problem.earliest_finish.get(p, 0.0)),
        ]
        for q in self._pred.get(p, set()):
            finish = state.finish_time.get(q)
            if finish is None:
                return None
            values.append(float(finish))
        return max(values)

    def _propagate_zero_requirement_completions(
        self,
        problem: HeuristicPlanningProblem,
        state: _CommitmentState,
    ) -> None:
        changed = True
        while changed:
            changed = False
            for p in problem.processes:
                if self._required_roles_by_process.get(p):
                    continue
                finish = self._completion_lower_bound(problem, state, p)
                if finish is None:
                    continue
                if (
                    state.finish_time[p] is None
                    or state.finish_time[p] + self.epsilon < finish
                ):
                    state.finish_time[p] = finish
                    state.last_work_end[p] = max(state.last_work_end[p], finish)
                    changed = True

    def _run_policy(self, problem: HeuristicPlanningProblem, *, use_mcts: bool, record: bool) -> _CommitmentState:
        state = self._initial_state(problem, record=record)
        transitions = 0
        while transitions <= self.options.rollout_transition_limit:
            self._normalize_all_cursors(problem, state)
            self._update_done(problem, state)
            if state.done_time is not None:
                return state
            actor = self._next_actor(problem, state)
            if actor is None:
                break
            legal = self._legal_actions(problem, state, actor, include_idle_when_work_legal=self.options.allow_idle_when_work_legal)
            if not legal:
                legal = [IDLE_ACTION]
            if legal == [IDLE_ACTION]:
                action: Action = IDLE_ACTION
                self._stats.idle_decisions += 1
            elif (
                use_mcts
                and len(legal) >= self.options.search_only_when_choices_at_least
            ):
                action = self._choose_by_root_rollouts(problem, state, actor, legal)
                self._stats.searched_decisions += 1
            else:
                action = self._greedy_action(problem, state, actor, legal)
                self._stats.greedy_decisions += 1
            self._apply_action(problem, state, actor, action, record=record)
            transitions += 1
        raise HeuristicInfeasibleError("commitment-window rollout did not terminate within the transition/horizon limit")

    def _normalize_all_cursors(self, problem: HeuristicPlanningProblem, state: _CommitmentState) -> None:
        cal = self._calendar()
        for j in problem.resources:
            cur = state.cursor.get(j, cal.horizon_start)
            if cur > cal.horizon_end + self.epsilon:
                continue
            if not cal.is_any_available_now(j, cur):
                nxt = cal.next_any_availability(j, cur)
                state.cursor[j] = cal.horizon_end + 1.0 if nxt is None else nxt

    def _next_actor(self, problem: HeuristicPlanningProblem, state: _CommitmentState) -> Optional[str]:
        cal = self._calendar()
        candidates = [j for j in problem.resources if state.cursor.get(j, cal.horizon_end + 1.0) <= cal.horizon_end + self.epsilon]
        if not candidates:
            return None
        return min(candidates, key=lambda j: (state.cursor[j], self._resource_order.get(j, 10**9)))

    def _process_ready_at(self, problem: HeuristicPlanningProblem, state: _CommitmentState, p: str, value: float) -> bool:
        if self._process_complete(problem, state, p):
            return False
        if value + self.epsilon < float(problem.earliest_start.get(p, 0.0)):
            return False
        for q in self._pred.get(p, set()):
            ft = state.finish_time.get(q)
            if ft is None or ft > value + self.epsilon:
                return False
        return True

    def _process_complete(self, problem: HeuristicPlanningProblem, state: _CommitmentState, p: str) -> bool:
        return all(
            state.rem.get((i, p), 0.0) <= self.epsilon
            for i in self._required_roles_by_process.get(p, ())
        )

    def _legal_actions(
        self,
        problem: HeuristicPlanningProblem,
        state: _CommitmentState,
        actor: str,
        *,
        include_idle_when_work_legal: bool,
    ) -> List[Action]:
        cal = self._calendar()
        cur = state.cursor.get(actor, cal.horizon_end + 1.0)
        if cur > cal.horizon_end + self.epsilon:
            return [IDLE_ACTION]
        actions: List[Action] = []
        for p in problem.processes:
            if not self._process_ready_at(problem, state, p, cur):
                continue
            if not self._allowed(problem, p, actor):
                continue
            for i in self._required_roles_by_process.get(p, ()):
                if state.rem.get((i, p), 0.0) <= self.epsilon:
                    continue
                if cal.is_role_available_now(actor, i, cur):
                    actions.append((p, i))
        # Stable order: deterministic, but not biased by score.
        actions.sort(key=lambda a: (a[0], a[1]))  # type: ignore[index]
        if not actions:
            return [IDLE_ACTION]
        if include_idle_when_work_legal:
            return [IDLE_ACTION] + actions
        return actions

    def _apply_action(self, problem: HeuristicPlanningProblem, state: _CommitmentState, actor: str, action: Action, *, record: bool) -> bool:
        if action == IDLE_ACTION:
            before = state.cursor.get(actor, self._calendar().horizon_end + 1.0)
            state.cursor[actor] = self._idle_advance_time(problem, state, actor)
            return state.cursor[actor] > before + self.epsilon
        assert isinstance(action, tuple)
        p, i = action
        return self._commit_work(problem, state, actor, p, i, record=record)

    def _idle_advance_time(self, problem: HeuristicPlanningProblem, state: _CommitmentState, actor: str) -> float:
        cal = self._calendar()
        cur = state.cursor.get(actor, cal.horizon_end + 1.0)
        if cur > cal.horizon_end + self.epsilon:
            return cur
        current_window_end = cal.current_any_window_end(actor, cur)
        candidate = current_window_end
        # If a predecessor/release event happens before the end of the current
        # work window, a resource that currently has no legal task can wake up
        # at that event rather than idling the entire day.
        for p in problem.processes:
            if self._process_complete(problem, state, p):
                continue
            rel = float(problem.earliest_start.get(p, 0.0))
            pred_times = [state.finish_time.get(q) for q in self._pred.get(p, set())]
            if any(x is None for x in pred_times):
                continue
            ready_time = max([rel] + [float(x) for x in pred_times if x is not None])
            if ready_time > cur + self.epsilon and ready_time < candidate - self.epsilon:
                candidate = ready_time
        if candidate <= cur + self.epsilon:
            nxt = cal.next_any_availability(actor, cur + self.epsilon)
            return cal.horizon_end + 1.0 if nxt is None else nxt
        return min(candidate, cal.horizon_end + 1.0)

    def _commit_work(self, problem: HeuristicPlanningProblem, state: _CommitmentState, j: str, p: str, i: str, *, record: bool) -> bool:
        cal = self._calendar()
        cur = state.cursor[j]
        if state.rem.get((i, p), 0.0) <= self.epsilon:
            return False
        if not self._process_ready_at(problem, state, p, cur):
            return False
        if not self._allowed(problem, p, j):
            return False

        started_at = cur
        total = 0.0
        while state.rem.get((i, p), 0.0) > self.epsilon and cur <= cal.horizon_end + self.epsilon:
            t = cal.bucket_at(cur)
            if t is None:
                break
            rate = cal.rate(j, i, t)
            if rate <= self.epsilon:
                break
            seg_end = _bucket_end(problem, t)
            if seg_end <= cur + self.epsilon:
                break
            possible = (seg_end - cur) * rate
            amount = min(state.rem.get((i, p), 0.0), possible)
            if amount <= self.epsilon:
                break
            duration = amount / rate
            end = cur + duration
            if record:
                assert state.assignments is not None
                state.assignments[(j, i, p, t)] = state.assignments.get((j, i, p, t), 0.0) + amount
            if state.start_time[p] is None:
                state.start_time[p] = cur
            state.rem[(i, p)] = max(0.0, state.rem[(i, p)] - amount)
            total += amount
            cur = end
            state.last_work_end[p] = max(state.last_work_end[p], end)
            # Stop if the role requirement is done before the window ends so the
            # resource can immediately choose another task in the same day.
            if state.rem.get((i, p), 0.0) <= self.epsilon:
                break
            # Stop if the next bucket is not part of the same consecutive
            # resource-role availability window.
            if cur < seg_end - self.epsilon:
                break
            next_t = cal.bucket_at(cur)
            if next_t is None or cal.rate(j, i, next_t) <= self.epsilon:
                break

        if total <= self.epsilon:
            return False
        state.cursor[j] = cur
        state.last_commitment[j] = (p, i)
        if self._process_complete(problem, state, p):
            state.finish_time[p] = max(
                state.last_work_end[p],
                float(problem.earliest_start.get(p, 0.0)),
                float(problem.earliest_finish.get(p, 0.0)),
            )
        self._stats.commitment_actions += 1
        self._update_done(problem, state)
        return True

    def _update_done(self, problem: HeuristicPlanningProblem, state: _CommitmentState) -> None:
        self._propagate_zero_requirement_completions(problem, state)
        if all(self._process_complete(problem, state, p) for p in problem.processes):
            finishes = [state.finish_time[p] for p in problem.processes if state.finish_time[p] is not None]
            state.done_time = max(finishes) if finishes else self._calendar().horizon_start

    # ------------------------------------------------------------------
    # Heuristic prior and root rollout search

    def _greedy_action(self, problem: HeuristicPlanningProblem, state: _CommitmentState, actor: str, legal: Sequence[Action]) -> Action:
        if not legal:
            return IDLE_ACTION
        return max(legal, key=lambda a: self._prior_score(problem, state, actor, a))

    def _prior_score(self, problem: HeuristicPlanningProblem, state: _CommitmentState, actor: str, action: Action) -> float:
        if action == IDLE_ACTION:
            return -1.0e9
        assert isinstance(action, tuple)
        p, i = action
        rem_ip = state.rem.get((i, p), 0.0)
        total_role_remaining = sum(
            state.rem.get((i, pp), 0.0)
            for pp in self._processes_by_role.get(i, ())
        )
        cur = state.cursor[actor]
        cal = self._calendar()
        total_role_capacity = self._role_capacity_after(problem, i, cur)
        scarcity = total_role_remaining / max(total_role_capacity, self.epsilon)
        continuity = self.options.continuity_bonus if state.last_commitment.get(actor) == (p, i) else 0.0
        flex = sum(
            self._scarcity.get(ii, 0.0)
            for ii in cal.available_roles_now(actor, cur)
        )
        # Prefer roles lagging behind other roles of the same process.
        reqs = [
            (ii, float(problem.requirements.get((ii, p), 0.0)))
            for ii in self._required_roles_by_process.get(p, ())
        ]
        progresses = [
            1.0 - state.rem.get((ii, p), 0.0) / r
            for ii, r in reqs
            if r > self.epsilon
        ]
        min_prog = min(progresses) if progresses else 1.0
        req_i = float(problem.requirements.get((i, p), 0.0))
        prog_i = 1.0 - state.rem.get((i, p), 0.0) / req_i if req_i > self.epsilon else 1.0
        lag = max(0.0, min_prog + 0.25 - prog_i)
        preferred = problem.preferred_resources.get((i, p))
        preferred_resource_bonus = 75.0 if preferred and actor in set(preferred) else 0.0
        return (
            self.options.downstream_weight * self._downstream.get(p, 0.0)
            + self.options.scarcity_weight * scarcity
            + self.options.remaining_weight * rem_ip
            + 20.0 * lag
            + preferred_resource_bonus
            + continuity
            - self.options.flexible_resource_penalty * flex
        )

    def _role_capacity_after(
        self,
        problem: HeuristicPlanningProblem,
        role: str,
        cursor: float,
    ) -> float:
        key = (role, round(cursor, 7))
        cached = self._role_capacity_after_cache.get(key)
        if cached is not None:
            return cached
        cal = self._calendar()
        total = 0.0
        for j in problem.resources:
            for t in problem.buckets:
                if _bucket_end(problem, t) > cursor + self.epsilon:
                    total += cal.rate(j, role, t) * _bucket_hours(problem, t)
        self._role_capacity_after_cache[key] = total
        return total

    def _choose_by_root_rollouts(self, problem: HeuristicPlanningProblem, root_state: _CommitmentState, actor: str, legal: Sequence[Action]) -> Action:
        # Baseline is the raw heuristic completion from this exact state.  Reward
        # is subtract-one-and-clip: heuristic-or-worse gets zero, better gets >0.
        baseline_state = root_state.clone(keep_assignments=False)
        baseline_makespan = self._rollout_to_terminal(problem, baseline_state, first_action=None, actor=actor)
        if not math.isfinite(baseline_makespan):
            return self._greedy_action(problem, root_state, actor, legal)

        action_values: Dict[Action, List[float]] = {a: [] for a in legal}
        for action in legal:
            self._stats.root_actions_evaluated += 1
            for _ in range(self.options.complete_rollouts_per_action):
                child = root_state.clone(keep_assignments=False)
                makespan = self._rollout_to_terminal(problem, child, first_action=action, actor=actor)
                reward = self._terminal_reward(baseline_makespan, makespan)
                action_values[action].append(reward)
                self._stats.complete_rollouts += 1

        greedy = self._greedy_action(problem, root_state, actor, legal)

        def key(a: Action) -> Tuple[float, float, float]:
            vals = action_values[a]
            mean = sum(vals) / len(vals) if vals else 0.0
            # Tie-break by heuristic prior and then greedy identity.
            return (mean, self._prior_score(problem, root_state, actor, a), 1.0 if a == greedy else 0.0)

        best = max(legal, key=key)
        # If no rollout beats the heuristic baseline, preserve the heuristic.
        vals = action_values[best]
        if not vals or max(vals) <= self.epsilon:
            return greedy
        return best

    def _rollout_to_terminal(
        self,
        problem: HeuristicPlanningProblem,
        state: _CommitmentState,
        *,
        first_action: Optional[Action],
        actor: Optional[str],
    ) -> float:
        self._stats.rollout_calls += 1
        transitions = 0
        if first_action is not None:
            assert actor is not None
            self._apply_action(problem, state, actor, first_action, record=False)
            transitions += 1
        key = self._rollout_state_key(problem, state)
        cached = self._rollout_terminal_cache.get(key)
        if cached is not None:
            return cached
        while transitions <= self.options.rollout_transition_limit:
            self._normalize_all_cursors(problem, state)
            self._update_done(problem, state)
            if state.done_time is not None:
                self._rollout_terminal_cache[key] = state.done_time
                return state.done_time
            j = self._next_actor(problem, state)
            if j is None:
                self._rollout_terminal_cache[key] = math.inf
                return math.inf
            legal = self._legal_actions(problem, state, j, include_idle_when_work_legal=self.options.allow_idle_when_work_legal)
            action = self._greedy_action(problem, state, j, legal)
            self._apply_action(problem, state, j, action, record=False)
            transitions += 1
        self._rollout_terminal_cache[key] = math.inf
        return math.inf

    def _rollout_state_key(
        self,
        problem: HeuristicPlanningProblem,
        state: _CommitmentState,
    ) -> Tuple[object, ...]:
        return (
            tuple(round(state.cursor[j], 7) for j in problem.resources),
            tuple(round(state.rem.get((i, p), 0.0), 7) for i, p in self._required_pairs),
            tuple(
                None if state.finish_time[p] is None else round(state.finish_time[p], 7)
                for p in problem.processes
            ),
            tuple(round(state.last_work_end[p], 7) for p in problem.processes),
            tuple(state.last_commitment.get(j) for j in problem.resources),
        )

    def _terminal_reward(self, baseline_makespan: float, rollout_makespan: float) -> float:
        if not math.isfinite(rollout_makespan) or rollout_makespan <= self.epsilon:
            return 0.0
        raw = baseline_makespan / rollout_makespan - 1.0
        return max(0.0, min(self.options.reward_clip_max, raw))

    # ------------------------------------------------------------------
    # Result conversion

    def _state_to_result(
        self,
        problem: HeuristicPlanningProblem,
        state: _CommitmentState,
        *,
        method: str = "commitment-window-mcts",
    ) -> HeuristicSolveResult:
        if state.done_time is None:
            raise HeuristicInfeasibleError("state is not terminal")
        assignments = state.assignments or {}
        role_assignments, resource_role_assignments, resource_process_assignments, h, u = self._decode_assignments(problem, assignments)
        timings: Dict[str, ProcessTiming] = {}
        for p in problem.processes:
            es = state.start_time.get(p)
            ef = state.finish_time.get(p)
            sb = self._time_to_bucket(problem, es)
            fb = self._time_to_bucket(problem, ef)
            timings[p] = ProcessTiming(
                process=p,
                es=es,
                ef=ef,
                ls=es,
                lf=ef,
                slack=0.0 if es is not None else None,
                finish_slack=0.0 if ef is not None else None,
                start_bucket=sb,
                finish_bucket=fb,
                latest_start_bucket=sb,
                latest_finish_bucket=fb,
            )
        binding_resources, binding_roles = self._binding_times(problem, h, u)
        critical_processes, critical_edges, critical_path = self._criticality(problem, timings)
        notes = (
            "Commitment-window planner: each action fills the selected resource-process-role forward "
            "until the role requirement is met or the current consecutive availability window ends.",
            f"searched_decisions={self._stats.searched_decisions}, root_actions_evaluated={self._stats.root_actions_evaluated}, "
            f"complete_rollouts={self._stats.complete_rollouts}, rollout_calls={self._stats.rollout_calls}, "
            f"commitment_actions={self._stats.commitment_actions}, runtime_seconds={self._stats.runtime_seconds:.6f}.",
        )
        return HeuristicSolveResult(
            status="feasible",
            objective_makespan=float(state.done_time),
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
            method=method,
            iterations=1,
            notes=notes,
        )

    def _time_to_bucket(self, problem: HeuristicPlanningProblem, value: Optional[float]) -> Optional[int]:
        if value is None:
            return None
        return self._calendar().previous_bucket_for_boundary(value)


# ---------------------------------------------------------------------------
# Counterexample construction and benchmark helpers


def make_single_day_context_counterexample() -> HeuristicPlanningProblem:
    """Small instance where the CPM-like commitment heuristic is provably wrong.

    Resources:
      F  : flexible, can perform X and Y on three separate working days.
      SX : specialist, can perform X only on day 1.

    Processes:
      A requires 8h of X and unlocks C.
      C requires 8h of Y after A.
      B requires 8h of Y and has no successor.

    If F greedily follows the precedence chain and does A-X on day 1, SX has
    nothing useful to do and the project finishes on day 3.  The better schedule
    is F -> B-Y on day 1, SX -> A-X on day 1, then F -> C-Y on day 2.
    """
    roles = ["X", "Y"]
    resources = ["F", "SX"]  # tie-breaking intentionally lets F choose first
    processes = ["A", "B", "C"]
    # Three 8-hour working windows separated by overnight gaps.
    starts = [0, 1, 2, 3, 4, 5, 6, 7, 24, 25, 26, 27, 28, 29, 30, 31, 48, 49, 50, 51, 52, 53, 54, 55]
    buckets = list(range(len(starts)))
    bucket_start = {b: float(starts[b]) for b in buckets}
    bucket_end = {b: float(starts[b] + 1) for b in buckets}
    bucket_hours = {b: 1.0 for b in buckets}
    requirements: Dict[Key2, float] = {}
    for p in processes:
        for i in roles:
            requirements[(i, p)] = 0.0
    requirements[("X", "A")] = 8.0
    requirements[("Y", "B")] = 8.0
    requirements[("Y", "C")] = 8.0
    availability: Dict[Tuple[str, str, int], float] = {}
    resource_capacity: Dict[Tuple[str, int], float] = {}
    for b in buckets:
        # F works all three days and can do X or Y.
        resource_capacity[("F", b)] = 1.0
        availability[("F", "X", b)] = 1.0
        availability[("F", "Y", b)] = 1.0
        # SX only works day 1 and can only do X.
        day1 = starts[b] < 8
        resource_capacity[("SX", b)] = 1.0 if day1 else 0.0
        availability[("SX", "X", b)] = 1.0 if day1 else 0.0
        availability[("SX", "Y", b)] = 0.0
    return HeuristicPlanningProblem(
        roles=roles,
        resources=resources,
        processes=processes,
        buckets=buckets,
        requirements=requirements,
        availability=availability,
        predecessors={"A": [], "B": [], "C": ["A"]},
        bucket_hours=bucket_hours,
        resource_capacity=resource_capacity,
        bucket_start=bucket_start,
        bucket_end=bucket_end,
        earliest_start={p: 0.0 for p in processes},
    )


def write_commitment_result_csvs(
    result: HeuristicSolveResult,
    output_dir: str | Path,
    *,
    metadata: Optional[ProjectMetadata] = None,
) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "schedule_report.md").write_text(summarize_result(result, metadata), encoding="utf-8")
    with (out / "resource_process_assignments.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["resource", "role", "process", "bucket", "bucket_start_h", "bucket_end_h", "hours"])
        for a in result.resource_process_assignments:
            w.writerow([a.resource, a.role, a.process, a.bucket, a.bucket_start, a.bucket_end, a.hours])
    with (out / "process_timings.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["process", "ES_h", "EF_h", "LS_h", "LF_h", "slack_h"])
        for p, tm in sorted(result.process_timings.items()):
            w.writerow([p, tm.es, tm.ef, tm.ls, tm.lf, tm.slack])

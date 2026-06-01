# RCPSP Commitment Scheduler

The active ProjDash `mcts` resource schedule backend routes through
`src/projdash/engine/resource_schedule_commitment.py`. That adapter translates
the richer ProjDash read model into the compact RCPSP planning model in
`src/rcpsp_mathprog_package`, runs the commitment-window MCTS planner, and
converts assignments back into ProjDash process rows, allocation slices,
critical-path diagnostics, and warnings.

## Model Shape

The RCPSP package works over compact tensors:

- tasks are processes;
- requirements are `(role, process) -> hours`;
- availability is `(resource, role, bucket) -> fraction`;
- resource capacity is `(resource, bucket) -> fraction`;
- time buckets have numeric start and end offsets;
- process-role pins express resource-owned work with forecast finishes.

ProjDash keeps the operational application model around that core:

- processes have statuses, dependencies, blockers, earliest starts, and delay
  rules;
- role requirements can have allocation policies, daily min/max rules,
  required resource counts, and process-role pins;
- resources have calendars, holidays, costs, active state, and role sets;
- calendars expand to timezone-aware capacity buckets;
- allocation slices and diagnostics must round-trip into the service and UI.

The commitment adapter keeps blocker metadata out of planning. It rejects
ProjDash-only constraints that are not represented in the compact RCPSP model,
such as dependency delay constraints, contiguous allocation requirements, and
daily min/max allocation limits; those inputs must explicitly use the greedy
backend instead.

## Commitment Heuristic

`CommitmentMCTSPlanner.solve_raw_heuristic` uses resource-cursor commitment
actions rather than one action per resource-hour bucket. Each resource owns a
cursor on its own calendar. A legal action assigns that resource to a
`(process, role)` pair from `max(current decision time, resource cursor)`
forward until either:

- the process-role requirement is complete;
- the current consecutive availability window for that resource-role ends;
- the horizon ends.

This captures the no context switching within a single work session principle.
The resource can choose a new task immediately when the current process-role
requirement is complete, but otherwise it keeps the focus through the current
contiguous work window.

The raw heuristic scores legal actions using downstream pressure, role scarcity,
remaining work, multi-role progress lag, continuity, and a penalty for consuming
flexible resources. The package still exposes the older
`ForwardBackwardHeuristicPlanner` for standalone experiments, but the active
ProjDash planning path uses the commitment-window MCTS scheduler.

## MCTS

`mcts` enables root-action rollout search on top of the same commitment
transition rules. At each searched decision the planner:

1. Computes the raw heuristic terminal makespan from the current state.
2. Evaluates every legal root action.
3. Runs exactly 10 complete terminal rollouts per legal root action.
4. Scores each rollout with positive clipped relative improvement:

   `max(0, min(1, heuristic_makespan / rollout_makespan - 1))`

5. Keeps the raw heuristic action if no rollout improves on the heuristic.

Rollouts always simulate to terminal completion unless the transition guard is
hit. Because commitment actions consume whole work windows, rollout length is
proportional to commitment blocks and idle jumps rather than hourly buckets.

## Counterexample

The regression counterexample lives in
`src/rcpsp_mathprog_package/rcpsp_commitment_mcts.py` and is benchmarked by
`src/rcpsp_mathprog_package/benchmark_commitment_counterexample.py`.

- Resources: `F` is flexible and can perform `X` or `Y` over three working
  days; `SX` is an `X` specialist available only on day 1.
- Processes: `A` needs 8h of `X`; `B` needs 8h of `Y`; `C` needs 8h of `Y` and
  depends on `A`.
- Raw failure: the CPM-like heuristic assigns `F` to `A-X` first because `A`
  has downstream work. That strands `SX` and pushes one `Y` task to day 3.
- Better schedule: assign `F` to `B-Y` on day 1, let `SX` do `A-X` on day 1,
  then let `F` do `C-Y` on day 2.

The exact commitment search proves an optimal makespan of 32 hours. The raw
commitment heuristic produces 56 hours, while commitment MCTS with 10 complete
rollouts per legal root action reaches the exact 32 hour optimum.

ProjDash projects the same case in
`tests/test_resource_schedule.py::test_projdash_commitment_counterexample_projection_is_exhaustively_proven`.
That test verifies the compact translated problem has the same exact optimum
and that the active `mcts` backend returns the matching schedule.

## Integration Direction

The active backend flow is:

1. Convert ProjectDashboard processes, dependencies, calendars, role
   requirements, blockers, earliest starts, and process-role pins into a compact
   `HeuristicPlanningProblem`.
2. Use commitment-window terminal rollout search for `mcts`, with the greedy
   heuristic as the rollout prior.
3. Convert resource-process-role assignments back into allocation slices,
   process rows, critical path diagnostics, and warnings.
4. Reject inputs that require greedy-only scheduling semantics so the caller
   chooses that backend explicitly.

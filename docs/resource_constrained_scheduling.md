# Resource-Constrained Scheduling

Resource-aware scheduling is driven by process role effort hours and resource
capacity. Processes do not carry target-date fields. The schedule is calculated
from dependencies, lifecycle anchors, earliest-start constraints, role
requirements, resource calendars, holidays, and availability.

## Core Invariants

- All schedule inputs and outputs use timezone-aware datetimes.
- Project `start_at` roots the graph unless process-role pins create a start
  anchor.
- Process `started_at` is derived from the first process-role pin;
  resource-aware schedule windows are computed from the current allocation plan.
- `earliest_start_at` is a not-before constraint.
- Process work is represented as role effort hours.
- Resources can fill one or more roles and use one calendar.
- Calendars define local working windows in their own timezone.
- Resource holidays are interpreted in the assigned calendar timezone.
- Resource cost currency is the project currency.
- Calendars recur indefinitely. There is no operator-supplied schedule horizon
  for schedule, graph, utilization, cost, or slippage computations.
- A resource can focus on only one process in each resource-hour bucket. Within
  that bucket, the resource may split capacity across multiple role requirements
  for the same process.
- For the commitment-window MCTS backend, a resource keeps working on the
  chosen process-role through the current contiguous availability window, or
  until the requirement is complete. That prevents context switches inside a
  single work session.
- Requirements that explicitly set `allocation_policy` to `contiguous` still
  require one uninterrupted sequence through the selected resource's working
  calendar. They cannot bridge over an intervening working bucket used by
  another process.

## Allocation Model

The scheduler allocates ready role requirements into project-time hour buckets.
For every bucket, it uses eligible resources that have capacity in their local
calendar and can fill the demanded role. Allocation must not exceed a resource's
available bucket capacity or assign the same resource bucket to more than one
process. The capacity search window is internal: if required work does not fit
in the current window, the solver extends the recurring calendars forward and
retries until the work is complete or a permanent configuration error is found.
When a resource can fill multiple ready role requirements, cup filling plans the
role with the least remaining eligible capacity first. Roles tied on remaining
capacity continue to share the bucket by the water-fill policy. If scarce-role
demand does not consume the full bucket, remaining capacity continues to the
next least available role for the same focused process.

The collapsed schedule evidence records:

- process ready/start/end datetimes
- allocation state
- allocation slices by process, role, resource, and bucket
- utilization by resource, role, and time
- costs by resource, role, process, and time

## Resource Windows and Sensitivity

Dependency-only CPM remains diagnostic metadata for dependency-only queries. In
resource-aware scheduling, actual `starts_at` and `ends_at` are the first and
last resource assignment buckets for a process. A schedule window starts after
all parent processes are planned to finish, plus any explicit
`earliest_start_at`; it ends at the earliest planned child start, or projected
project completion for terminal work. Schedule buffer is the window width minus
the actual planned elapsed duration.

Resource-aware completion is authoritative for committed slippage points. There
is no resource-aware critical path field in the commitment scheduler.
Sensitivity replaces criticality: add one role-hour to a process-role, recompute
the schedule, and record the projected makespan delta. Sensitivity computations
can be dispatched across a process pool because each perturbation is
independent.

## Lifecycle

- `planned` clears lifecycle anchors.
- `in_progress` and `paused` are lifecycle statuses; they do not create a
  started anchor without a process-role pin.
- `done` sets `finished_at` from verified process-role pins. Unpinning deletes
  the pin forecast, verified finish, and verified work evidence represented by
  that pin.
- Done processes are historical anchors: current role, resource, and calendar
  availability changes do not make them infeasible, though dependency readiness
  is still reported for diagnostics.
- `canceled` preserves an existing start anchor but does not create completion.
- Blockers do not change computed schedule timing; they are visible through
  blocker summaries and resolver dependencies for review and prioritization.
- Permanent resource infeasibility is reported as a structured
  `resource_schedule_unsatisfiable` query error. Diagnostics distinguish missing
  active roles, missing eligible resources, calendars with no recurring
  capacity, and unavailable contiguous windows.

## Slippage

`commit_project_state` persists a schedule snapshot only when the operator
commits. Each snapshot stores the commit timestamp, selected terminal symbols,
schedule basis, completion datetime, convergence state, and optional note. The
UI plots these snapshots as slippage history.

## Topology

`replace_process_with_subgraph` retires one or more selected processes and
creates a replacement subgraph. Original external predecessors connect to new
roots; new leaves connect to original external children.

`collapse_subgraph` retires many selected processes and creates one replacement
process. When replacement role requirements are omitted, collapsed role effort
is inferred from the selected subgraph.

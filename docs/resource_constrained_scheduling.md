# Resource-Constrained Scheduling

Resource-aware scheduling is driven by process role effort hours and resource
capacity. Processes do not carry target-date fields. The schedule is calculated
from dependencies, lifecycle anchors, earliest-start constraints, role
requirements, resource calendars, holidays, and availability.

## Core Invariants

- All schedule inputs and outputs use timezone-aware datetimes.
- Project `start_at` roots the graph unless a process has a `started_at` anchor.
- A started process has `ES == LS == started_at`.
- `earliest_start_at` is a not-before constraint.
- Process work is represented as role effort hours.
- Resources can fill one or more roles and use one calendar.
- Calendars define local working windows in their own timezone.
- Resource holidays are interpreted in the assigned calendar timezone.
- Resource cost currency is the project currency.
- Calendars recur indefinitely. There is no operator-supplied schedule horizon
  for schedule, graph, utilization, cost, or slippage computations.
- A resource may carry concurrent fractional allocations, but once a resource
  starts work on a process requirement it must continue through that resource's
  working-calendar time until that requirement contribution is complete. Nights,
  weekends, holidays, and other zero-capacity periods do not break continuity.

## Allocation Model

The scheduler allocates ready role requirements into project-time hour buckets.
For every bucket, it uses eligible resources that have capacity in their local
calendar and can fill the demanded role. Allocation must not exceed a resource's
available bucket capacity. The capacity search window is internal: if required
work does not fit in the current window, the solver extends the recurring
calendars forward and retries until the work is complete or a permanent
configuration error is found.

The collapsed schedule evidence records:

- process ready/start/end datetimes
- allocation state
- allocation slices by process, role, resource, and bucket
- utilization by resource, role, and time
- costs by resource, role, process, and time

## Critical Path

Dependency-only CPM remains diagnostic metadata for graph windows:

- `ES`: earliest start
- `EF`: earliest finish
- `LS`: latest start
- `LF`: latest finish
- slack hours
- criticality label

Resource-aware completion is authoritative for committed slippage points.
The solver must produce at least one resource-aware critical path. If slack work
cannot be placed before its latest feasible finish with available resource
capacity, that work pushes descendants forward and may become critical on the
next iteration.

## Lifecycle

- `planned` clears lifecycle anchors.
- `in_progress` and `paused` set `started_at` when it is missing.
- `done` sets `finished_at`; if no start anchor exists, finish time also anchors
  start.
- Done processes are historical anchors: current role, resource, and calendar
  availability changes do not make them infeasible, though dependency readiness
  is still reported for diagnostics.
- `canceled` preserves an existing start anchor but does not create completion.
- Blockers do not change computed schedule timing; they mark processes as blocked
  for status, review, and prioritization.
- Permanent resource infeasibility is reported as a structured
  `resource_schedule_unsatisfiable` query error. Diagnostics distinguish missing
  active roles, missing eligible resources, calendars with no recurring
  capacity, and continuity constraints.

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

# Resource-Constrained Scheduling Ticket

## Objective

Implement and maintain resource-aware project scheduling where process work is
defined by role effort hours and resource capacity, not target-date facts.

## Current Scope

- Validate all command/query datetimes as timezone-aware.
- Persist projects, process revisions, dependencies, roles, resources,
  calendars, holidays, blockers, lifecycle anchors, aliases, topology rewrites,
  and committed schedule snapshots.
- Compute dependency-only CPM windows as diagnostic graph metadata.
- Compute resource-aware schedules from role effort and resource capacity.
- Expose allocation slices, utilization, costs, capacity inspection, and
  structured infeasibility errors.
- Commit immutable schedule snapshots for slippage history only on explicit
  operator command.

## Scheduling Requirements

- `start_at` roots the project.
- `started_at` pins a process start so `ES == LS == started_at`.
- `earliest_start_at` remains a not-before constraint.
- Calendar windows are local to each calendar timezone.
- Resource holidays use the resource calendar timezone.
- Allocation must not exceed resource bucket capacity.
- Calendars recur indefinitely; schedule computations do not accept public
  horizon bounds.
- Resource-process assignments are continuous through each resource's working
  calendar time once started.
- Role and resource utilization must reconcile with allocation slices.
- Costs use the project currency.

## Topology Requirements

- Add/remove dependencies in batch and reject implied cycles atomically.
- Replace any selected process subgraph with a supplied replacement subgraph.
- Infer replacement roots and leaves from internal topology when omitted.
- Collapse a selected subgraph into one process while conserving role effort.
- Rename process symbols and maintain aliases while preserving uniqueness.

## UI Requirements

- Prefer dropdowns and table selections over free text for existing database
  facts.
- Process creation is separate from process modification.
- Modification supports batch-selected processes and pre-fills aggregated
  predecessor, child, status, lifecycle, blocker, and role-effort state.
- Schedule view includes Gantt windows and role/resource priority tables.
- Resource view includes resource and role utilization heatmaps.
- Slippage view commits and plots snapshot history.

## Test Requirements

- Keep service tests first: validation, scheduling invariants, topology rewrites,
  persistence round trips, and query shapes.
- Add UI adapter tests for aggregation, allowed topology choices, role pattern
  parsing, priority sorting, and datetime formatting.
- Review implementation with strict checks for accidental state leakage, cycle
  creation, resource over-allocation, and non-aware datetimes.

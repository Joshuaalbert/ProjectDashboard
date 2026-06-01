# Committed Schedule Snapshots

## Intent

Project schedules can be explored freely, but slippage history should only move
when an operator explicitly commits the current planning state. A committed state
records the completion datetime calculated from the current project graph,
resources, roles, process lifecycle facts, blockers, and selected terminal
processes.

## Scheduling Rules

- The resource-aware schedule is authoritative for committed slippage points.
  It is derived from resource calendars, role effort, lifecycle anchors, and
  dependency readiness. Dependency-only CPM is only graph diagnostic metadata.
- The schedule is rooted at the project `start_at`.
- When a process has process-role pins, the first pin is its actual start. For
  resource-aware scheduling, pinned process-role finishes come from resource
  forecasts or verified finish datetimes; planned process-role finishes are
  derived from role effort on resource-hour buckets.
- Resource-aware arithmetic is in timezone-aware hour buckets, not business days.
  A useful implementation model is to expand each process role requirement into
  virtual resource-hour bucket demand, sweep project-time buckets breadth-first
  across all ready requirements, consume the shared resource ledger while
  respecting every resource calendar/timezone/holiday, then collapse the virtual
  evidence back into process, role, and resource metadata. The service API should
  expose the collapsed process graph and allocation evidence, not the virtual
  nodes.
- Process `duration_business_days` is not an input to resource-aware finish
  arithmetic. `inferred_duration_hours` is the collapsed active scheduled time
  implied by fulfilled resource-hour buckets; start/end datetimes still preserve
  the wall-clock placement across nights, weekends, and holidays.
- `started_at` is derived from process-role pins, not from entering
  `in_progress` or `paused`.
- Marking a process `done` requires verified finished process-role pins.
- Reopening or unpinning clears the derived process lifecycle facts that came
  from those pins.

## Slippage Points

- `commit_project_state` computes the current schedule for the requested terminal
  symbols and persists one immutable schedule snapshot.
- Empty terminal symbols mean whole-project completion.
- Non-empty terminal symbols use the induced ancestor subgraph, matching the UI's
  completion-target behavior.
- Each snapshot stores `committed_at`, terminal symbols, schedule basis,
  completion datetime, and convergence state from the resource-aware schedule.
- Repeating a commit for the same project, `committed_at`, and terminal symbol set
  is idempotent and returns the original snapshot id.

## UI

The Slippage tab is backed by committed snapshots only. It can commit the current
state, plot committed completion history, and load a historical commit timestamp
into the sidebar `as_of` controls for review or follow-on edits.

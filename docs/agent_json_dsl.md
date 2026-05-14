# Agent JSON DSL

This document describes the current service contract for agents. The service is
the authority for validation, persistence, graph rewrites, schedule projection,
resource allocation, utilization, cost summaries, and committed slippage
snapshots.

## Principles

- All datetime fields are timezone-aware and use `*_at` names.
- Processes do not have target-date fields. Schedule pressure is derived from
  dependency/resource completion windows: `ES`, `EF`, `LS`, and `LF`.
- Process work is expressed as role effort hours. Resource-aware scheduling
  allocates those role hours into resource-hour buckets using resource
  calendars, holidays, availability, roles, and capacity.
- A resource can focus on only one process in a resource-hour bucket, but may
  switch process context between adjacent hour buckets.
- `allocation_policy: "contiguous"` is the explicit exception: that requirement
  must use one uninterrupted sequence on a selected resource's working calendar.
- When multiple ready role requirements can use the same resource bucket, cup
  filling prioritizes the role with the least remaining eligible capacity, then
  passes residual bucket capacity to the next least constrained role.
- `earliest_start_at` is a valid not-before constraint.
- `started_at` pins a process start: `ES == LS == started_at`.
- `commit_project_state` records a point in slippage history. Exploration before
  commit does not create a historical point.

## Command Envelope

Commands are wrapped as:

```json
{
  "command_id": "00000000-0000-4000-8000-000000000001",
  "command": {
    "action": "create_project",
    "name": "Project",
    "start_at": "2026-05-13T09:00:00+00:00"
  }
}
```

Supported command actions are:

- `create_project`
- `update_project`
- `delete_project`
- `set_project_default_currency`
- `upsert_process_revision`
- `set_process_status`
- `commit_project_state`
- `add_blocker`
- `resolve_blocker`
- `rename_process`
- `add_process_aliases`
- `batch_update_process_graph`
- `replace_process_with_subgraph`
- `collapse_subgraph`
- `create_role`
- `rename_role`
- `deactivate_role`
- `upsert_resource_calendar`
- `set_calendar_active`
- `add_calendar_exception`
- `remove_calendar_exception`
- `upsert_resource`
- `set_resource_active`
- `set_resource_roles`
- `set_resource_calendar`

## Process Revisions

`upsert_process_revision` creates a process when no identity is supplied, or
appends a new planning revision for an existing process.

Important fields:

- `project_id`
- `process_symbol` or `process_id` for updates
- `name`
- `description`
- `effective_at`
- `dependencies`
- `earliest_start_at`
- `role_requirements`

Each `role_requirements` item has:

- `role_id`
- `effort_hours`
- optional allocation policy and daily allocation bounds

## Topology Rewrites

`replace_process_with_subgraph` accepts `process_symbols` or `process_ids` for
the selected subgraph to retire, plus replacement processes and internal
dependencies. The service infers new roots and leaves when omitted. External
predecessors connect to replacement roots; replacement leaves connect to external
children.

`collapse_subgraph` accepts many existing process symbols and creates one
replacement process. If replacement role requirements are omitted, the service
infers a collapsed requirement set from the selected subgraph.

## Queries

Queries are wrapped as:

```json
{
  "query": {
    "action": "query_process_graph",
    "project_id": "project",
    "as_of": "2026-05-13T09:00:00+00:00",
    "now": "2026-05-13T09:00:00+00:00"
  }
}
```

Supported query actions are:

- `get_project`
- `query_projects`
- `query_project_catalog`
- `query_schedule`
- `query_critical_path`
- `query_process_graph`
- `query_blockers`
- `query_schedule_snapshots`
- `query_resource_schedule`
- `query_resource_capacity`
- `query_utilization`
- `query_costs`

`query_process_graph` returns process nodes with dependency-only schedule fields
and, when requested, resource-aware fields plus allocation slices. Resource-aware
schedule, graph, utilization, and cost queries do not accept an
operator-supplied schedule horizon; the service computes and extends its
capacity search span from project anchors, terminal scope, dependencies, role
effort, and recurring resource calendars. `query_resource_capacity` is the
inspection endpoint that accepts explicit `horizon_starts_at` and
`horizon_ends_at` values.

If a resource-aware schedule cannot ever be solved because required roles,
eligible resources, or recurring calendar capacity are missing, the query
returns a structured `resource_schedule_unsatisfiable` error rather than a
partial schedule.

## Slippage

`commit_project_state` computes the resource-aware schedule for the selected
terminal symbols and persists an immutable snapshot with:

- `committed_at`
- terminal symbols
- schedule basis
- completion datetime
- convergence status
- optional note

Repeated commits for the same project, timestamp, and terminal symbol set are
idempotent.

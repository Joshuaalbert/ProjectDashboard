# ProjDash API Reference for Project-Manager Agents

Load this reference when issuing ProjDash commands or queries.

## Service Access

Use the validated Python service API when working inside a repo or environment
that has `projdash` installed:

```python
from projdash.service.commands import CommandEnvelope
from projdash.service.ladybug_repository import LadybugProjectRepository
from projdash.service.queries import QueryEnvelope
from projdash.service.service import ProjectService

repo = LadybugProjectRepository("projdash.lbug")
repo.initialize_schema()
service = ProjectService(repo)

query_result = service.handle_query(QueryEnvelope.model_validate(query_payload))
command_result = service.handle_command(CommandEnvelope.model_validate(command_payload))
```

In the ProjectDashboard development repo, run Python through:

```bash
conda run -n projdash_py python <script.py>
```

Use `query_project_catalog` first when ids are unknown. It returns project-owned
roles, resources, calendars, processes, aliases, and blockers for dropdown-like
selection.

## Hard Invariants

- All API datetimes are timezone-aware and use `*_at` names.
- Processes do not have due dates. Schedule pressure comes from `ES`, `EF`,
  `LS`, `LF`, critical path, blockers, and slippage snapshots.
- Schedules have no user-supplied horizon. Recurring resource calendars extend
  indefinitely.
- Process effort is stored as `role_requirements`: one row per role with
  whole-number `effort_hours`.
- A resource can fill one process per resource-hour bucket, may switch between
  adjacent buckets, and may hold multiple roles. Resource-hour allocation is
  binary: 0 or 1, never fractional.
- A resource's `calendar_id` is its default unbounded calendar. Use
  `calendar_overrides` on the resource for bounded replacement calendar rules,
  such as a different August or September availability pattern.
- Blockers do not delay the schedule. They affect PM prioritization and status.
- `earliest_start_at` is a not-before constraint.
- `started_at` pins a process start: `ES == LS == started_at`.
- `done` processes are historical anchors and must have `finished_at`.
- `commit_project_state` is what creates a slippage-history point.

## Query Patterns

Agent context, the default starting point:

```json
{
  "query": {
    "action": "query_agent_context",
    "project_id": "project_id",
    "as_of": "2026-05-14T09:00:00+00:00",
    "now": "2026-05-14T09:00:00+00:00",
    "terminal_process_symbols": ["optional-terminal-symbol"],
    "snapshot_limit": 5
  }
}
```

The response includes `project`, `summary`, `graph`, `schedule`, `slippage`,
`prioritized_work`, `blockers`, and `available_queries`. `prioritized_work`
contains `by_role` and `by_resource` groups. Each priority process includes the
priority label, process symbol and name, latest-start timing, effort hours, and
status fields needed for action planning.

Use the JSON response as the canonical state for command construction. Markdown
context summaries should be generated from this JSON for briefing and hand-off;
Markdown is more expressive for explaining why work is urgent, which calendars
apply, and how slippage changed, but it should remain a derived view rather
than the source used to build mutation payloads.

Recommended Markdown context summary sections:

- Snapshot: project id, `as_of`, `now`, scope, terminal targets, completion,
  status counts, blocked count, and schedule convergence.
- Critical path.
- Role priorities and resource priorities, grouped by entity.
- Schedule watchlist with LS/end times, slack, allocation state, and status.
- Open blockers.
- Resource calendar rules, including default calendars and bounded overrides.
- Follow-up queries available for deeper evidence.

Use narrower queries for evidence:

- `query_process_graph`: dependency graph and optional resource-aware fields.
- `query_resource_schedule`: allocation slices and inferred schedule.
- `query_utilization`: resource and role utilization.
- `query_costs`: cost by resource, role, process, or time.
- `query_schedule_snapshots`: committed slippage history.
- `query_blockers`: unresolved or resolved blockers.
- `query_project_catalog`: ids and current facts for safe command construction.

Common scope forms:

```json
{"type": "project"}
{"type": "target_process", "process_symbol": "process-symbol"}
{
  "type": "topo_filter",
  "root_process_symbols": ["terminal-symbol"],
  "direction": "ancestors"
}
```

## Command Envelope

Every mutation uses:

```json
{
  "command_id": "00000000-0000-4000-8000-000000000001",
  "command": {"action": "..."}
}
```

Use a fresh UUID per intended command. Reusing a `command_id` with a different
payload is an idempotency conflict.

## Project, Roles, Calendars, Resources

Create or update core capacity before adding process effort that depends on it:

```json
{"action": "create_role", "project_id": "project_id", "role_id": "role_pm", "name": "PM"}
```

```json
{
  "action": "upsert_resource_calendar",
  "project_id": "project_id",
  "calendar_id": "cal_josh_cet",
  "name": "Josh CET",
  "timezone": "Europe/Paris",
  "weekly_windows": [
    {"weekday": 0, "start_local_time": "10:00", "end_local_time": "18:00", "capacity_hours": 8},
    {"weekday": 1, "start_local_time": "10:00", "end_local_time": "18:00", "capacity_hours": 8}
  ],
  "active": true
}
```

```json
{
  "action": "upsert_resource",
  "project_id": "project_id",
  "resource_id": "res_josh",
  "name": "Josh",
  "role_ids": ["role_pm", "role_lead"],
  "calendar_id": "cal_josh_cet",
  "available_from_at": "2026-05-14T10:00:00+02:00",
  "cost_rate": "100",
  "cost_unit": "hour",
  "cost_currency": "USD",
  "calendar_overrides": [
    {
      "rule_id": "august-2026",
      "calendar_id": "cal_josh_august_cet",
      "starts_at": "2026-08-01T00:00:00+02:00",
      "ends_at": "2026-09-01T00:00:00+02:00",
      "reason": "Temporary August availability pattern from planning notes."
    }
  ],
  "holidays": [
    {
      "holiday_id": "holiday_2026_05_25",
      "starts_at": "2026-05-25T00:00:00+02:00",
      "ends_at": "2026-05-26T00:00:00+02:00",
      "reason": "Local holiday"
    }
  ],
  "active": true
}
```

## Process Revisions

Create or revise a process with role effort:

```json
{
  "action": "upsert_process_revision",
  "project_id": "project_id",
  "process_symbol": "existing-symbol",
  "name": "Implement service API",
  "description": "Done means validated JSON commands and queries cover PM workflows.",
  "effective_at": "2026-05-14T09:00:00+00:00",
  "dependencies": ["predecessor-symbol"],
  "earliest_start_at": null,
  "role_requirements": [
    {"role_id": "role_lead", "effort_hours": 8},
    {"role_id": "role_qa", "effort_hours": 4}
  ],
  "assumption_note": "Estimate reconciled from 2026-05-14 planning notes."
}
```

For a new process, omit both `process_id` and `process_symbol`; the service
creates the symbol. Use symbols or aliases for human-facing references after
creation.

## Status and Blockers

```json
{
  "action": "set_process_status",
  "project_id": "project_id",
  "process_symbol": "process-symbol",
  "status": "in_progress",
  "edit_at": "2026-05-14T09:00:00+00:00",
  "started_at": "2026-05-14T09:00:00+00:00",
  "note": "Started after kickoff."
}
```

```json
{
  "action": "add_blocker",
  "project_id": "project_id",
  "process_symbol": "process-symbol",
  "summary": "Need vendor approval",
  "details": "Approval was requested in the May 14 meeting.",
  "severity": "blocking",
  "created_at": "2026-05-14T09:00:00+00:00"
}
```

Resolve blockers with `resolve_blocker`.

## Dependencies and Batch Updates

Use `batch_update_process_graph` for dependency edits and resource assignment
edits that should succeed or fail atomically:

```json
{
  "action": "batch_update_process_graph",
  "project_id": "project_id",
  "edit_at": "2026-05-14T09:00:00+00:00",
  "operations": [
    {
      "action": "add_dependency",
      "predecessor_process_symbol": "api-design",
      "successor_process_symbol": "api-implementation"
    },
    {
      "action": "add_role_requirement",
      "process_symbol": "api-implementation",
      "requirement": {"role_id": "role_backend", "effort_hours": 6}
    }
  ]
}
```

The service rejects cycles and invalid cross-project references.

## Topology Rewrites

Expand one or more processes into a replacement subgraph:

```json
{
  "action": "replace_process_with_subgraph",
  "project_id": "project_id",
  "process_symbols": ["coarse-process"],
  "edit_at": "2026-05-14T09:00:00+00:00",
  "processes": [
    {
      "process_symbol": "detail-a",
      "name": "Detail A",
      "description": "Trackable first deliverable.",
      "role_requirements": [{"role_id": "role_lead", "effort_hours": 4}]
    },
    {
      "process_symbol": "detail-b",
      "name": "Detail B",
      "description": "Trackable second deliverable.",
      "role_requirements": [{"role_id": "role_qa", "effort_hours": 3}]
    }
  ],
  "dependencies": [
    {"predecessor_symbol": "detail-a", "successor_symbol": "detail-b"}
  ],
  "parent_alias_target_symbol": "detail-a"
}
```

Omit `root_symbols` and `leaf_symbols` unless the inferred topology would be
wrong. Collapse a connected subgraph when the details are no longer useful:

```json
{
  "action": "collapse_subgraph",
  "project_id": "project_id",
  "edit_at": "2026-05-14T09:00:00+00:00",
  "process_symbols": ["detail-a", "detail-b"],
  "new_process": {
    "name": "Consolidated deliverable",
    "description": "Collapsed from detail-a and detail-b."
  }
}
```

When `new_process.role_requirements` is omitted, the service infers collapsed
role effort from the selected subgraph.

## Commit Slippage Snapshot

Commit only after the reconciled state should become history:

```json
{
  "action": "commit_project_state",
  "project_id": "project_id",
  "committed_at": "2026-05-14T17:00:00+00:00",
  "terminal_process_symbols": [],
  "note": "Reconciled notes: kickoff.md, vendor-email.eml."
}
```

An empty terminal list means the default project completion basis. Use explicit
terminal symbols for a filtered completion date.

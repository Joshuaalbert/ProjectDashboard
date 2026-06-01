# Agent JSON DSL

This document describes the current service contract for agents. The service is
the authority for validation, persistence, graph rewrites, schedule projection,
resource allocation, utilization, cost summaries, and committed slippage
snapshots.

## Principles

- All datetime fields are timezone-aware and use `*_at` names.
- Processes do not have target-date fields. Resource-aware schedule pressure is
  derived from planned starts, planned finishes, schedule windows, schedule
  buffer, and makespan sensitivity.
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
- `started_at` is derived from process-role pins.
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
- `delete_process`
- `set_process_status`
- `commit_project_state`
- `upsert_milestone`
- `set_milestone_active`
- `add_blocker`
- `resolve_blocker`
- `set_blocker_resolution_owner`
- `reopen_blocker`
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
- `upsert_process_role_pin`
- `delete_process_role_pin`
- `upsert_slack_project_config`
- `update_slack_continuity_note`
- `set_resource_slack_user`
- `record_slack_collection_cursor`
- `store_slack_bot_token`
- `clear_slack_bot_token`
- `start_slack_run`
- `finish_slack_run`
- `create_slack_outbox_messages`
- `mark_slack_outbox_sent`
- `mark_slack_outbox_failed`
- `update_slack_outbox_body`
- `mark_slack_outbox_skipped`

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

Use `upsert_process_role_pin` when a teammate is pinned to a process-role and
has supplied a forecasted finish. Pins carry `resource_id`, `pinned_at`
(`pinned_started_at` in read models), `forecast_finish_at`, and become
`pinned_finished` only after resource verification (`verified_finished_at` in
read models).
Active pins reserve that resource from plannable capacity until the forecast
finish and make the pinned process-role finish at the forecast during planning.
Use `delete_process_role_pin` to unpin the process-role; deleting the pin also
removes its forecast, verified finish, and verified work evidence.

`delete_process` hard-deletes a process by `process_id` or `process_symbol`.
The service removes dependency references to that process from the remaining
graph. A blocker and its resolver process are deleted only when deleting the
process leaves the resolver with no remaining child processes.

## Milestones

`upsert_milestone` defines a named subset of process symbols for milestone
slippage tracking. The service resolves aliases to canonical active symbols and
stores `process_symbols`, `name`, `description`, `active`, and timestamps.
Inactive milestones are retained for audit but omitted from agent context.

`commit_project_state` accepts either `terminal_process_symbols` or
`milestone_id`. When `milestone_id` is supplied, the snapshot terminal symbols
come from that milestone. `query_schedule_snapshots` likewise accepts
`milestone_id` to fetch slippage history for that milestone.

Slack reconciliation agents should prefer milestone snapshots over whole-project
snapshots when a project has active milestones.

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
- `query_milestones`
- `query_schedule`
- `query_critical_path`
- `query_process_graph`
- `query_blockers`
- `query_schedule_snapshots`
- `query_resource_schedule`
- `query_agent_context`
- `query_resource_capacity`
- `query_utilization`
- `query_costs`
- `query_slack_project_config`
- `query_slack_bot_token`
- `query_slack_runs`
- `query_pending_slack_outbox`
- `query_slack_outbox`

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

`query_agent_context` returns a concise JSON report intended for project-manager
agents. It includes the resource-aware process graph, role requirements,
inferred process durations, makespan sensitivity, committed slippage summary,
milestones with their own slippage summaries, blockers, and role-prioritized
work. Agents should use the narrower queries above when they need detailed
schedule, utilization, cost, or capacity evidence.

The request accepts `project_id`, timezone-aware `as_of` and `now`, optional
`scope`, optional `terminal_process_symbols`, scheduler convergence options, and
`snapshot_limit`. When terminal symbols are provided without an explicit scope,
the context is scoped to those terminal nodes and their ancestors. Terminal
symbols may be aliases; the response includes both requested
`terminal_process_symbols` and resolved `canonical_terminal_process_symbols`.
Slippage lookup includes snapshots committed under either the requested aliases
or the resolved canonical terminal symbols. Top-level query warnings mirror
resource-schedule warnings, such as `max_iterations_reached`. The response is
stable JSON with these main sections: `project`, `summary`, `graph`, `schedule`,
`slippage`, `prioritized_work`, `milestones`, `blockers`, and
`available_queries`. Slippage summaries include `timeline` entries with
`commit_datetime` and `estimated_done_datetime`. Blocker rows include optional
`resolution_owner_resource_id`, immediately blocked process context, and derived
role/resource ids that need the blocker resolved.

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

## Slack Continuity

`update_slack_continuity_note` stores the handoff note that the next Slack run
will read even when no new Slack messages were collected. Slack runner output
requires this note as structured JSON, serialized into the existing storage
string. It must include:

- `teammate_theory_of_mind`: exactly one entry per mapped teammate, covering
  what they likely know, likely do not know, may be confused about, have been
  asked to do, and when a response/action is expected.
- `team_theory_of_mind`: team-wide shared knowledge, unknowns, context requests,
  alignment needs, expected team actions, and the configured project channel for
  the team entity when one exists.
- `pm_assessment`: exactly one direct answer for each PM checklist point 1
  through 18, plus the implication for message/no-message decisions.
- machine-readable `follow_up_plan` fields on teammate and team TOM entries:
  cadence, cadence reason, next follow-up time, do-not-message-before time,
  escalation time, and escalation channel.
- `commitment_ledger`, `raid_register`, `outbound_message_review`, and
  `cumulative_outstanding_items`.
- `next_run_focus`: what the next run should inspect first.

Ownership in commitments, outstanding items, and RAID entries is represented by
`owner_type` and `owner_id`; do not add a separate `owner` shorthand field.

Each teammate theory-of-mind entry, the team theory-of-mind entry, and each
`pm_assessment` item must include `evidence_recency`:

- `last_evidence_at`: the ISO timestamp of the newest evidence for that item,
  or `null` when there is no timestamped evidence.
- `last_evidence_note`: a brief note describing what that evidence was.
- `evidence_is_stale`: whether the project manager should refresh the evidence.
- `stale_if_no_update_by`: the time or threshold when the evidence becomes
  stale without a reply or state change.
- `refresh_by`: `dm`, `channel`, `either`, `none`, or `null`.
- `refresh_prompt`: the concrete question to ask if the evidence is stale.

When evidence is stale, refresh teammate-specific facts by direct DM and shared
context, coordination, or alignment facts in the team-wide project channel.

The Slack outbox supports two target types:

- `dm`: requires `slack_user_id` and may reference the mapped `resource_id`.
- `channel`: requires `slack_channel_id` and is used for the project team
  channel. Channel rows are reviewed and audited like DMs, but are not modeled
  as teammate messages.

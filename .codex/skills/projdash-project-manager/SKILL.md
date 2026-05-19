---
name: projdash-project-manager
description: Project-management operating procedure for using ProjDash as an agentic planning tool. Use when Codex is asked to reconcile meeting notes, emails, Slack exports, or teammate updates from unreconciled folders into a ProjDash project; update project topology, roles, resources, blockers, statuses, inferred estimates, calendars, slippage snapshots, or retrospective calibration; produce daily PM briefings, current action items, schedule-risk summaries, or agent-readable project context from the ProjDash service.
---

# ProjDash Project Manager

## Invocation Contract

The normal user prompt is expected to name an input location and a completed
location, for example:

`$projdash-project-manager from data/unreconciled putting in data/reconciled when done`

Treat those paths as the reconciliation contract. Read source material from the
`from` path, apply validated ProjDash updates, commit the intentional state when
requested or implied by reconciliation, then move fully incorporated files to
the `reconciled` path. The rest of the project context should come from this
skill, the API reference, and ProjDash queries, not from conversation memory.

## Operating Model

Treat ProjDash as the source of truth for project facts and schedule projections.
Use notes, transcripts, emails, and chat exports as evidence that should become
validated service commands, not free-form side summaries.

Prefer this loop:

1. Parse the `from` and `reconciled` paths from the user prompt.
2. Discover the workspace, database path, and project id.
3. Query the current project state with `query_agent_context`.
4. Extract proposed changes, inferred estimates, and explicit evidence from
   the notes.
5. Ask concise questions when facts are ambiguous or conflicting.
6. Apply validated service commands.
7. Re-query context, schedule, blockers, utilization, and slippage.
8. Compare projections against prior committed snapshots and recent actuals.
9. Commit the project state when the reconciled state is intentional.
10. Move incorporated source files to `reconciled/`.
11. Report the changes, risks, action items, slippage, and unresolved
    questions.

Read [references/projdash-api.md](references/projdash-api.md) before issuing
service commands or writing JSON envelopes.

## Fresh Context Bootstrap

When starting from a fresh context, build this inventory before mutating state:

- Source paths: unreconciled input files and reconciled output location.
- Service access: database path, project id, project timezone if known, and the
  `as_of`/`now` timestamp used for queries.
- Current context: `query_agent_context` JSON, including summary, graph,
  schedule, slippage, role priorities, resource priorities, blockers, and
  available follow-up queries.
- Catalog facts: roles, resources, calendars, calendar overrides, aliases, and
  existing blockers from `query_project_catalog` when ids are needed.
- Schedule evidence: resource schedule, utilization, capacity, costs, or
  snapshots only when the agent-context summary is not enough to justify the
  planned change.

If the project id is not named, list the project catalog. If exactly one
project exists, use it. If multiple projects are plausible, ask which project to
update before mutating.

`query_agent_context` JSON is the canonical machine-readable state for planning
and commands. A generated Markdown context summary is the preferred briefing
surface for humans and hand-offs because it can explain priorities, calendars,
slippage, and risks in prose. Do not treat Markdown as a replacement for the
JSON when constructing commands; it is a derived view.

## Reconcile Notes

Discover the project workspace, database path, and project id from the user
request, repo config, UI defaults, or current ProjDash project catalog. If more
than one project is plausible, ask which project to update before mutating.

Load every file in `unreconciled/` that looks like a note, transcript, email,
chat export, or teammate update. Preserve source attribution by filename and,
when available, speaker/date. Ignore already reconciled files unless the user
explicitly asks for a replay.

For each source, extract:

- Decisions that change scope, sequencing, ownership, resources, calendars, or
  effort.
- New deliverables or intermediate processes.
- Processes that should be expanded into a subgraph or collapsed into a coarser
  node.
- Updated role effort hours for each process.
- Evidence that prior estimates were too high or too low, including actual
  starts, finishes, rework, waiting time, or scope discovered after the estimate.
- New roles, resources, resource calendars, holidays, cost rates, and resource
  role assignments.
- Status changes: started, paused, done, canceled.
- Blockers and blocker resolutions.
- Earliest-start constraints.
- Assumption changes that should be recorded in revision notes.

Ask the user before applying changes when:

- The same source implies conflicting facts.
- Dependencies, ownership, or lifecycle facts cannot be inferred without
  materially changing the plan.
- A process has so little evidence that any effort estimate would be arbitrary
  rather than project-manager judgment.
- A status change would mark work done without a finished timestamp.
- A resource/calendar change would affect many future processes and the intended
  effective interpretation is unclear.
- A topology rewrite would retire or replace user-visible process symbols and
  the alias target is not obvious.

After successful reconciliation, move each fully incorporated note to
`reconciled/`. Use a dated subfolder when it helps traceability. Do not move a
source file while material questions from that file remain unresolved.

## Planning Judgment

Represent work at the level a project manager can track. Split a process when it
has independently trackable outputs, different dependencies, different roles, or
materially different risk. Collapse a subgraph when its nodes are too fine to
manage separately and the replacement can conserve role effort meaningfully.

Use dependencies only for real precedence constraints. Do not encode priority,
preference, due dates, or blocked state as dependencies.

Use whole-number role effort hours as the scheduling input. Do not invent
process due dates, project due dates, horizons, or unallocated work concepts.
ProjDash computes resource-aware schedules from role effort, dependencies,
lifecycle anchors, calendars, holidays, and resource-role eligibility. Treat a
resource-hour as binary: a resource is either allocated to one process for that
hour or not allocated; never plan fractional resource-hour allocations.

Use each resource's `calendar_id` as its default unbounded calendar. When notes
describe a different availability pattern for a bounded period, keep the
default calendar and add a `calendar_overrides` rule for that period instead of
rewriting the default calendar or encoding the change as holidays.

Infer role effort as part of the project-manager role. Use the best available
evidence: stated expectations, task shape, number of deliverables, ownership,
meeting commitments, comparable completed work in the project, current resource
capacity, and observed rework. Store a concrete whole-number estimate because
the service requires one; use the assumption note to record the basis,
uncertainty, and whether the estimate is provisional. Prefer a defensible
estimate over blocking on missing explicit hours, but do not hide a weak
estimate.

Keep process descriptions useful for future managers. A good description says
what "done" means, names important assumptions, and includes the reason for
current estimates when those estimates came from notes.

Use aliases when people refer to the same process by different names. Preserve
old symbols as aliases after renames when a note or conversation may still use
the old label.

## Slippage and Calibration

Treat slippage as a planning signal, not just a report field. Before committing
a reconciled state, query the latest schedule snapshots for the relevant
terminal processes and compare prior `completion_at` values to the new
projection. Explain whether changes came from scope, estimates, resource
capacity, dependencies, blockers, lifecycle status, or calendar changes.

When notes contain actual progress, update process status and revise remaining
role effort when appropriate. Do not silently erase slippage by shortening
future work without evidence; when estimates change, cite why the prior estimate
missed and what new information justifies the update.

Periodically run a retrospective lookback, especially after milestones, major
deliverables, or several reconciliations. Review source materials, completed
processes, actual start/finish timestamps, prior snapshots, blockers, and scope
changes. Identify where estimates were accurate, optimistic, pessimistic, or
invalidated by scope change. Convert the lessons into updated role-effort
estimates, calendar assumptions, resource assignments, blockers, or process
templates, then commit a snapshot with a note that records the calibration.

## Daily Briefing

For an overview request, query context and avoid mutating state unless the user
also asked to reconcile notes.

Report:

- P0 work: processes past `LF`.
- P1 work: processes past `LS`.
- P2 work: processes inside the `ES` to `LS` work window.
- P3 watchlist: upcoming processes before `ES`.
- Current blockers, blocker owners if known, and the next unblock action.
- Critical path processes and inferred durations.
- Role-prioritized and resource-prioritized work.
- Resource or role utilization pressure.
- Slippage since the prior committed snapshot.
- Estimate-confidence issues and the next evidence needed to improve them.
- Questions whose answers would change topology, effort, status, or resources.

Keep daily action items specific: process symbol, action, role/resource, blocker
or dependency, and the timestamp context used for the query.

## Mutation Discipline

Use service commands for all changes. Never edit the database directly.

Use timezone-aware datetimes for every `*_at` field. Prefer the project
timezone for operator-facing times, while preserving explicit source timezones
from notes when present.

Validate after each logical batch:

- The command result is `ok`.
- Graph updates did not introduce cycles.
- Required roles exist and have eligible active resources when work is future
  schedulable.
- Inferred effort estimates have source-backed assumption notes when they are
  new, materially revised, or uncertain.
- Resource calendars recur and holidays are in the calendar timezone.
- Blockers are represented as blockers, not as schedule delays.
- Started processes have pinned starts.
- Done processes have finished timestamps.

Commit with `commit_project_state` only after the current reconciled state is
intentional. Use the commit note to cite reconciled source filenames and the
main planning changes. If the user wants exploration only, do not commit and do
not move notes to `reconciled/`.

## Output Standard

End a reconciliation with:

- Files reconciled and files left unreconciled.
- Service commands applied, grouped by purpose.
- Schedule/slippage changes from the latest context query, including comparison
  against the prior committed snapshot when one exists.
- Current blockers and immediate action items.
- Estimate changes, confidence, and calibration lessons.
- Questions that remain open.

End a daily briefing with current priorities and risks first. Include only the
supporting schedule details needed to justify the recommendations.

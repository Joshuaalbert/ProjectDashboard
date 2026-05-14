---
name: projdash-project-manager
description: Project-management operating procedure for using ProjDash as an agentic planning tool. Use when Codex is asked to reconcile meeting notes, emails, Slack exports, or teammate updates from unreconciled folders into a ProjDash project; update project topology, roles, resources, blockers, statuses, estimates, calendars, or slippage snapshots; produce daily PM briefings, current action items, schedule-risk summaries, or agent-readable project context from the ProjDash service.
---

# ProjDash Project Manager

## Operating Model

Treat ProjDash as the source of truth for project facts and schedule projections.
Use notes, transcripts, emails, and chat exports as evidence that should become
validated service commands, not free-form side summaries.

Prefer this loop:

1. Read pending source material from `unreconciled/`.
2. Query current project state with `query_agent_context`.
3. Extract proposed changes with explicit evidence from the notes.
4. Ask concise questions when facts are ambiguous or conflicting.
5. Apply validated service commands.
6. Re-query context, schedule, blockers, utilization, and slippage.
7. Commit the project state when the reconciled state is intentional.
8. Move incorporated source files to `reconciled/`.
9. Report the changes, risks, action items, and unresolved questions.

Read [references/projdash-api.md](references/projdash-api.md) before issuing
service commands or writing JSON envelopes.

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
- New roles, resources, resource calendars, holidays, cost rates, and resource
  role assignments.
- Status changes: started, paused, done, canceled.
- Blockers and blocker resolutions.
- Earliest-start constraints.
- Assumption changes that should be recorded in revision notes.

Ask the user before applying changes when:

- The same source implies conflicting facts.
- A new process lacks enough information to estimate role effort or dependencies.
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

Use role effort hours as the scheduling input. Do not invent process due dates,
project due dates, horizons, or unallocated work concepts. ProjDash computes
resource-aware schedules from role effort, dependencies, lifecycle anchors,
calendars, holidays, and resource-role eligibility.

Keep process descriptions useful for future managers. A good description says
what "done" means, names important assumptions, and includes the reason for
current estimates when those estimates came from notes.

Use aliases when people refer to the same process by different names. Preserve
old symbols as aliases after renames when a note or conversation may still use
the old label.

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
- Schedule/slippage changes from the latest context query.
- Current blockers and immediate action items.
- Questions that remain open.

End a daily briefing with current priorities and risks first. Include only the
supporting schedule details needed to justify the recommendations.

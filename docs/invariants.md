# Project Invariants

## API and Time

Invariant: The service is the authority for validation, persistence, graph rewrites, schedule projection, resource
allocation, utilization, cost summaries, committed slippage snapshots, and Slack workflow state.

Invariant: Agents interact with project state through typed Python models or validated JSON command and query envelopes.

Invariant: All service API and persistence moments are timezone-aware datetimes.

Invariant: Moment fields use `*_at` names such as `start_at`, `effective_at`, `as_of`, `now`, lifecycle anchors,
calendar exceptions, holidays, allocation slices, and schedule snapshots.

Invariant: Commands mutate durable facts and queries compute projections from those facts.

Invariant: Batch commands stage changes in memory and commit through the repository in one replacement step.

## Blockers

Invariant: No blocker shall exist that has no reference to a process.

Invariant: A blocker shall not reference more than one process.

Invariant: Blockers are single-concept items backed by `process_type="blocker"` resolver processes named
`resolve-<blocker_name>`.

Invariant: Every unresolved blocker is represented by a mandatory resolver process dependency that remains a parent of
the blocker-referenced process until and after resolution.

Invariant: If a process is initially started then a blocker assigned then immediately the process should become
early_start, because the blocker is now an unfinished parent process.

Invariant: Active blocker resolver dependencies are maintained automatically and cannot be removed from processes with
the blocker reference. I.e. process A (holds list of blocker ids) ==> each of those blockers is an automatic parent.

Invariant: Blocker metadata is not a planning primitive.

Invariant: Planning sees blockers only through normal process mechanics such as resolver processes, dependency edges,
roles, resources, calendars, and statuses.

Invariant: Blockers do not change computed schedule timing and instead appear through blocker summaries and resolver
dependencies for review and prioritization.

Invariant: Deleting a process deletes each blocker and blocker resolver that is tied to that process.

## Completedness

Invariant: Process graph derived state is exactly one of these: `waiting`, `early_start`, `ready`,
`started`, `due`, or `finished`.

Invariant: A process is `due` if it is pinned, not past its forecast finish time, not verified finished, and all parents
are finished.

Invariant: Process lifecycle state, start time, and finish time are query projections only and are never persisted on
the
process record.

Invariant: There is no special state for overdue or late processes, as those no long make sense.

Invariant: There is no special blocked state for blockers, as those are just processes, with child holding references.

Invariant: Lateness and overdue risk are schedule-pressure observations and not completedness states.

Invariant: A process with any unfinished parent and no active pin is in `waiting`.

Invariant: A process with any unfinished parent and an active pin is in `early_start`.

Invariant: A process with all finished parents and no active pin is in `ready`.

Invariant: A process cannot be verified finished unless it has an active pin.

Invariant: A process with all finished parents and a verified finished pin is `finished`.

Invariant: A process with all finished parents, an active pin, not past forecast finish time, and not verified finished
is
in `due`.

Invariant: A process with all finished parents, an active pin, past forecast finish time, and not verified finished is
in
`started`.

Invariant: A process that is one of `early_start`, `started`, `due`, or `finished` has derived start time equal to its
active pin's pinned start datetime.

Invariant: If a process that is not in `early_start`, `started`, `due`, or `finished` has no derived start time.

Invariant: A `finished` process uses its active pin's verified finish datetime as its finish datetime.

Invariant: A process that is not `finished` has no derived finish time.

Invariant: A process cannot be derived `finished` while any parent process is unfinished.

Invariant: All processes must have exactly one role requirement defined.

Invariant: Durable revision storage must reject revision rows unless `role_requirements` contains exactly one item.

Invariant: All role requirements must have non-zero effort hour definitions.

## Dependency Graph

Invariant: The process dependency graph is a directed acyclic graph.

Invariant: A process dependency always points from predecessor process to successor process.

Invariant: Dependency-only CPM is diagnostic graph metadata and is not authoritative for committed slippage.

Invariant: Terminal-symbol scope is the induced ancestor subgraph of the requested terminal processes.

## PM Evidence

Invariant: PM Slack runs start from service-prepared project-manager markdown context.

Invariant: PM agents answer every service-prepared evidence line item with Yes or No before drafting teammate or
team-channel messages.

Invariant: Process evidence freshness is line-item-specific.

Invariant: Updating blockers does not refresh role requirements, done criteria, pin data, or planned-resource evidence
unless that underlying value changed.

Invariant: PM process evidence uses `pin_data` for process pin correctness; legacy staking-resource and standalone
process-finished evidence line items are not rendered or persisted.

Invariant: PM markdown context marks stale evidence for current, blocked, active, and soon-starting processes so those
line items can be prioritised before lower-impact work.

Invariant: PM evidence freshness targets are P0 under one day, P1 under three days, P2 under seven days, and P3 under
fourteen days.

Invariant: PM process priority is P0 for started, due, early-started, or planned start under three days; P1 for planned
start under seven days; P2 for planned start under fourteen days; and P3 for planned start fourteen days or later.

Invariant: PM markdown context prefixes stale evidence line items with a service-rendered `*`.

Invariant: PM markdown context keeps dependency parents grouped per successor, milestone terminal-process and makespan
lines explicit, process expander content readable, and evidence rows in the
`<process>.<attribute> last modified ... last evidence ...` shape.

Invariant: PM agents must not add multiple role requirements to one process; multiple owners, specialties, or
independent
forecasts are represented as multiple one-role processes.

## Process Pins

Invariant: Process pins are the auditable source of process pinned state.

Invariant: A process cannot be pinned without a recorded pinned resource, pinned start time, and forecast finish time.

Invariant: A process pin becomes verified finished only after resource verification with a `verified_finished_at`.

Invariant: A process pin that is verified finished must have been pinned started.

Invariant: When a process removes its pin, it immediately loses pinned start, pinned resource, forecast, and any
possible
verified finished state and `verified_finished_at`.

Invariant: Process pins may predate dependency readiness as head-start work while remaining not verified finished.

Invariant: A process cannot become verified finished while any parent process is unfinished.

Invariant: A pinned process cannot be verified finished without a specific `verified_finished_at` datetime.

Invariant: When a pinned process is verified finished its forecast finish datetime is set immediately to the verified
finished datetime.

Invariant: Process finish is gated by parent completion even when head-start process work was forecast earlier.

Invariant: A resource that is pinned to any process is removed entirely from available planning until the forecasted
date.

Invariant: A resource that is not currently pinned to any process is available for planning starting from the current
planning `now`, and subject to their calendar.

Invariant: A pinned started datetime cannot be in the future.

## Resources and Scheduling

Invariant: All planning happens directly on the process dependency graph.

Invariant: Each process is one atomic schedulable work package.

Invariant: The sole role requirement provides the process effort, compatible role, and pin target.

Invariant: A human-level deliverable requiring multiple roles is modeled as multiple one-role processes with explicit
dependencies, not as one process with multiple role requirements.

Invariant: Resource-aware scheduling is driven by process effort hours, compatible resources, resource calendars, pins,
and process dependencies.

Invariant: Processes do not carry target-date fields.

Invariant: `earliest_start_at` is a not-before constraint.

Invariant: A planned unpinned process cannot receive a planned start earlier than the planning `now`.

Invariant: Calendars define local working windows in their own timezone.

Invariant: Resource holidays are interpreted in the assigned calendar timezone.

Invariant: Calendars recur indefinitely without an operator-supplied schedule horizon for schedule, graph, utilization,
cost, or slippage computations.

Invariant: Resource allocation should be contiguous for a resource within a single work session, i.e. only one process
within that session.

Invariant: A resource can switch to another process within a work session IFF the previous process is planned completed.

Invariant: A resource can focus on only one process per resource-hour bucket.

Invariant: Allocation never exceeds a resource bucket's available capacity.

Invariant: The commitment-window MCTS backend owns the schedule when selected and uses heuristic scheduling only as its
prior or rollout policy.

Invariant: Permanent resource infeasibility is reported as a structured `resource_schedule_unsatisfiable` query error.

Invariant: Resource-aware completion is authoritative for committed slippage points.

Invariant: During planning the planned start of a process is the maximum of `now` and the maximum of planned
finishes or verified finishes of its parents.

## Slack and Outbox

Invariant: Slack teammate draft messages are self-contained for people without ProjDash or dashboard access.

Invariant: Slack teammate draft messages avoid internal scheduling and tool terms such as graph, LS, LF, ES, EF, slack,
critical path, schedule snapshot, process id, role id, and blocker id.

Invariant: PM Slack runs include both collected Slack messages and unreconciled manual notes as evidence.

Invariant: Successful PM Slack runs archive reconciled manual notes under `reconciled/manual_notes/<run_id>`.

Invariant: Slack UI runs are guarded by persisted `SlackRun` rows.

Invariant: Active persisted Slack runs without an in-process worker are treated as interrupted or orphaned and require a
UI recovery path before a new run starts.

Invariant: Slack team-channel drafts are first-class outbox targets and not fake teammate direct messages.

Invariant: Slack outbox targets are either `dm` with a Slack user id or `channel` with a Slack channel id.

Invariant: Accepted PM Slack drafts include `pm_evidence_claims`.

Invariant: Sent Slack outbox ids are the auditable proof of satisfied PM communication obligations.

Invariant: Slack PM agents return draft message content as `message_markdown` only.

## SQLite Storage

Invariant: SQLite is the durable local service store.

Invariant: Project facts are persisted as typed JSON rows while scheduling fields remain computed query outputs.

Invariant: SQLite storage preserves project facts, process revisions, calendars, resources, blockers, schedule
snapshots, command replay records, and Slack configuration, token, run, and outbox fields.

Invariant: Local state databases and secrets are not committed.

## Topology and Snapshots

Invariant: `commit_project_state` records a slippage-history point only when an operator explicitly commits the current
planning state.

Invariant: Repeated schedule commits for the same project, timestamp, and terminal symbol set are idempotent.

Invariant: Each committed schedule snapshot stores the commit timestamp, selected terminal symbols, schedule basis,
completion datetime, convergence state, and optional note.

Invariant: `replace_process_with_subgraph` connects original external predecessors to replacement roots and replacement
leaves to original external children.

Invariant: `collapse_subgraph` infers collapsed role effort from the selected subgraph when replacement role
requirements are omitted.

Invariant: Deleting a process removes dependency references to it from the remaining graph.

## UI

Invariant: The UI calls the service for reads and writes and does not mutate persistence directly.

Invariant: Gantt bar coloring is keyed to process derived state.

Invariant: Gantt rows are at the derived process level.

Invariant: For each pinned process, there shall be an "o" overlaid on the Gantt bar at the pinned start time.

Invariant: For each verified finished process, there shall be an "x" overlaid on the Gantt bar at the verified finish
time.

Invariant: A Gantt row has at most one pinned-start "o" marker because each process has exactly one role requirement.

Invariant: A Gantt row has at most one verified-finish "x" marker because each process has exactly one role requirement.

Invariant: Gantt rows are in topological order from top to bottom, such that if a process A is parent to process B then
A appears above B.

Invariant: For each parent child relationship there should be a step-type line connecting the planned finish or verified
finish of the parent to the planned start of the child or pinned started time of the child, where the step should
consist of 3 segments: horizontal to the mid-point, vertical to the child, horizontal to the child bar. Black, lw=1,
solid line.

Invariant: The ordering of processes in the chart should be such that the children of a parent are as close to the
parent as possible, minimising the vertical distance between parent and children, while still respecting topological
order.
